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
        self._old = (gg.CACHE_DIR, gg.claude_call, gg.review_call, gg.ai_available)
        gg.CACHE_DIR = os.path.join(self.tmp, ".cache", "gitgraph")
        os.makedirs(gg.CACHE_DIR)
        gg.ai_available = lambda: True
        self.calls = []
        self.addCleanup(self._restore)
        self.assertTrue(gg.CACHE_DIR.startswith(tempfile.gettempdir()))

    def _restore(self):
        gg.CACHE_DIR, gg.claude_call, gg.review_call, gg.ai_available = self._old
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
        # the review passes stream on the claude backend; both routes land on the same fake
        gg.review_call = (lambda prompt, model, phase, timeout, cwd, tools, on_event=None:
                          fake(prompt, model, phase, timeout=timeout, cwd=cwd, tools=tools))


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
        self.assertIn(rv.findings[0].line, rv.file("fs/f2fs/data.c").commentable("RIGHT"))

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



VERDICT = '<<<GG_VERDICT\n{"verdict": "%s", "reason": "%s"}\nGG_VERDICT>>>'


class TestVerifyPass(RunReviewCase):
    """Pass 2: one call per finding whose whole job is to disprove it."""

    def one(self, **kw):
        rv = review()
        f = gg.Finding(severity=kw.pop("severity", "bug"), path="fs/f2fs/data.c", line=221,
                       side="RIGHT", title="lock leak", body="b", evidence="e", **kw)
        rv.findings = [f]
        return rv, f

    def test_a_confirmed_verdict_lands_on_the_finding(self):
        self.answer(VERDICT % ("CONFIRMED", "out_unlock at data.c:224 does not drop it"))
        rv, f = self.one()
        gg.run_verify(rv)
        self.assertEqual(f.verdict, "CONFIRMED")
        self.assertIn("data.c:224", f.verdict_reason)
        self.assertEqual(rv.status, "done")

    def test_a_disproved_finding_goes_to_dropped_and_stays_there(self):
        self.answer(VERDICT % ("FALSE", "the caller holds i_lock at data.c:198"))
        rv, f = self.one()
        gg.run_verify(rv)
        self.assertEqual(f.verdict, "FALSE")
        self.assertEqual(gg._find_bucket(f), "dropped")
        _, _, dropped = gg.review_history(rv.repo, rv.number)
        self.assertIn(f.digest, dropped)
        later = review()
        later.findings = [gg.Finding(path="fs/f2fs/data.c", line=221, title="lock leak")]
        gg.apply_history(later)
        self.assertEqual(later.findings[0].verdict, "FALSE")

    def test_the_check_runs_in_the_worktree_and_sees_the_claim(self):
        self.answer(VERDICT % ("CONFIRMED", "r"))
        rv, f = self.one()
        gg.run_verify(rv)
        p = self.calls[0]["prompt"]
        self.assertEqual(self.calls[0]["cwd"], rv.worktree)
        self.assertEqual(self.calls[0]["phase"], "verify")
        for want in ("lock leak", "fs/f2fs/data.c", "evidence offered: e", "GG_VERDICT>>>",
                     "STEP 2 — argue as the author."):
            self.assertIn(want, p, want)

    def test_an_unusable_answer_leaves_it_plausible_rather_than_dropping_it(self):
        self.answer("I am not sure, it depends.")
        rv, f = self.one()
        gg.run_verify(rv)
        self.assertEqual(f.verdict, "PLAUSIBLE")
        self.assertIn("agreed form", f.verdict_reason)

    def test_a_crashing_check_does_not_drop_the_finding(self):
        self.answer(ValueError("claude: rate limited"))
        rv, f = self.one()
        gg.run_verify(rv)
        self.assertEqual(f.verdict, "PLAUSIBLE")
        self.assertIn("rate limited", f.verdict_reason)

    def test_a_corrected_line_is_re_anchored(self):
        self.answer('<<<GG_VERDICT\n{"verdict": "CONFIRMED", "reason": "r", "line": 999}\nGG_VERDICT>>>')
        rv, f = self.one()
        gg.run_verify(rv)
        self.assertIn(f.line, rv.file("fs/f2fs/data.c").commentable("RIGHT"))
        self.assertEqual(f.anchor, "moved")

    def test_an_already_settled_finding_is_not_checked_again(self):
        self.answer(VERDICT % ("CONFIRMED", "r"))
        rv, f = self.one(state="ignored")
        gg.run_verify(rv)
        self.assertEqual(self.calls, [])
        self.assertIsNone(f.verdict)

    def test_run_review_verifies_by_default_and_can_be_told_not_to(self):
        self.answer(block(GOOD), VERDICT % ("FALSE", "no"))
        rv = review()
        gg.run_review(rv)
        self.assertEqual(rv.findings[0].verdict, "FALSE")
        self.assertEqual(len(self.calls), 2)

        self.calls.clear()
        os.remove(gg.reviews_path("test/repo"))   # else the FALSE above is carried forward, as it should be
        self.answer(block(GOOD))
        rv2 = review()
        gg.run_review(rv2, verify=False)
        self.assertIsNone(rv2.findings[0].verdict)
        self.assertEqual(len(self.calls), 1)


