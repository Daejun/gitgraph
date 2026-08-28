"""Unit tests for the marks/answers layer: todo.json (marks made with `m` in the tui) and qa.json
(answers saved with the question that produced them), plus the `gg todo ...` CLI commands.

load_todo/save_todo/render_todo_md/todo_md_path/todo_find/todo_finish/todo_entry and load_qa/save_qa
are exercised in-process (env.load_module() + env.fixture_graph()); `gg todo`, `gg todo done|remove N`
and `gg todo clear-done` are driven end to end through a subprocess with env.child_env(), each on its
own fresh env.make_home() so CLI tests never share state with the in-process ones or each other.

Absolute rule (see tests/env.py): every test must first prove TODO_JSON / QA_JSON / todo_md_path() /
CACHE_DIR resolve under the temp HOME before touching them — never the real ~/.config/gitgraph or
~/gitgraph-todo.md.
"""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as testenv  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GITGRAPH_PY = os.path.join(ROOT, "gitgraph.py")


def run_gg(home, *args, **extra_env):
    return subprocess.run([sys.executable, GITGRAPH_PY] + list(args),
                           env=testenv.child_env(home, **extra_env), capture_output=True, text=True)


# --------------------------------------------------------------------------
# in-process: load_todo/save_todo/render_todo_md/todo_find/todo_finish/todo_entry, load_qa/save_qa
# --------------------------------------------------------------------------
class TodoQaInProcessTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gg = testenv.load_module()

    def setUp(self):
        gg = self.gg
        home = os.environ["HOME"]
        # the absolute rule: refuse to touch anything unless it is provably under the temp HOME
        for p in (gg.TODO_JSON, gg.QA_JSON, gg.todo_md_path(), gg.CACHE_DIR):
            self.assertTrue(os.path.realpath(p).startswith(os.path.realpath(home)),
                             f"{p!r} is not under the test HOME {home!r}; refusing to run")
        # fresh Graph per test (build_graph() re-reads the fixture cache; cheap) so mutating a node's
        # .summary in one test can never leak into another
        self.g = testenv.fixture_graph(gg)
        # clean slate: an earlier test's leftovers must not leak in (TODO_JSON/QA_JSON are shared with
        # every other in-process test in this process via env.load_module()'s single cached HOME)
        gg.save_todo([])
        gg.write_json(gg.QA_JSON, {})

    # -- todo_entry: item vs comment anchor -------------------------------------------------------
    def test_todo_entry_on_an_item_has_no_comment_fields(self):
        e = self.gg.todo_entry(self.g, "test/repo#1", "check this")
        self.assertEqual(e["repo"], "test/repo")
        self.assertEqual(e["item"], "test/repo#1")
        self.assertEqual(e["item_num"], "#1")
        self.assertEqual(e["title"], "Crash when the extent list is empty")
        self.assertEqual(e["note"], "check this")
        self.assertFalse(e["done"])
        for k in ("comment", "comment_url", "comment_author", "comment_when", "comment_text"):
            self.assertNotIn(k, e)

    def test_todo_entry_anchored_to_a_comment(self):
        e = self.gg.todo_entry(self.g, "test/repo#4/c101", "")
        # anchored under the comment's *item*, not the comment itself
        self.assertEqual(e["item"], "test/repo#4")
        self.assertEqual(e["item_num"], "#4")
        self.assertEqual(e["comment"], "test/repo#4/c101")
        self.assertEqual(e["comment_url"], "https://github.com/test/repo/issues/4#issuecomment-101")
        self.assertEqual(e["comment_author"], "dave")
        self.assertEqual(e["comment_when"], "+0d")  # comment and item share the same fixture date
        self.assertIn("cc @carol", e["comment_text"])  # excerpt() of the comment body (no summary yet)

    def test_todo_entry_comment_text_prefers_the_ai_summary_when_present(self):
        self.g.nodes["test/repo#4/c101"].summary = "manual summary here"
        e = self.gg.todo_entry(self.g, "test/repo#4/c101", "")
        self.assertEqual(e["comment_text"], "manual summary here")

    # -- save_todo / render_todo_md ----------------------------------------------------------------
    def test_save_todo_writes_json_source_of_truth(self):
        entries = [self.gg.todo_entry(self.g, "test/repo#1", "note1")]
        self.gg.save_todo(entries)
        self.assertEqual(self.gg.load_todo(), entries)

    def test_render_todo_md_groups_by_repo_marks_done_and_keeps_notes(self):
        e1 = self.gg.todo_entry(self.g, "test/repo#1", "line one\nline two")
        e2 = self.gg.todo_entry(self.g, "test/repo#4/c101", "")
        e2["done"] = True
        md = self.gg.render_todo_md([e1, e2])
        # the date column is the mark's own "created" timestamp (today, when todo_entry() ran here),
        # not the fixture item's date, so match everything after it rather than pin an exact string
        self.assertIn("## test/repo", md)
        self.assertRegex(md, r"- \[ \] \d{4}-\d{2}-\d{2} #1 Crash when the extent list is empty")
        self.assertRegex(md, r"- \[x\] \d{4}-\d{2}-\d{2} #4 Stale metadata after crash")
        self.assertIn("note: line one", md)
        self.assertIn("    line two", md)  # continuation lines of a multi-line note are indented further
        self.assertIn("comment by @dave +0d:", md)
        self.assertIn("https://github.com/test/repo/issues/4#issuecomment-101", md)

    def test_save_todo_rewrites_the_markdown_mirror_from_scratch(self):
        """todo.json is the source of truth: the .md mirror must reflect only the latest save_todo() call,
        not accumulate across calls."""
        e1 = self.gg.todo_entry(self.g, "test/repo#1", "first batch")
        self.gg.save_todo([e1])
        md_path = self.gg.todo_md_path()
        with open(md_path, encoding="utf-8") as f:
            first_md = f.read()
        self.assertIn("first batch", first_md)

        e2 = self.gg.todo_entry(self.g, "test/repo#2", "second batch")
        self.gg.save_todo([e2])
        with open(md_path, encoding="utf-8") as f:
            second_md = f.read()
        self.assertIn("second batch", second_md)
        self.assertNotIn("first batch", second_md)  # e1 is gone: full rewrite, not an append
        self.assertNotIn("#1 ", second_md)

    # -- todo_find -----------------------------------------------------------------------------------
    def test_todo_find_resolves_number_hash_owner_repo_and_comment_url(self):
        item_entry = self.gg.todo_entry(self.g, "test/repo#1", "")
        comment_entry = self.gg.todo_entry(self.g, "test/repo#4/c101", "")
        entries = [item_entry, comment_entry]
        for ref in ("1", "#1", "test/repo#1", "owner/name#1"):
            hits = self.gg.todo_find(entries, ref)
            self.assertEqual([e["id"] for e in hits], [item_entry["id"]], f"ref={ref!r}")
        hits = self.gg.todo_find(entries, comment_entry["comment_url"])
        self.assertEqual([e["id"] for e in hits], [comment_entry["id"]])

    def test_todo_find_matches_only_on_the_bare_number_suffix_ignoring_the_repo(self):
        """Suspected bug (gitgraph.py todo_find, ~line 5010-5022): the owner/repo qualifier in a ref like
        'other/lib#42' is never checked — todo_find only compares the numeric suffix, so an entry in a
        *different* repo with the same item number is also returned. Documented here as current, real
        behaviour (not fixed): both entries below share the fixture's #42 number across two repos."""
        primary = self.gg.todo_entry(self.g, "test/repo#1", "")
        primary["item"], primary["item_num"] = "test/repo#42", "#42"
        other = self.gg.todo_entry(self.g, "test/repo#1", "")
        other["item"], other["item_num"] = "other/lib#42", "lib#42"
        entries = [primary, other]
        for ref in ("42", "#42", "other/lib#42", "test/repo#42", "unrelated/repo#42"):
            ids = sorted(e["id"] for e in self.gg.todo_find(entries, ref))
            self.assertEqual(ids, sorted([primary["id"], other["id"]]),
                              f"ref={ref!r} unexpectedly resolved to only one repo's #42")

    def test_todo_find_skips_entries_already_marked_done(self):
        e = self.gg.todo_entry(self.g, "test/repo#1", "")
        e["done"] = True
        self.assertEqual(self.gg.todo_find([e], "1"), [])

    # -- todo_finish -----------------------------------------------------------------------------------
    def test_todo_finish_marks_done_without_removing(self):
        e = self.gg.todo_entry(self.g, "test/repo#1", "")
        self.gg.save_todo([e])
        msg = self.gg.todo_finish("1", remove=False)
        self.assertIn("marked done 1 entry", msg)
        saved = self.gg.load_todo()
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0]["done"])
        self.assertIn("done_at", saved[0])

    def test_todo_finish_remove_deletes_the_entry(self):
        e = self.gg.todo_entry(self.g, "test/repo#1", "")
        self.gg.save_todo([e])
        msg = self.gg.todo_finish("1", remove=True)
        self.assertIn("removed 1 entry", msg)
        self.assertEqual(self.gg.load_todo(), [])

    def test_todo_finish_no_match_reports_it_and_does_not_touch_the_file(self):
        e = self.gg.todo_entry(self.g, "test/repo#1", "")
        self.gg.save_todo([e])
        msg = self.gg.todo_finish("999", remove=False)
        self.assertEqual(msg, "nothing marked matches '999'")
        self.assertEqual(self.gg.load_todo(), [e])  # untouched

    def test_todo_finish_done_ref_cannot_be_finished_or_removed_again(self):
        """todo_find drops already-done entries, so calling todo_finish a second time on the same ref
        is a no-op (only `clear-done` purges done entries) — current, intentional-looking behaviour."""
        e = self.gg.todo_entry(self.g, "test/repo#1", "")
        self.gg.save_todo([e])
        self.gg.todo_finish("1", remove=False)
        msg = self.gg.todo_finish("1", remove=True)
        self.assertEqual(msg, "nothing marked matches '1'")
        saved = self.gg.load_todo()
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0]["done"])  # still there, still done — "remove" did not touch it

    # -- clear-done semantics (the part of `main()`'s `todo clear-done` branch that is a pure function
    # call: load_todo() filtered to the not-done entries, then save_todo()) ------------------------
    def test_clear_done_semantics_keep_open_drop_done(self):
        open_e = self.gg.todo_entry(self.g, "test/repo#1", "")
        done_e = self.gg.todo_entry(self.g, "test/repo#2", "")
        done_e["done"] = True
        self.gg.save_todo([open_e, done_e])
        kept = [e for e in self.gg.load_todo() if not e.get("done")]
        self.gg.save_todo(kept)
        self.assertEqual(self.gg.load_todo(), [open_e])

    # -- qa.json -----------------------------------------------------------------------------------
    def test_qa_anchored_to_an_item_and_read_back(self):
        self.gg.save_qa("test/repo#1", "why does this crash?", "empty extent list")
        qa = self.gg.load_qa()
        self.assertIn("test/repo#1", qa)
        self.assertEqual(len(qa["test/repo#1"]), 1)
        entry = qa["test/repo#1"][0]
        self.assertEqual(entry["q"], "why does this crash?")
        self.assertEqual(entry["a"], "empty extent list")
        self.assertIn("when", entry)

    def test_qa_anchored_to_a_comment_is_kept_separate_from_its_item(self):
        self.gg.save_qa("test/repo#4", "item-level question", "item-level answer")
        self.gg.save_qa("test/repo#4/c101", "comment-level question", "comment-level answer")
        qa = self.gg.load_qa()
        self.assertEqual([e["q"] for e in qa["test/repo#4"]], ["item-level question"])
        self.assertEqual([e["q"] for e in qa["test/repo#4/c101"]], ["comment-level question"])

    def test_qa_multiple_answers_for_the_same_node_all_accumulate(self):
        self.gg.save_qa("test/repo#1", "q1", "a1")
        self.gg.save_qa("test/repo#1", "q2", "a2")
        qa = self.gg.load_qa()
        self.assertEqual([e["q"] for e in qa["test/repo#1"]], ["q1", "q2"])


