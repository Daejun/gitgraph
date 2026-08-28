"""The review pass itself (gitgraph.py's "the review pass" part, 0.23.0): how a reply is parsed, how a
big diff is split, and what run_review() does with what comes back — including the answers that are not
valid, since an AI CLI produces those regularly.

No AI CLI is started here: claude_call() is replaced per test. tests/fakes/claude covers the other
direction (the real subprocess path) in tui_smoke.py.

Run: python3 -m unittest tests.test_review_ai -v
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as testenv  # noqa: E402
from test_review_rows import DIFF, review  # noqa: E402

gg = testenv.load_module()


def block(payload):
    return "some prose first\n\n<<<GG_REVIEW\n" + payload + "\nGG_REVIEW>>>\ntrailing words"


GOOD = """{"reachability": {"verdict": "confirmed", "reason": "reached from write_begin"},
 "changes": [{"cid": "CHANGE-1", "kind": "locking", "path": "fs/f2fs/data.c",
              "symbol": "f2fs_write_page", "summary": "the error path"}],
 "findings": [{"cid": "CHANGE-1", "severity": "bug", "path": "fs/f2fs/data.c", "line": 221,
               "side": "RIGHT", "title": "lock leak", "body": "out_unlock keeps i_lock.",
               "evidence": "data.c:221 -> data.c:224", "diff": "--- a\\n+++ b\\n"}]}"""


class TestJsonBlock(unittest.TestCase):
    def test_between_the_markers(self):
        self.assertEqual(gg._json_block(block('{"a": 1}'), "GG_REVIEW"), {"a": 1})

    def test_a_fenced_block_between_the_markers(self):
        self.assertEqual(gg._json_block("<<<GG_REVIEW\n```json\n{\"a\": 1}\n```\nGG_REVIEW>>>", "GG_REVIEW"),
                         {"a": 1})

    def test_no_markers_falls_back_to_the_last_object(self):
        self.assertEqual(gg._json_block('chatter {"a": 1} more chatter', "GG_REVIEW"), {"a": 1})

    def test_prose_after_the_object_is_ignored(self):
        self.assertEqual(gg._json_block('{"a": 1}\n\nHope that helps!', "GG_REVIEW"), {"a": 1})

    def test_no_json_at_all(self):
        self.assertIsNone(gg._json_block("I could not review this.", "GG_REVIEW"))
        self.assertIsNone(gg._json_block("", "GG_REVIEW"))

    def test_a_bare_array_is_not_accepted(self):
        self.assertIsNone(gg._json_block("[1, 2, 3]", "GG_REVIEW"))


class TestParseReviewReply(unittest.TestCase):
    def test_a_good_reply(self):
        reach, changes, findings = gg.parse_review_reply(block(GOOD))
        self.assertEqual(reach, {"verdict": "confirmed", "reason": "reached from write_begin"})
        self.assertEqual([c.cid for c in changes], ["CHANGE-1"])
        self.assertEqual(changes[0].kind, "locking")
        f = findings[0]
        self.assertEqual((f.severity, f.path, f.line, f.side), ("bug", "fs/f2fs/data.c", 221, "RIGHT"))
        self.assertEqual(f.evidence, "data.c:221 -> data.c:224")
        self.assertTrue(f.digest)

    def test_an_unusable_reply_raises(self):
        with self.assertRaises(ValueError):
            gg.parse_review_reply("I had trouble reading the diff, sorry.")

    def test_an_empty_findings_list_is_a_valid_answer(self):
        _, _, findings = gg.parse_review_reply(block('{"findings": []}'))
        self.assertEqual(findings, [])

    def test_an_unknown_severity_falls_back_instead_of_crashing(self):
        _, _, f = gg.parse_review_reply(block('{"findings": [{"severity": "CRITICAL!!", '
                                              '"path": "a.c", "line": 1, "title": "t"}]}'))
        self.assertEqual(f[0].severity, "logic")

    def test_a_finding_without_a_title_is_dropped(self):
        _, _, f = gg.parse_review_reply(block('{"findings": [{"path": "a.c", "line": 1, "title": "  "},'
                                              ' {"path": "a.c", "line": 2, "title": "real"}]}'))
        self.assertEqual([x.title for x in f], ["real"])

    def test_junk_entries_are_skipped(self):
        _, _, f = gg.parse_review_reply(block('{"findings": ["not an object", null, '
                                              '{"path": "a.c", "line": 1, "title": "real"}]}'))
        self.assertEqual(len(f), 1)

    def test_side_is_normalised(self):
        _, _, f = gg.parse_review_reply(block('{"findings": [{"path": "a.c", "line": 1, "title": "t", '
                                              '"side": "left"}, {"path": "a.c", "line": 2, '
                                              '"title": "u", "side": "nonsense"}]}'))
        self.assertEqual([x.side for x in f], ["LEFT", "RIGHT"])

    def test_a_missing_reachability_is_not_invented(self):
        reach, _, _ = gg.parse_review_reply(block('{"findings": []}'))
        self.assertIsNone(reach)


class TestPromptAndChunking(unittest.TestCase):
    def test_the_diff_handed_over_names_every_file(self):
        rv = review()
        text = gg.diff_text(rv)
        for f in rv.files:
            self.assertIn(f"b/{f.path}", text)
        self.assertIn("@@ -220,6 +220,7 @@", text)
        self.assertIn("+\t\tgoto out_unlock;", text)

    def test_one_file_of_the_diff_can_be_taken_on_its_own(self):
        rv = review()
        only = gg.diff_text(rv, {"fs/f2fs/gc.c"})
        self.assertIn("b/fs/f2fs/gc.c", only)
        self.assertNotIn("data.c", only)

    def test_a_small_diff_is_one_call(self):
        self.assertEqual(gg.review_chunks(review()), [["fs/f2fs/data.c", "fs/f2fs/gc.c", "include/f2fs.h"]])

    def test_a_big_diff_is_split_by_file(self):
        rv = review()
        old = gg.REVIEW_MAX_BYTES
        gg.REVIEW_MAX_BYTES = 60
        try:
            chunks = gg.review_chunks(rv)
        finally:
            gg.REVIEW_MAX_BYTES = old
        self.assertGreater(len(chunks), 1)
        self.assertEqual(sorted(p for c in chunks for p in c), sorted(f.path for f in rv.files))

    def test_the_prompt_carries_the_pr_the_diff_and_the_contract(self):
        rv = review()
        rv.threads = [{"path": "fs/f2fs/gc.c", "line": 88, "resolved": False,
                       "comments": [{"author": "bob", "body": "why rename this?"}]}]
        p = gg.review_prompt(rv, [f.path for f in rv.files], body="allocate on zone boundaries")
        for want in ("test/repo#12", "allocate on zone boundaries", "GG_REVIEW>>>",
                     "@bob", "why rename this?", "b/fs/f2fs/data.c", "STEP 3 — reachability gate."):
            self.assertIn(want, p, want)

    def test_a_resolved_thread_is_not_quoted_back(self):
        rv = review()
        rv.threads = [{"path": "a.c", "line": 1, "resolved": True,
                       "comments": [{"author": "bob", "body": "already handled"}]}]
        self.assertNotIn("already handled", gg.review_prompt(rv, ["fs/f2fs/data.c"]))

    def test_a_partial_chunk_says_so(self):
        rv = review()
        p = gg.review_prompt(rv, ["fs/f2fs/gc.c"])
        self.assertIn("reviewing 1 of its 3 files", p)
        self.assertNotIn("reviewing 3 of its 3", gg.review_prompt(rv, [f.path for f in rv.files]))


class RunReviewCase(unittest.TestCase):
    """run_review() writes to the findings cache, so CACHE_DIR goes to a throwaway directory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gg-review-ai-")
        self._old = (gg.CACHE_DIR, gg.claude_call, gg.ai_available)
        gg.CACHE_DIR = os.path.join(self.tmp, ".cache", "gitgraph")
        os.makedirs(gg.CACHE_DIR)
        gg.ai_available = lambda: True
        self.calls = []
        self.addCleanup(self._restore)
        self.assertTrue(gg.CACHE_DIR.startswith(tempfile.gettempdir()))

    def _restore(self):
        gg.CACHE_DIR, gg.claude_call, gg.ai_available = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def answer(self, *replies):
        """Each call gets the next reply; an Exception instance is raised instead."""
        seq = list(replies)

        def fake(prompt, model, phase, timeout=300, cwd=None, tools=()):
            self.calls.append({"prompt": prompt, "model": model, "phase": phase,
                               "cwd": cwd, "tools": tools})
            out = seq.pop(0) if len(seq) > 1 else seq[0]
            if isinstance(out, Exception):
                raise out
            return out

        gg.claude_call = fake