class TestSubjectiveDiscipline(RunReviewCase):
    def subjective(self, n):
        rv = review()
        rv.findings = [gg.Finding(severity="style", path="fs/f2fs/data.c", line=221,
                                  title=f"remark {i}", evidence="e" if i < 2 else None)
                       for i in range(n)]
        return rv

    def test_at_most_three_remarks_survive(self):
        rv = self.subjective(6)
        gg.cap_subjective(rv)
        kept = [f for f in rv.findings if f.verdict != "FALSE"]
        self.assertEqual(len(kept), gg.SUBJECTIVE_CAP)
        self.assertTrue(all("cap of 3" in f.verdict_reason for f in rv.findings if f.verdict == "FALSE"))

    def test_the_ones_with_evidence_are_the_ones_kept(self):
        rv = self.subjective(6)
        gg.cap_subjective(rv)
        kept = [f.title for f in rv.findings if f.verdict != "FALSE"]
        self.assertIn("remark 0", kept)
        self.assertIn("remark 1", kept)

    def test_three_or_fewer_are_left_alone(self):
        rv = self.subjective(3)
        gg.cap_subjective(rv)
        self.assertEqual([f.verdict for f in rv.findings], [None, None, None])

    def test_defects_are_never_capped(self):
        rv = review()
        rv.findings = [gg.Finding(severity="bug", path="a", line=1, title=f"bug {i}") for i in range(6)]
        gg.cap_subjective(rv)
        self.assertEqual([f.verdict for f in rv.findings], [None] * 6)

    def test_remarks_are_held_back_while_a_confirmed_defect_stands(self):
        rv = review()
        rv.findings = [gg.Finding(severity="bug", path="fs/f2fs/data.c", line=221, title="real",
                                  verdict="CONFIRMED"),
                       gg.Finding(severity="style", path="fs/f2fs/data.c", line=221, title="remark")]
        self.assertTrue(gg.subjective_held(rv))
        shown = [r.text for r in gg.findings_rows(rv, "open", 40)]
        self.assertFalse(any("remark" in t for t in shown))
        self.assertTrue(any("held back" in t for t in shown))

    def test_a_merely_plausible_defect_does_not_hold_them_back(self):
        rv = review()
        rv.findings = [gg.Finding(severity="bug", path="fs/f2fs/data.c", line=221, title="maybe",
                                  verdict="PLAUSIBLE"),
                       gg.Finding(severity="style", path="fs/f2fs/data.c", line=221, title="remark")]
        self.assertFalse(gg.subjective_held(rv))


