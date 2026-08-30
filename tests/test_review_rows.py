"""The display contract of review mode: review_files_rows / diff_rows / findings_rows / changes_rows /
threads_rows, and the styles segments() gives them. A review is built here from a literal diff and
literal findings — no git, no gh, no worktree — so these are pure rendering tests.

Golden files live in tests/golden/rows_review_*.txt. Regenerate with:

    GG_UPDATE_GOLDEN=1 python3 -m unittest tests.test_review_rows

Run directly: python3 -m unittest tests.test_review_rows -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as testenv  # noqa: E402
from test_rows import assert_golden, rows_text  # noqa: E402

gg = testenv.load_module()

DIFF = """\
diff --git a/fs/f2fs/data.c b/fs/f2fs/data.c
--- a/fs/f2fs/data.c
+++ b/fs/f2fs/data.c
@@ -220,6 +220,7 @@ static int f2fs_write_page(struct page *page)
 	if (!page)
-		return -ENOMEM;
+		goto out_unlock;
+
 	spin_lock(&sbi->lock);
 out_unlock:
 	spin_unlock(&sbi->lock);
 	return 0;
diff --git a/fs/f2fs/gc.c b/fs/f2fs/gc.c
--- a/fs/f2fs/gc.c
+++ b/fs/f2fs/gc.c
@@ -88,2 +88,2 @@ gc_thread_func
-	unsigned int segno = 0;   /* 시작 segment */
+	unsigned int start_segment_number = 0;
 	wait_event(sbi->gc_wait, kthread_should_stop());
