"""Unit tests for the AI layer, driven by the deterministic fake CLI in tests/fakes/claude — no network,
no real `claude`, no cost: translations (titles/excerpts and full bodies), comment summaries, link
reasons, `ask`, the per-text-hash caches under the temp HOME, USAGE accounting and AI_FAILURES.

Two invariants matter most here and both have shipped broken before:
  * a result that is already cached must NOT reach the CLI again (asserted through FAKE_AI_LOG);
  * cache_merge() is called from several finished jobs at once (0.17.0 parallel AI jobs), so concurrent
    merges must not lose entries.

Run: python3 -m unittest tests.test_ai -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as testenv  # noqa: E402

gg = testenv.load_module()

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_CLAUDE = os.path.join(HERE, "fakes", "claude")


class AiCase(unittest.TestCase):
    """Fake AI CLI + a throwaway CACHE_DIR, restored afterwards."""

    def setUp(self):
        self.assertTrue(os.access(FAKE_CLAUDE, os.X_OK), "tests/fakes/claude must be executable")
        self.tmp = tempfile.mkdtemp(prefix="gg-ai-test-")
        self.log = os.path.join(self.tmp, "ai.log")
        self._old = (gg.CACHE_DIR, gg.CLAUDE_BIN, gg.IS_CLAUDE, dict(gg.USAGE), list(gg.AI_FAILURES))
        # a private cache dir per test (AI results must not leak between tests) that still holds the
        # fixture repo, so fixture_graph() below reads it instead of trying to fetch
        home = testenv.make_home(self.tmp)
        gg.CACHE_DIR = os.path.join(home, ".cache", "gitgraph")
        gg.CLAUDE_BIN = FAKE_CLAUDE
        gg.IS_CLAUDE = True                       # basename is "claude": the JSON backend path
        os.environ["FAKE_AI_LOG"] = self.log
        gg.USAGE.update({"calls": 0, "input": 0, "cache_read": 0, "cache_create": 0, "output": 0,
                         "cost_usd": 0.0, "by": {}})
        gg.AI_FAILURES.clear()
        self.addCleanup(self._restore)
        self.assertTrue(gg.CACHE_DIR.startswith(tempfile.gettempdir()))

    def _restore(self):
        gg.CACHE_DIR, gg.CLAUDE_BIN, gg.IS_CLAUDE, usage, failures = self._old
        gg.USAGE.clear()
        gg.USAGE.update(usage)
        gg.AI_FAILURES[:] = failures
        for k in ("FAKE_AI_LOG", "FAKE_AI_FAIL", "FAKE_AI_BAD_LEN", "FAKE_AI_IS_ERROR", "FAKE_AI_SLEEP"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def calls(self):
        """[{argv, prompt}] the fake CLI has received so far."""
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def cache(self, name):
        path = os.path.join(gg.CACHE_DIR, name)
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)


class TestBackendDispatch(AiCase):
    def test_claude_backend_is_selected_by_basename(self):
        self.assertEqual(gg.ai_backend(FAKE_CLAUDE), "claude")
        self.assertEqual(gg.ai_backend("/usr/bin/codex"), "codex")
        self.assertEqual(gg.ai_backend("/usr/bin/whatever"), "generic")

    def test_the_json_reply_is_unwrapped_and_usage_accounted(self):
        out = gg._ai_call("plain prompt", "haiku", "translate")
        self.assertTrue(out.startswith("TRFULL:"))
        self.assertEqual(gg.USAGE["calls"], 1)
        self.assertEqual(gg.USAGE["output"], 7)
        self.assertEqual(gg.USAGE["cache_read"], 100)
        self.assertAlmostEqual(gg.USAGE["cost_usd"], 0.001)
        self.assertIn("translate", gg.USAGE["by"])

    def test_the_model_is_passed_on_the_command_line(self):
        gg._ai_call("x", "sonnet", "ask")
        self.assertIn("sonnet", self.calls()[0]["argv"])

    def test_a_failing_cli_records_an_ai_failure(self):
        os.environ["FAKE_AI_FAIL"] = "1"
        with self.assertRaises(ValueError):
            gg.claude_call("x", "haiku", "translate")
        self.assertEqual(len(gg.AI_FAILURES), 1)
        self.assertEqual(os.path.basename(gg.AI_FAILURES[0]["bin"]), "claude")
        self.assertIn("login", gg.AI_FAILURES[0]["msg"].lower())

    def test_is_error_in_the_reply_raises(self):
        os.environ["FAKE_AI_IS_ERROR"] = "1"
        with self.assertRaises(ValueError):
            gg.claude_call("x", "haiku", "translate")

    def test_a_missing_binary_names_gg_ai(self):
        gg.CLAUDE_BIN = os.path.join(self.tmp, "not-installed-claude")
        with self.assertRaises(ValueError) as cm:
            gg._ai_call("x", "haiku", "translate")
        self.assertIn("gg ai", str(cm.exception))


class TestClaudeJson(AiCase):
    def test_a_wrong_length_reply_is_rejected(self):
        os.environ["FAKE_AI_BAD_LEN"] = "1"
        prompt = gg.TR_PROMPT.format(lang="Korean", payload=json.dumps(["a", "b", "c"]))
        with self.assertRaises(ValueError):
            gg.claude_json(prompt, 3)

    def test_a_correct_reply_is_returned_as_a_list(self):
        prompt = gg.TR_PROMPT.format(lang="Korean", payload=json.dumps(["a", "b"]))
        self.assertEqual(gg.claude_json(prompt, 2), ["TR:a", "TR:b"])


class TestTranslate(AiCase):
    def test_texts_are_translated_and_cached_by_text_and_language(self):
        out = gg.translate_texts(["空间不足", "hello"], lang="Korean")
        self.assertEqual(out, {"空间不足": "TR:空间不足", "hello": "TR:hello"})
        self.assertEqual(set(self.cache("translations.json")), {"Korean:空间不足", "Korean:hello"})

    def test_a_cached_text_never_reaches_the_cli_again(self):
        gg.translate_texts(["空间不足"], lang="Korean")
        self.assertEqual(len(self.calls()), 1)
        gg.translate_texts(["空间不足"], lang="Korean")
        self.assertEqual(len(self.calls()), 1, "the second call must be served from translations.json")

    def test_duplicates_are_asked_for_once(self):
        gg.translate_texts(["same", "same", "other"], lang="Korean")
        payload = json.loads(self.calls()[0]["prompt"].rsplit("\n\n", 1)[-1])
        self.assertEqual(payload, ["same", "other"])

    def test_batches_are_capped_at_tr_batch(self):
        texts = [f"文{i}" for i in range(gg.TR_BATCH + 5)]
        gg.translate_texts(texts, lang="Korean")
        sizes = [len(json.loads(c["prompt"].rsplit("\n\n", 1)[-1])) for c in self.calls()]
        self.assertEqual(sizes, [gg.TR_BATCH, 5])

    def test_a_failure_keeps_the_originals_instead_of_raising(self):
        os.environ["FAKE_AI_FAIL"] = "1"
        self.assertEqual(gg.translate_texts(["空间不足"], lang="Korean"), {})

    def test_prepare_translations_only_touches_what_needs_it(self):
        g = testenv.fixture_graph(gg)
        n = gg.prepare_translations(g, "zh")
        self.assertGreater(n, 0, "the fixture has at least one CJK title")
        for node in g.nodes.values():
            if node.kind == "item" and node.tr_title:
                self.assertTrue(gg.needs_translation(node.title, "zh"))
                self.assertTrue(node.tr_title.startswith("TR:"))
            if node.kind == "item" and gg.HANGUL_RE.search(node.title or ""):
                self.assertIsNone(node.tr_title, "text already in the target language is left alone")

    def test_mode_none_asks_nothing(self):
        g = testenv.fixture_graph(gg)
        self.assertEqual(gg.prepare_translations(g, "none"), 0)
        self.assertEqual(self.calls(), [])

    def test_translate_body_caches_by_body_hash(self):
        g = testenv.fixture_graph(gg)
        node = next(n for n in g.nodes.values() if n.kind == "item" and (n.body or "").strip())
        first = gg.translate_body(node, g, lang="Korean")
        self.assertTrue(first)
        n_calls = len(self.calls())
        self.assertGreater(n_calls, 0)
        self.assertEqual(gg.translate_body(node, g, lang="Korean"), first)
        self.assertEqual(len(self.calls()), n_calls, "translations_full.json must serve the second call")
        self.assertTrue(self.cache("translations_full.json"))


class TestSummaries(AiCase):
    def test_summaries_are_returned_and_cached(self):
        entries = [("k1", {"kind": "comment", "text": "found the bug in recovery"}),
                   ("k2", {"kind": "issue", "text": "device full"})]
        out = gg.summarize_comments(entries, lang="Korean")
        self.assertEqual(out["k1"], "SUM:found the bug in recovery")
        self.assertIn("Korean:k1", self.cache("summaries.json"))

    def test_a_cached_summary_is_not_asked_again(self):
        entries = [("k1", {"kind": "comment", "text": "x"})]
        gg.summarize_comments(entries, lang="Korean")
        gg.summarize_comments(entries, lang="Korean")
        self.assertEqual(len(self.calls()), 1)

    def test_batches_are_capped_at_40_entries(self):
        entries = [(f"k{i}", {"kind": "comment", "text": f"t{i}"}) for i in range(45)]
        gg.summarize_comments(entries, lang="Korean")
        sizes = [len(json.loads(c["prompt"].rsplit("\n\n", 1)[-1])) for c in self.calls()]
        self.assertEqual(sizes, [40, 5])

    def test_batches_are_also_capped_by_characters(self):
        big = "x" * (gg.SUM_BATCH_CHARS // 3)
        entries = [(f"k{i}", {"kind": "comment", "text": big}) for i in range(5)]
        gg.summarize_comments(entries, lang="Korean")
        self.assertGreater(len(self.calls()), 1, f"{gg.SUM_BATCH_CHARS} chars per call is the cap")

    def test_prepare_summaries_marks_the_graph(self):
        g = testenv.fixture_graph(gg)
        gg.prepare_summaries(g)
        summarised = [n for n in g.nodes.values() if n.summary]
        self.assertTrue(summarised)
        self.assertTrue(all(n.summary.startswith("SUM:") for n in summarised))


class TestLinkReasons(AiCase):
    def test_link_reasons_are_generated_and_cached(self):
        """0.18.0 restored WHY_PROMPT: 0.17.0 dropped its definition while keeping the use at
        summarize_whys(), so every call raised NameError inside its own try/except and the ↳ link
        reason silently never appeared. Keep this test asserting a real reply, not an empty dict.
        """
        self.assertTrue(hasattr(gg, "WHY_PROMPT"), "0.17.0 shipped without it; do not drop it again")
        out = gg.summarize_whys([("k1", {"ref": "#2", "sentence": "see #2 for the fix"})], lang="Korean")
        self.assertEqual(out, {"k1": "WHY:#2"})
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("Korean:k1", self.cache("whys.json"))
        gg.summarize_whys([("k1", {"ref": "#2", "sentence": "see #2 for the fix"})], lang="Korean")
        self.assertEqual(len(self.calls()), 1, "the cached reason must not be asked again")

    def test_prepare_whys_fills_the_graph(self):
        g = testenv.fixture_graph(gg)
        pairs = [pr for pr in g.ctx][:2]
        if pairs:
            gg.prepare_whys(g, pairs)
            self.assertTrue(any(v for v in g.why.values()), "g.why stays empty when WHY_PROMPT is gone")

    def test_a_cached_reason_is_still_served_without_the_cli(self):
        # the cache path is independent of the broken prompt, and is what keeps old reasons visible
        with open(os.path.join(gg.CACHE_DIR, "whys.json"), "w", encoding="utf-8") as f:
            json.dump({"Korean:k1": "충돌 여부를 확인한 관련 PR"}, f, ensure_ascii=False)
        out = gg.summarize_whys([("k1", {"ref": "#2", "sentence": "s"})], lang="Korean")
        self.assertEqual(out, {"k1": "충돌 여부를 확인한 관련 PR"})


class TestAsk(AiCase):
    def test_the_prompt_carries_the_thread_and_the_links(self):
        g = testenv.fixture_graph(gg)
        nid = next(n.id for n in g.nodes.values()
                   if n.kind == "item" and g.comments_of(n.id) and not n.stub)
        answer = gg.ask_claude(g, nid, "why does it mention that PR?", model="sonnet", lang="Korean")
        self.assertEqual(answer, "ANSWER: why does it mention that PR?")
        prompt = self.calls()[0]["prompt"]
        self.assertIn("comments, oldest first", prompt)
        self.assertIn("=== question ===", prompt)
        self.assertIn("why does it mention that PR?", prompt)

    def test_context_for_a_comment_marks_the_selected_one(self):
        g = testenv.fixture_graph(gg)
        cid = next(n.id for n in g.nodes.values() if n.kind == "comment")
        kind, label, text = gg.ask_context(g, cid)
        self.assertEqual(kind, "comment")
        self.assertIn("THE COMMENT THE USER IS LOOKING AT", text)

    def test_context_is_capped(self):
        g = testenv.fixture_graph(gg)
        nid = next(n.id for n in g.nodes.values() if n.kind == "item" and not n.stub)
        _, _, text = gg.ask_context(g, nid)
        self.assertLessEqual(len(text), gg.ASK_MAX_CHARS + 100)


class TestCacheMergeConcurrency(AiCase):
    """Regression for 0.17.0: several AI jobs finish at once and merge into the same cache file."""

    def test_no_update_is_lost_when_threads_merge_at_the_same_time(self):
        path = os.path.join(gg.CACHE_DIR, "translations.json")
        threads = [threading.Thread(target=gg.cache_merge, args=(path, {f"k{i}": f"v{i}"})) for i in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
        self.assertEqual(got, {f"k{i}": f"v{i}" for i in range(40)})

    def test_merge_keeps_existing_entries(self):
        path = os.path.join(gg.CACHE_DIR, "summaries.json")
        gg.cache_merge(path, {"a": "1"})
        gg.cache_merge(path, {"b": "2"})
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"a": "1", "b": "2"})


class TestOnDemandTranslationOnly(unittest.TestCase):
    """0.18.0: the main content is translated only when the user asks (i, or the [i 번역] title button).
    Nothing is translated in the background any more, so there is no auto_translate setting and no
    maybe_auto_translate() step in the draw path."""

    def test_the_auto_translate_setting_is_gone(self):
        self.assertNotIn("auto_translate", gg.CONFIG_KEYS)

    def test_no_background_translation_hook_remains(self):
        self.assertFalse(hasattr(gg.Tui, "maybe_auto_translate"))
        self.assertTrue(hasattr(gg.Tui, "translate_content"), "i must still translate on demand")

    def test_a_fresh_tui_starts_on_the_original(self):
        # show_tr decides original vs translation in refresh_main(); a new Tui must start with it False
        # (tests/tui_smoke.py checks the visible half of this: the body stays untranslated until i).
        src = open(gg.__file__, encoding="utf-8").read()
        init = src[src.index("    def __init__(self, scr, opts):"):src.index("    # ------------------------------------------------------------------ background work")]
        self.assertIn("self.show_tr = False", init)


class TestAiPickers(unittest.TestCase):
    def test_installed_ais_excludes_the_current_one(self):
        names = gg.installed_ais(exclude="claude")
        self.assertNotIn("claude", names)

    def test_every_backend_has_a_login_hint(self):
        for name, (how, hint) in gg.AI_BACKENDS.items():
            self.assertTrue(how and hint, f"{name} is missing its description or login hint")


class TestGenericBackend(AiCase):
    """A CLI whose basename is not claude/codex takes the plain `-p PROMPT` path: stdout is the answer."""

    def test_plain_stdout_is_used_and_only_the_call_is_counted(self):
        generic = os.path.join(self.tmp, "mycli")
        with open(generic, "w") as f:
            f.write("#!/bin/sh\necho 'plain answer'\n")
        os.chmod(generic, 0o755)
        gg.CLAUDE_BIN, gg.IS_CLAUDE = generic, False
        self.assertEqual(gg._ai_call("x", "haiku", "ask"), "plain answer")
        self.assertEqual(gg.USAGE["calls"], 1)
        self.assertEqual(gg.USAGE["input"], 0, "only claude reports tokens")


if __name__ == "__main__":
    unittest.main()