class TestPosting(RunReviewCase):
    """What leaves the machine, and what is only marked as having left it."""

    def review_with(self, *findings):
        rv = review()
        rv.pr_id = "PR_kwDO123"
        rv.findings = list(findings)
        gg.anchor_findings(rv)
        return rv

    def finding(self, **kw):
        kw.setdefault("severity", "bug")
        kw.setdefault("path", "fs/f2fs/data.c")
        kw.setdefault("line", 221)
        kw.setdefault("title", "lock leak")
        kw.setdefault("body", "out_unlock keeps i_lock.")
        return gg.Finding(**kw)

    def graphql_returns(self, result=None, error=None):
        self.sent = []

        def fake(query, variables=None, host=gg.DEFAULT_HOST, repo=None):
            self.sent.append({"query": query, "variables": variables, "host": host, "repo": repo})
            if error:
                raise gg.GhError(error)
            return result or {"addPullRequestReview": {"pullRequestReview": {"url": "https://x/1"}}}

        self._old_graphql = gg.graphql
        gg.graphql = fake
        self.addCleanup(lambda: setattr(gg, "graphql", self._old_graphql))

    # -- the text -----------------------------------------------------
    def test_a_comment_is_the_title_the_body_and_the_fix(self):
        body = gg.comment_body(self.finding(diff="--- a\n+++ b\n"))
        self.assertTrue(body.startswith("lock leak\n\nout_unlock keeps i_lock."))
        self.assertIn("```diff\n--- a\n+++ b\n```", body)

    def test_no_signature_unless_the_user_asked_for_one(self):
        self.assertNotIn("gitgraph", gg.comment_body(self.finding()).lower())
        self.assertNotIn("claude", gg.comment_body(self.finding()).lower())
        old = gg.REVIEW_SIGNATURE
        gg.REVIEW_SIGNATURE = "-- posted with gg"
        try:
            self.assertTrue(gg.comment_body(self.finding()).endswith("-- posted with gg"))
        finally:
            gg.REVIEW_SIGNATURE = old

    def test_the_preview_is_built_from_the_same_text_that_is_sent(self):
        rv = self.review_with(self.finding())
        payload = gg.post_payload(rv, rv.findings)
        preview = "\n".join(gg.post_preview(rv, rv.findings))
        self.assertIn(payload["threads"][0]["body"].splitlines()[0], preview)
        self.assertIn("fs/f2fs/data.c:221", preview)

    # -- the payload --------------------------------------------------
    def test_one_thread_per_finding_with_path_line_and_side(self):
        rv = self.review_with(self.finding(), self.finding(title="other", line=222))
        p = gg.post_payload(rv, rv.findings)
        self.assertEqual(p["pr"], "PR_kwDO123")
        self.assertIsNone(p["body"])
        self.assertEqual([(t["path"], t["line"], t["side"]) for t in p["threads"]],
                         [("fs/f2fs/data.c", 221, "RIGHT"), ("fs/f2fs/data.c", 222, "RIGHT")])

    def test_a_range_becomes_startline_to_line(self):
        rv = self.review_with(self.finding(line=221, end_line=222))
        t = gg.post_payload(rv, rv.findings)["threads"][0]
        self.assertEqual((t["startLine"], t["line"], t["startSide"], t["side"]), (221, 222, "RIGHT", "RIGHT"))

    # -- what may be posted -------------------------------------------
    def test_only_open_anchored_findings_are_offered(self):
        rv = self.review_with(
            self.finding(title="good"),
            self.finding(title="dropped", verdict="FALSE"),
            self.finding(title="ignored", state="ignored"),
            self.finding(title="already posted", state="posted"),
            self.finding(title="off the diff", path="nowhere.c"))
        self.assertEqual([f.title for f in gg.postable_findings(rv)], ["good"])

    def test_something_posted_under_an_earlier_head_is_not_offered_again(self):
        rv = self.review_with(self.finding(state="posted", thread_url="https://x/1"))
        gg.save_review(rv)
        again = self.review_with(self.finding())          # same digest, fresh state
        self.assertEqual(gg.postable_findings(again), [])

    def test_a_subset_can_be_named(self):
        a, b = self.finding(title="a"), self.finding(title="b", line=222)
        rv = self.review_with(a, b)
        self.assertEqual([f.title for f in gg.postable_findings(rv, {b.fid})], ["b"])

    # -- sending ------------------------------------------------------
    def test_a_successful_post_marks_them_and_remembers_the_url(self):
        self.graphql_returns()
        rv = self.review_with(self.finding())
        url, err = gg.post_findings(rv, rv.findings)
        self.assertIsNone(err)
        self.assertEqual(url, "https://x/1")
        self.assertEqual(rv.findings[0].state, "posted")
        self.assertEqual(rv.findings[0].thread_url, "https://x/1")
        posted, _, _ = gg.review_history(rv.repo, rv.number)
        self.assertIn(rv.findings[0].digest, posted)

    def test_the_mutation_says_which_repo_it_is_about(self):
        self.graphql_returns()
        rv = self.review_with(self.finding())
        gg.post_findings(rv, rv.findings)
        self.assertEqual(self.sent[0]["repo"], "test/repo")   # a mutation names no repository() itself
        self.assertIn("addPullRequestReview", self.sent[0]["query"])

    def test_a_failure_leaves_every_finding_untouched(self):
        self.graphql_returns(error="Pull request review thread line must be part of the diff")
        rv = self.review_with(self.finding())
        url, err = gg.post_findings(rv, rv.findings)
        self.assertIsNone(url)
        self.assertIn("part of the diff", err)
        self.assertEqual(rv.findings[0].state, "new")
        posted, _, _ = gg.review_history(rv.repo, rv.number)
        self.assertEqual(posted, {})

    def test_a_dry_run_sends_nothing(self):
        self.graphql_returns()
        rv = self.review_with(self.finding())
        gg.post_findings(rv, rv.findings, dry_run=True)
        self.assertEqual(self.sent, [])
        self.assertEqual(rv.findings[0].state, "new")

    def test_posting_nothing_is_refused_not_silently_ignored(self):
        self.graphql_returns()
        rv = self.review_with(self.finding())
        self.assertEqual(gg.post_findings(rv, []), (None, "nothing to post"))
        self.assertEqual(self.sent, [])

    def test_a_missing_pr_id_is_refused(self):
        self.graphql_returns()
        rv = self.review_with(self.finding())
        rv.pr_id = None
        url, err = gg.post_findings(rv, rv.findings)
        self.assertIn("pull request id", err)
        self.assertEqual(self.sent, [])