# --------------------------------------------------------------------------
# CLI: gg todo / gg todo done|remove N / gg todo clear-done, each on its own fresh HOME
# --------------------------------------------------------------------------
class TodoCliTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gg = testenv.load_module()
        cls.g = testenv.fixture_graph(cls.gg)

    def _home_with_entries(self, entries):
        home = testenv.make_home()
        cfg_dir = os.path.join(home, ".config", "gitgraph")
        os.makedirs(cfg_dir, exist_ok=True)
        todo_path = os.path.join(cfg_dir, "todo.json")
        self.assertTrue(os.path.realpath(todo_path).startswith(os.path.realpath(home)))
        with open(todo_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=1)
        return home, todo_path

    def test_cli_todo_with_nothing_marked(self):
        home = testenv.make_home()
        r = run_gg(home, "todo")
        self.assertEqual(r.returncode, 0)
        self.assertIn("nothing marked yet", r.stdout)
        self.assertIn(os.path.join(home, "gitgraph-todo.md"), r.stdout)

    def test_cli_todo_prints_markdown_and_footer_comment(self):
        # `gg todo` (bare) only lists todo.json -> render_todo_md(); it does not call save_todo(), so the
        # ~/gitgraph-todo.md mirror on disk is whatever an earlier `m`/done/remove/clear-done left behind
        # (nothing, here) — verified real behaviour, not assumed.
        entries = [self.gg.todo_entry(self.g, "test/repo#1", "check this")]
        home, _ = self._home_with_entries(entries)
        r = run_gg(home, "todo")
        self.assertEqual(r.returncode, 0)
        self.assertIn("# gg todo", r.stdout)
        self.assertIn("#1 Crash when the extent list is empty", r.stdout)
        self.assertIn("check this", r.stdout)
        md_path = os.path.join(home, "gitgraph-todo.md")
        self.assertIn(f"<!-- {md_path} -->", r.stdout)
        self.assertFalse(os.path.exists(md_path))  # bare `gg todo` never writes the mirror file

    def test_cli_todo_done_writes_the_markdown_mirror(self):
        # unlike bare `gg todo`, `todo done/remove/clear-done` call save_todo() -> the mirror IS written
        entries = [self.gg.todo_entry(self.g, "test/repo#1", "check this")]
        home, _ = self._home_with_entries(entries)
        r = run_gg(home, "todo", "done", "1")
        self.assertEqual(r.returncode, 0)
        md_path = os.path.join(home, "gitgraph-todo.md")
        self.assertTrue(os.path.exists(md_path))
        with open(md_path, encoding="utf-8") as f:
            self.assertIn("[x]", f.read())

    def test_cli_todo_done_marks_and_persists(self):
        entries = [self.gg.todo_entry(self.g, "test/repo#1", ""),
                   self.gg.todo_entry(self.g, "test/repo#2", "")]
        home, todo_path = self._home_with_entries(entries)
        r = run_gg(home, "todo", "done", "1")
        self.assertEqual(r.returncode, 0)
        self.assertIn("marked done 1 entry", r.stdout)
        with open(todo_path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertTrue(saved[0]["done"])
        self.assertFalse(saved[1]["done"])

    def test_cli_todo_done_missing_ref(self):
        entries = [self.gg.todo_entry(self.g, "test/repo#1", "")]
        home, _ = self._home_with_entries(entries)
        r = run_gg(home, "todo", "done", "999")
        self.assertEqual(r.returncode, 0)
        self.assertIn("nothing marked matches '999'", r.stdout)

    def test_cli_todo_done_needs_an_id(self):
        home, _ = self._home_with_entries([self.gg.todo_entry(self.g, "test/repo#1", "")])
        r = run_gg(home, "todo", "done")
        self.assertEqual(r.returncode, 2)
        self.assertIn("todo done needs an id", r.stderr)

    def test_cli_todo_remove_deletes_and_persists(self):
        entries = [self.gg.todo_entry(self.g, "test/repo#1", ""),
                   self.gg.todo_entry(self.g, "test/repo#2", "")]
        home, todo_path = self._home_with_entries(entries)
        r = run_gg(home, "todo", "remove", "#2")
        self.assertEqual(r.returncode, 0)
        self.assertIn("removed 1 entry", r.stdout)
        with open(todo_path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual([e["item"] for e in saved], ["test/repo#1"])

    def test_cli_todo_clear_done_drops_only_ticked_entries(self):
        open_e = self.gg.todo_entry(self.g, "test/repo#1", "")
        done_e = self.gg.todo_entry(self.g, "test/repo#2", "")
        done_e["done"] = True
        home, todo_path = self._home_with_entries([open_e, done_e])
        r = run_gg(home, "todo", "clear-done")
        self.assertEqual(r.returncode, 0)
        self.assertIn("kept 1 open entry", r.stdout)
        with open(todo_path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual([e["item"] for e in saved], ["test/repo#1"])


if __name__ == "__main__":
    unittest.main()