class TestRunReview(RunReviewCase):
    def test_a_good_run_fills_the_review_and_the_cache(self):
        self.answer(block(GOOD))
        rv = review()
        gg.run_review(rv, body="d")
        self.assertEqual(rv.status, "done")
        self.assertIsNone(rv.error)
        self.assertEqual([f.title for f in rv.findings], ["lock leak"])
        self.assertEqual(rv.reachability["verdict"], "confirmed")
        self.assertEqual(rv.findings[0].anchor, "ok")
        cached = gg.cached_review(rv.repo, rv.number, rv.head_oid)
        self.assertEqual([f["title"] for f in cached["findings"]], ["lock leak"])

    def test_it_runs_in_the_worktree_with_read_only_tools(self):
        self.answer(block(GOOD))
        rv = review()
        gg.run_review(rv)
        self.assertEqual(self.calls[0]["cwd"], rv.worktree)
        self.assertIn("Read", self.calls[0]["tools"])
        self.assertTrue(any(t.startswith("Bash(git ") for t in self.calls[0]["tools"]))

    def test_a_finding_off_the_diff_is_anchored_before_it_is_stored(self):
        self.answer(block('{"findings": [{"severity": "bug", "path": "fs/f2fs/data.c", "line": 900,'
                          ' "side": "RIGHT", "title": "t", "evidence": "e"}]}'))
        rv = review()
        gg.run_review(rv)
        self.assertEqual(rv.findings[0].anchor, "moved")
        self.assertIn(rv.findings[0].line, rv.file("fs/f2fs/data.c").touched("RIGHT"))

    def test_an_unusable_reply_fails_the_review_instead_of_pretending(self):
        self.answer("I could not do it.")
        rv = review()
        gg.run_review(rv)
        self.assertEqual(rv.status, "failed")
        self.assertIn("GG_REVIEW", rv.error)
        self.assertEqual(rv.findings, [])

    def test_a_crashing_cli_fails_the_review(self):
        self.answer(ValueError("claude: not logged in"))
        rv = review()
        gg.run_review(rv)
        self.assertEqual(rv.status, "failed")
        self.assertIn("not logged in", rv.error)

    def test_one_bad_chunk_does_not_lose_the_others(self):
        old = gg.REVIEW_MAX_BYTES
        gg.REVIEW_MAX_BYTES = 60
        try:
            self.answer("nonsense", block(GOOD), block(GOOD))
            rv = review()
            gg.run_review(rv)
        finally:
            gg.REVIEW_MAX_BYTES = old
        self.assertEqual(rv.status, "done")
        self.assertTrue(rv.findings)
        self.assertIn("GG_REVIEW", rv.error)          # the failure is still reported

    def test_the_same_finding_from_two_chunks_is_kept_once(self):
        old = gg.REVIEW_MAX_BYTES
        gg.REVIEW_MAX_BYTES = 60
        try:
            self.answer(block(GOOD))
            rv = review()
            gg.run_review(rv)
        finally:
            gg.REVIEW_MAX_BYTES = old
        self.assertEqual(len(rv.findings), 1)

    def test_change_ids_are_renumbered_across_chunks(self):
        old = gg.REVIEW_MAX_BYTES
        gg.REVIEW_MAX_BYTES = 60
        try:
            self.answer(block(GOOD))
            rv = review()
            gg.run_review(rv)
        finally:
            gg.REVIEW_MAX_BYTES = old
        self.assertEqual([c.cid for c in rv.changes],
                         [f"CHANGE-{i}" for i in range(1, len(rv.changes) + 1)])

    def test_an_earlier_verdict_is_carried_onto_the_new_run(self):
        rv = review()
        rv.findings = [gg.Finding(path="fs/f2fs/data.c", line=221, title="lock leak", state="ignored")]
        gg.save_review(rv)
        self.answer(block(GOOD))
        again = review()
        gg.run_review(again)
        self.assertEqual(again.findings[0].state, "ignored")

    def test_an_empty_diff_is_refused_without_calling_the_cli(self):
        self.answer(block(GOOD))
        rv = review()
        rv.files = []
        gg.run_review(rv)
        self.assertEqual(rv.status, "failed")
        self.assertEqual(self.calls, [])

    def test_no_ai_cli_is_said_plainly(self):
        gg.ai_available = lambda: False
        self.answer(block(GOOD))
        rv = review()
        gg.run_review(rv)
        self.assertEqual(rv.status, "failed")
        self.assertIn("not installed", rv.error)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