class TestIncremental(RunReviewCase):
    """A push invalidates the part of a review the new commits touched, not the whole of it."""

    def stored(self, head, files=("fs/f2fs/data.c", "fs/f2fs/gc.c"), merge_base="mb", created=1):
        rv = review()
        rv.head_oid, rv.merge_base, rv.created = head, merge_base, created
        rv.changes = [gg.Change(f"CHANGE-{i}", "locking", p, "fn", "s") for i, p in enumerate(files, 1)]
        rv.findings = [gg.Finding(severity="bug", path=p, line=221 if "data" in p else 88,
                                  side="RIGHT", title=f"finding in {p}", verdict="CONFIRMED",
                                  verdict_reason="checked") for p in files]
        rv.reachability = {"verdict": "confirmed", "reason": "r"}
        gg.save_review(rv)
        return rv

    def now(self, head="new", merge_base="mb", moved="fs/f2fs/gc.c", prev_alive=True):
        rv = review()
        rv.head_oid, rv.merge_base, rv.clone = head, merge_base, "/nonexistent-clone"
        self._old_moved = gg.head_moved_files
        gg.head_moved_files = lambda clone, prev, ref: ({moved} if moved else set()) if prev_alive else None
        self.addCleanup(lambda: setattr(gg, "head_moved_files", self._old_moved))
        return rv

    def test_only_the_files_the_new_commits_touched_are_redone(self):
        self.stored("old")
        rv = self.now()
        plan = gg.incremental_plan(rv)
        self.assertEqual(plan["prev_oid"], "old")
        self.assertEqual(plan["redo"], ["fs/f2fs/gc.c"])
        self.assertEqual([f.path for f in plan["carry"]], ["fs/f2fs/data.c"])
        self.assertEqual([c.path for c in plan["changes"]], ["fs/f2fs/data.c"])

    def test_a_carried_finding_keeps_its_verdict(self):
        self.stored("old")
        plan = gg.incremental_plan(self.now())
        self.assertEqual(plan["carry"][0].verdict, "CONFIRMED")
        self.assertEqual(plan["carry"][0].verdict_reason, "checked")

    def test_a_rebase_starts_again(self):
        self.stored("old", merge_base="mb")
        self.assertIsNone(gg.incremental_plan(self.now(merge_base="a different base")))

    def test_a_pruned_previous_head_starts_again(self):
        self.stored("old")
        self.assertIsNone(gg.incremental_plan(self.now(prev_alive=False)))

    def test_no_earlier_review_means_no_plan(self):
        self.assertIsNone(gg.incremental_plan(self.now()))

    def test_when_every_file_moved_there_is_nothing_to_carry(self):
        self.stored("old")
        rv = self.now()
        gg.head_moved_files = lambda clone, prev, ref: {"fs/f2fs/data.c", "fs/f2fs/gc.c"}
        self.assertIsNone(gg.incremental_plan(rv))

    def test_two_heads_are_kept_and_no_more(self):
        for i, head in enumerate(("h1", "h2", "h3"), 1):
            self.stored(head, created=i)
        stored = (gg.load_reviews("test/repo")["12"]["reviews"])
        self.assertEqual(sorted(stored), ["h2", "h3"])

    def test_the_review_only_calls_the_cli_for_the_changed_files(self):
        self.stored("old")
        rv = self.now()
        rv.merge_base = "mb"
        plan = gg.incremental_plan(rv)
        self.answer(block('{"findings": [{"severity": "bug", "path": "fs/f2fs/gc.c", "line": 88,'
                          ' "side": "RIGHT", "title": "new one", "evidence": "e"}]}'),
                    VERDICT % ("CONFIRMED", "r"))
        gg.run_review(rv, plan=plan)
        self.assertEqual(len(self.calls), 2)          # one review call, one check of the new finding
        self.assertIn("b/fs/f2fs/gc.c", self.calls[0]["prompt"])
        self.assertNotIn("b/fs/f2fs/data.c", self.calls[0]["prompt"])
        titles = sorted(f.title for f in rv.findings)
        self.assertEqual(titles, ["finding in fs/f2fs/data.c", "new one"])

    def test_a_carried_finding_is_not_checked_again(self):
        self.stored("old")
        rv = self.now()
        plan = gg.incremental_plan(rv)
        self.answer(block('{"findings": []}'))
        gg.run_review(rv, plan=plan)
        self.assertEqual(len(self.calls), 1)          # nothing new to check
        self.assertEqual(rv.findings[0].verdict, "CONFIRMED")

    def test_chunks_can_be_narrowed_to_a_subset(self):
        rv = review()
        self.assertEqual(gg.review_chunks(rv, ["fs/f2fs/gc.c"]), [["fs/f2fs/gc.c"]])
        self.assertEqual(gg.review_chunks(rv, []), [])