diff --git a/include/f2fs.h b/include/f2fs.h
new file mode 100644
--- /dev/null
+++ b/include/f2fs.h
@@ -0,0 +1,2 @@
+#define F2FS_ZONE 1
+#define F2FS_MAX 2
"""

WIDTH_FILES, WIDTH_DIFF, WIDTH_FIND = 22, 56, 30


def review(findings=(), changes=(), threads=(), **kw):
    rv = gg.Review("test/repo", 12, title="f2fs: allocate on zone boundaries",
                   state="OPEN", author="alice", head_oid="abc1234def5678", url="https://x/pull/12",
                   head_ref="topic", base_ref="main", worktree="/tmp/wt", wt_size=1234567,
                   engine="builtin" if findings else None, **kw)
    rv.files = gg.parse_unified_diff(DIFF)
    rv.findings = list(findings)
    rv.changes = list(changes)
    rv.threads = list(threads)
    gg.anchor_findings(rv)
    return rv


def findings():
    return [gg.Finding(severity="bug", path="fs/f2fs/data.c", line=221, side="RIGHT",
                       title="lock leak on the out_unlock path", verdict="CONFIRMED",
                       body="out_unlock does not release i_lock.", evidence="data.c:224"),
            gg.Finding(severity="logic", path="fs/f2fs/gc.c", line=88, side="RIGHT",
                       title="renaming segno widens every caller line", verdict="PLAUSIBLE"),
            gg.Finding(severity="style", path="include/f2fs.h", line=1, side="RIGHT",
                       title="F2FS_MAX has no user in this series"),
            gg.Finding(severity="regress", path="fs/f2fs/gc.c", line=999, side="RIGHT",
                       title="a finding the diff does not reach"),
            gg.Finding(severity="bug", path="fs/f2fs/data.c", line=222, side="RIGHT",
                       title="already reported upstream", verdict="FALSE",
                       verdict_reason="the caller holds it"),
            gg.Finding(severity="design", path="fs/f2fs/data.c", line=221, side="RIGHT",
                       title="posted one", state="posted", thread_url="https://x/1")]


class TestReviewRowContract(unittest.TestCase):
    """Structure, not text: every row is a Row, and its ids are the ones the TUI navigates by."""

    def test_file_rows_name_their_file(self):
        rv = review()
        rows = gg.review_files_rows(rv, WIDTH_FILES, sel="fs/f2fs/gc.c")
        ids = [r.nid for r in rows if r.nid]
        self.assertEqual(ids, ["file:fs/f2fs/data.c", "file:fs/f2fs/gc.c", "file:include/f2fs.h"])
        self.assertTrue(all(isinstance(r, gg.Row) for r in rows))

    def test_the_selected_file_is_marked(self):
        rows = gg.review_files_rows(review(), WIDTH_FILES, sel="fs/f2fs/gc.c")
        picked = [r.text for r in rows if r.nid == "file:fs/f2fs/gc.c"]
        self.assertTrue(picked[0].startswith("▸"), picked)

    def test_file_rows_fit_the_panel_width(self):
        for w in (20, 22, 30, 40):
            for r in gg.review_files_rows(review(findings()), w):
                self.assertLessEqual(gg.dw(r.text), w, (w, r.text))

    def test_diff_rows_carry_side_and_line(self):
        rows = gg.diff_rows(review(), "fs/f2fs/data.c", WIDTH_DIFF)
        ids = [r.nid for r in rows if r.kind in ("diff_add", "diff_del")]
        self.assertIn("line:fs/f2fs/data.c:LEFT:221", ids)
        self.assertIn("line:fs/f2fs/data.c:RIGHT:221", ids)
        self.assertIn("line:fs/f2fs/data.c:RIGHT:222", ids)

    def test_diff_rows_fit_the_panel_width(self):
        for w in (40, 56, 100):
            for r in gg.diff_rows(review(findings()), "fs/f2fs/gc.c", w):
                self.assertLessEqual(gg.dw(r.text), w, (w, r.text))

    def test_a_finding_flags_its_line_in_the_diff(self):
        rows = gg.diff_rows(review(findings()), "fs/f2fs/data.c", WIDTH_DIFF)
        flagged = [r.text for r in rows if r.nid == "line:fs/f2fs/data.c:RIGHT:221"]
        self.assertTrue(flagged[0].startswith("⚠"), flagged)

    def test_a_dropped_or_ignored_finding_does_not_flag_the_diff(self):
        f = gg.Finding(severity="bug", path="fs/f2fs/data.c", line=221, side="RIGHT",
                       title="x", verdict="FALSE")
        rows = gg.diff_rows(review([f]), "fs/f2fs/data.c", WIDTH_DIFF)
        flagged = [r.text for r in rows if r.nid == "line:fs/f2fs/data.c:RIGHT:221"]
        self.assertFalse(flagged[0].startswith("⚠"), flagged)

    def test_hunks_fold(self):
        rv = review()
        full = gg.diff_rows(rv, "fs/f2fs/data.c", WIDTH_DIFF)
        hunk = next(r.nid for r in full if r.kind == "diff_hunk")
        folded = gg.diff_rows(rv, "fs/f2fs/data.c", WIDTH_DIFF, collapsed={hunk})
        self.assertEqual([r.kind for r in folded], ["diff_hunk"])
        self.assertTrue(folded[0].text.startswith("▸"))
        self.assertTrue(full[0].text.startswith("▾"))

    def test_a_binary_file_says_so_instead_of_drawing_nothing(self):
        rv = review()
        rv.files = gg.parse_unified_diff("diff --git a/x.png b/x.png\nBinary files a/x.png and b/x.png differ\n")
        self.assertIn("binary", gg.diff_rows(rv, "x.png", WIDTH_DIFF)[0].text)

    def test_tabs_become_columns_not_a_single_space(self):
        self.assertEqual(gg.expand_tabs("a\tb"), "a   b")
        self.assertEqual(gg.expand_tabs("\tx"), "    x")
        self.assertEqual(gg.expand_tabs("한\tx"), "한  x")     # a wide char is two columns

    def test_finding_rows_name_the_finding_and_jump_to_its_line(self):
        rv = review(findings())
        rows = gg.findings_rows(rv, "open", WIDTH_FIND)
        picked = [r for r in rows if r.kind.startswith("sev_")]
        self.assertTrue(picked)
        for r in picked:
            self.assertTrue(r.nid.startswith("finding:"), r.nid)
            self.assertTrue(r.jump.startswith("line:"), r.jump)

    def test_findings_are_bucketed_by_state(self):
        rv = review(findings())
        titles = lambda tab: [r.text for r in gg.findings_rows(rv, tab, WIDTH_FIND)]  # noqa: E731
        self.assertTrue(any("posted one" in t for t in titles("posted")))
        self.assertTrue(any("already reported" in t for t in titles("dropped")))
        self.assertTrue(any("nothing ignored" in t for t in titles("ignored")))

    def test_a_moved_finding_shows_both_line_numbers(self):
        rv = review(findings())
        bad = next(f for f in rv.findings if f.title.startswith("a finding the diff"))
        self.assertEqual(bad.anchor, "moved")           # gc.c has a commentable line, it is pulled onto it
        self.assertEqual(bad.claimed_line, 999)         # where the reviewer actually pointed
        rows = gg.findings_rows(rv, "open", WIDTH_FIND)
        self.assertTrue(any("999→" in r.text for r in rows if r.kind == "note"))
        self.assertFalse(any("⚠" in r.text for r in rows if r.kind == "note"))   # ⚠ means severity

    def test_a_moved_comment_says_where_the_finding_actually_points(self):
        rv = review(findings())
        bad = next(f for f in rv.findings if f.anchor == "moved")
        self.assertIn(f"The code in question is at {bad.path}:{bad.claimed_line}",
                      gg.comment_body(bad))
        ok = next(f for f in rv.findings if f.anchor == "ok")
        self.assertNotIn("The code in question", gg.comment_body(ok))

    def test_subjective_findings_are_held_while_a_confirmed_defect_stands(self):
        rv = review(findings())
        self.assertTrue(gg.subjective_held(rv))
        rows = gg.findings_rows(rv, "open", WIDTH_FIND)
        self.assertFalse(any("F2FS_MAX" in r.text for r in rows))
        self.assertTrue(any("held back" in r.text for r in rows))

    def test_without_a_confirmed_defect_they_are_shown(self):
        subj = [f for f in findings() if f.severity in ("style", "design") and f.state != "posted"]
        rv = review(subj)
        self.assertFalse(gg.subjective_held(rv))
        rows = gg.findings_rows(rv, "open", WIDTH_FIND)
        self.assertTrue(any("F2FS_MAX" in r.text for r in rows))

    def test_an_unreviewed_pr_says_so(self):
        rows = gg.findings_rows(review(), "open", WIDTH_FIND)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].kind, "head")

    def test_a_failed_review_shows_the_error(self):
        rv = review()
        rv.status, rv.error = "failed", "no local clone of test/repo found"
        self.assertIn("no local clone", "\n".join(r.text for r in gg.findings_rows(rv, "open", WIDTH_FIND)))

    def test_changes_and_threads_tabs(self):
        rv = review(findings(),
                    changes=[gg.Change("CHANGE-1", "locking", "fs/f2fs/data.c", "f2fs_write_page", "error path")],
                    threads=[{"id": "T1", "path": "fs/f2fs/gc.c", "line": 88, "side": "RIGHT",
                              "resolved": False, "outdated": False,
                              "comments": [{"author": "bob", "body": "why rename this?"}]}])
        rv.reachability = {"verdict": "confirmed", "reason": "reached from write_begin"}
        ch = gg.findings_rows(rv, "changes", WIDTH_FIND)
        self.assertTrue(any("CHANGE-1" in r.text for r in ch))
        self.assertTrue(any("reachability: confirmed" in r.text for r in ch))
        th = gg.findings_rows(rv, "github", WIDTH_FIND)
        self.assertTrue(any("@bob" in r.text for r in th))
        self.assertEqual([r.nid for r in th if r.nid][0], "thread:T1")


class TestVerifyVisibility(unittest.TestCase):
    """#779 ran without the disprove pass and nothing on screen said so — the changes-tab header and
    the verdict marks now do. A review that skipped verification is not the same product."""

    def test_the_changes_tab_says_verify_off(self):
        rv = review(findings())
        rv.verify = False
        head = gg.findings_rows(rv, "changes", 60)[0].text
        self.assertIn("verify off", head)
        rv.verify = True
        self.assertNotIn("verify off", gg.findings_rows(rv, "changes", 60)[0].text)

    def test_unverified_findings_carry_a_dash_not_a_blank(self):
        rv = review(findings())
        rv.verify = False
        for f in rv.findings:
            f.verdict = None
        rows = [r.text for r in gg.findings_rows(rv, "open", 40) if r.kind.startswith("sev_")]
        self.assertTrue(rows and all(" - " in t for t in rows), rows)

    def test_a_verified_review_uses_the_verdict_marks(self):
        rv = review(findings())
        rv.verify = True
        rows = [r.text for r in gg.findings_rows(rv, "open", 40) if r.kind.startswith("sev_")]
        self.assertTrue(any(" ✓ " in t for t in rows))
        self.assertFalse(any(" - " in t for t in rows))


class TestReviewSegments(unittest.TestCase):
    """Review rows must colour without a graph lookup: their ids are not nodes."""

    def setUp(self):
        self.g = gg.Graph("test/repo")

    def styles(self, rows):
        return [st for r in rows for _, st in gg.segments(r, self.g)]

    def test_diff_rows_get_diff_styles(self):
        rows = gg.diff_rows(review(), "fs/f2fs/data.c", WIDTH_DIFF)
        got = {r.kind: self.styles([r])[0] for r in rows if r.kind}
        self.assertEqual(got.get("diff_add"), "diff_add")
        self.assertEqual(got.get("diff_del"), "diff_del")
        self.assertEqual(got.get("diff_ctx"), "diff_ctx")
        self.assertEqual(got.get("diff_hunk"), "diff_hunk")

    def test_a_flagged_diff_line_colours_its_marker_separately(self):
        rows = gg.diff_rows(review(findings()), "fs/f2fs/data.c", WIDTH_DIFF)
        row = next(r for r in rows if r.nid == "line:fs/f2fs/data.c:RIGHT:221")
        segs = gg.segments(row, self.g)
        self.assertEqual(segs[0][1], "sev_bug")
        self.assertEqual(segs[1][1], "diff_add")
        self.assertEqual("".join(t for t, _ in segs), row.text)

    def test_finding_rows_get_their_severity_style(self):
        rows = gg.findings_rows(review(findings()), "open", WIDTH_FIND)
        for r in rows:
            if r.kind.startswith("sev_"):
                self.assertEqual(gg.segments(r, self.g), [(r.text, r.kind)])

    def test_every_review_style_exists_in_every_theme(self):
        want = set(gg.DIFF_KINDS) | {f"sev_{s}" for s in gg.SEVERITIES}
        for name, theme in gg.THEMES.items():
            self.assertEqual(want - set(theme), set(), name)

    def test_the_cursor_can_land_on_diff_and_finding_rows(self):
        for kind in gg.DIFF_KINDS:
            self.assertIn(kind, gg.LIST_KINDS)
        for sev in gg.SEVERITIES:
            self.assertIn(f"sev_{sev}", gg.LIST_KINDS)

    def test_a_panel_can_jump_onto_a_diff_line(self):
        p = gg.Panel("rdiff", "Diff")
        p.rows = gg.diff_rows(review(), "fs/f2fs/data.c", WIDTH_DIFF)
        self.assertTrue(p.goto_nid("line:fs/f2fs/data.c:RIGHT:222"))
        self.assertEqual(p.current().nid, "line:fs/f2fs/data.c:RIGHT:222")


class TestReviewRowsGolden(unittest.TestCase):
    def test_files_rows_golden(self):
        assert_golden(self, "review_files", rows_text(
            gg.review_files_rows(review(findings()), WIDTH_FILES, sel="fs/f2fs/data.c")))

    def test_diff_rows_golden(self):
        assert_golden(self, "review_diff", rows_text(
            gg.diff_rows(review(findings()), "fs/f2fs/data.c", WIDTH_DIFF)))

    def test_diff_rows_cjk_golden(self):
        assert_golden(self, "review_diff_cjk", rows_text(
            gg.diff_rows(review(findings()), "fs/f2fs/gc.c", WIDTH_DIFF)))

    def test_findings_rows_golden(self):
        assert_golden(self, "review_findings", rows_text(
            gg.findings_rows(review(findings()), "open", WIDTH_FIND)))


if __name__ == "__main__":
    unittest.main()