class TestReviewCommand(unittest.TestCase):
    """review_cmd swaps gg's protocol for a domain skill — per repo, and claude only."""

    def setUp(self):
        self._old = (dict(gg.CONFIG), gg.ai_backend)
        gg.ai_backend = lambda *a: "claude"
        self.addCleanup(self._restore)

    def _restore(self):
        gg.CONFIG.clear()
        gg.CONFIG.update(self._old[0])
        gg.ai_backend = self._old[1]

    def set(self, spec):
        gg.CONFIG["review_cmd"] = spec

    def test_empty_means_the_builtin_protocol(self):
        self.assertEqual(gg.parse_review_cmd(""), ([], ""))
        self.set("")
        self.assertEqual(gg.review_cmd_for("any/repo"), "")

    def test_a_bare_command_applies_everywhere(self):
        self.set("/kreview")
        self.assertEqual(gg.review_cmd_for("a/b"), "/kreview")
        self.assertEqual(gg.review_cmd_for("ghe.example.com/c/d"), "/kreview")

    def test_per_repo_rules_win_over_the_default(self):
        self.set("torvalds/linux=/kreview, other/*=/review-pr, /code-review")
        self.assertEqual(gg.review_cmd_for("torvalds/linux"), "/kreview")
        self.assertEqual(gg.review_cmd_for("other/thing"), "/review-pr")
        self.assertEqual(gg.review_cmd_for("someone/else"), "/code-review")

    def test_an_enterprise_repo_matches_its_owner_name_too(self):
        self.set("team/proj=/kreview")
        self.assertEqual(gg.review_cmd_for("ghe.example.com/team/proj"), "/kreview")

    def test_no_default_means_the_builtin_for_everything_unmatched(self):
        self.set("torvalds/*=/kreview")
        self.assertEqual(gg.review_cmd_for("torvalds/linux"), "/kreview")
        self.assertEqual(gg.review_cmd_for("me/mine"), "")

    def test_only_claude_has_slash_commands(self):
        self.set("/kreview")
        gg.ai_backend = lambda *a: "gemini"
        self.assertEqual(gg.review_cmd_for("a/b"), "")

    def test_the_prompt_hands_the_skill_the_range_and_still_appends_the_contract(self):
        rv = review()
        rv.merge_base, rv.head_oid = "base123", "head456"
        p = gg.review_prompt(rv, ["fs/f2fs/data.c"], cmd="/kreview")
        self.assertTrue(p.startswith("/kreview base123..head456"))
        self.assertIn("GG_REVIEW>>>", p)
        self.assertIn("b/fs/f2fs/data.c", p)
        self.assertNotIn("STEP 2 — split the change up.", p)   # the skill brings its own method


class TestReviewCommandRun(RunReviewCase):
    def setUp(self):
        super().setUp()
        self._old_backend, self._old_cfg = gg.ai_backend, dict(gg.CONFIG)
        gg.ai_backend = lambda *a: "claude"
        gg.CONFIG["review_cmd"] = "/kreview"
        self.addCleanup(self._restore_cmd)

    def _restore_cmd(self):
        gg.ai_backend = self._old_backend
        gg.CONFIG.clear()
        gg.CONFIG.update(self._old_cfg)

    def test_the_command_is_used_and_recorded(self):
        self.answer(block(GOOD), VERDICT % ("CONFIRMED", "r"))
        rv = review()
        gg.run_review(rv)
        self.assertTrue(self.calls[0]["prompt"].startswith("/kreview "))
        self.assertEqual(rv.engine, "claude:/kreview")

    def test_a_borrowed_skill_may_write_its_report_inside_the_worktree(self):
        self.answer(block(GOOD), VERDICT % ("CONFIRMED", "r"))
        gg.run_review(review())
        self.assertIn("Write", self.calls[0]["tools"])

    def test_the_builtin_protocol_stays_read_only(self):
        gg.CONFIG["review_cmd"] = ""
        self.answer(block(GOOD), VERDICT % ("CONFIRMED", "r"))
        gg.run_review(review())
        self.assertNotIn("Write", self.calls[0]["tools"])

    def test_a_skill_that_ignores_the_contract_falls_back_once(self):
        self.answer("I wrote review-inline.txt, have a look.", block(GOOD), VERDICT % ("CONFIRMED", "r"))
        rv = review()
        gg.run_review(rv)
        self.assertEqual([f.title for f in rv.findings], ["lock leak"])
        self.assertIn("fell back from /kreview", rv.engine)
        self.assertTrue(self.calls[0]["prompt"].startswith("/kreview "))
        self.assertFalse(self.calls[1]["prompt"].startswith("/kreview "))

    def test_when_both_fail_it_says_so_instead_of_reporting_nothing(self):
        self.answer("no json anywhere")
        rv = review()
        gg.run_review(rv)
        self.assertEqual(rv.status, "failed")
        self.assertIn("/kreview did not answer in the agreed form", rv.error)

    def test_the_check_pass_always_uses_gg_own_discipline(self):
        self.answer(block(GOOD), VERDICT % ("FALSE", "no"))
        gg.run_review(review())
        verify = [c for c in self.calls if c["phase"] == "verify"]
        self.assertTrue(verify)
        self.assertFalse(verify[0]["prompt"].startswith("/kreview"))
        self.assertIn("STEP 2 — argue as the author.", verify[0]["prompt"])

if __name__ == "__main__":
    unittest.main()
