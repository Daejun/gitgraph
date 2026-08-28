"""The display contract: tree_rows / log_rows / overview_rows / focus_rows / render_show over the
fixture at a fixed width, plus segments() -- the (text, style) split every row goes through in both
frontends (ansi_rows() for the CLI, Tui.style_attr() for curses).

Golden files live in tests/golden/rows_*.txt. Regenerate with:

    GG_UPDATE_GOLDEN=1 python3 -m unittest tests.test_rows

overview_rows()'s head line embeds a wall-clock "(data fetched YYYY-MM-DD HH:MM)" (see gitgraph.py's
tr_note()/overview_rows() using g.fetched_at); it is normalised to a fixed placeholder before writing or
comparing so golden output stays byte-identical across runs and machines.

Run directly: python3 -m unittest tests.test_rows -v
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as testenv  # noqa: E402

REPO = testenv.FIXTURE_REPO  # "test/repo"
GOLDEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")
WIDTH = 80

_FETCHED_RE = re.compile(r"\(data fetched \d{4}-\d\d-\d\d \d\d:\d\d\)")


def qid(n):
    return f"{REPO}#{n}"


def normalize(text):
    """Strip the one piece of wall-clock output rows can contain (overview_rows()' fetched-at stamp)."""
    return _FETCHED_RE.sub("(data fetched TIMESTAMP)", text)


def assert_golden(test, name, text):
    """Compare `text` (already normalize()d by the caller) against tests/golden/rows_<name>.txt.
    With GG_UPDATE_GOLDEN=1 set, (re)writes the file instead of asserting."""
    path = os.path.join(GOLDEN_DIR, f"rows_{name}.txt")
    if os.environ.get("GG_UPDATE_GOLDEN"):
        os.makedirs(GOLDEN_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return
    with open(path, encoding="utf-8") as f:
        expected = f.read()
    test.assertEqual(text, expected, f"tests/golden/rows_{name}.txt is stale "
                                      f"(rerun with GG_UPDATE_GOLDEN=1 if this change is intended)")


def rows_text(rows):
    return "\n".join(normalize(r.text) for r in rows) + "\n"


class RowShapeMixin:
    """Shared structural assertions: every renderer must yield real Row objects whose nid (when set)
    names an actual node in the graph being rendered."""

    def assert_rows_shape(self, g, rows):
        Row = self.gg.Row
        for r in rows:
            self.assertIsInstance(r, Row)
            self.assertIsInstance(r.text, str)
            if r.nid is not None:
                self.assertIn(r.nid, g.nodes, f"Row.nid {r.nid!r} is not a node in this graph")
            if r.jump is not None:
                self.assertIn(r.jump, g.nodes, f"Row.jump {r.jump!r} is not a node in this graph")


class TestRowShapes(unittest.TestCase, RowShapeMixin):
    """Not golden: these just assert the display contract itself (Row type + valid nid/jump), so they
    stay meaningful even if the exact rendered text changes."""

    @classmethod
    def setUpClass(cls):
        cls.gg = testenv.load_module()
        cls.g = testenv.fixture_graph(cls.gg)
        cls.cg = cls.gg.apply_filters(cls.g, "linked", True, True)

    def _comps(self):
        comps = self.gg.components(self.cg)
        comps.sort(key=lambda c: (-sum(1 for i in c if self.cg.nodes[i].kind == "item"),
                                   -max(self.cg.nodes[i].time for i in c)))
        return comps

    def test_tree_rows(self):
        for comp in self._comps():
            root = self.gg.pick_root(self.cg, comp)
            rows = self.gg.tree_rows(self.cg, comp, root, WIDTH)
            self.assertTrue(rows)
            self.assert_rows_shape(self.cg, rows)

    def test_log_rows(self):
        comps = self._comps()
        for comp in comps:
            rows = self.gg.log_rows(self.cg, comp, WIDTH)
            self.assertTrue(rows)
            self.assert_rows_shape(self.cg, rows)
        # the big component (multiple lanes, a branch) produces connector rows; the small 2-node
        # component (a single lane, no branch) legitimately produces none -- checked on the big one only.
        big_rows = self.gg.log_rows(self.cg, comps[0], WIDTH)
        self.assertTrue(any(r.kind == "conn" for r in big_rows))

    def test_overview_rows(self):
        rows = self.gg.overview_rows(self.cg, "tree", WIDTH)
        self.assertTrue(rows)
        self.assert_rows_shape(self.cg, rows)
        self.assertEqual(rows[0].kind, "head")

    def test_focus_rows(self):
        rows = self.gg.focus_rows(self.cg, qid(5), "tree", WIDTH)
        self.assertTrue(rows)
        self.assert_rows_shape(self.cg, rows)
        self.assertEqual(rows[0].kind, "head")

    def test_render_show_returns_a_plain_string(self):
        text = self.gg.render_show(self.cg, qid(5), WIDTH)
        self.assertIsInstance(text, str)
        head = text.split("\n", 1)[0]
        self.assertIn("#5", head)
        self.assertIn(self.cg.nodes[qid(5)].title, head)


class TestGoldenRows(unittest.TestCase):
    """Golden text comparisons -- run via `python3 tests/run.py golden` (dotted id contains "golden")."""

    @classmethod
    def setUpClass(cls):
        cls.gg = testenv.load_module()
        cls.g = testenv.fixture_graph(cls.gg)
        cls.cg = cls.gg.apply_filters(cls.g, "linked", True, True)

    def _comps(self):
        comps = self.gg.components(self.cg)
        comps.sort(key=lambda c: (-sum(1 for i in c if self.cg.nodes[i].kind == "item"),
                                   -max(self.cg.nodes[i].time for i in c)))
        return comps

    def test_tree_rows_golden(self):
        comps = self._comps()
        root0 = self.gg.pick_root(self.cg, comps[0])
        root1 = self.gg.pick_root(self.cg, comps[1])
        assert_golden(self, "tree_comp1", rows_text(self.gg.tree_rows(self.cg, comps[0], root0, WIDTH)))
        assert_golden(self, "tree_comp2", rows_text(self.gg.tree_rows(self.cg, comps[1], root1, WIDTH)))

    def test_log_rows_golden(self):
        comps = self._comps()
        assert_golden(self, "log_comp1", rows_text(self.gg.log_rows(self.cg, comps[0], WIDTH)))

    def test_overview_rows_golden(self):
        rows = self.gg.overview_rows(self.cg, "tree", WIDTH)
        assert_golden(self, "overview_tree", rows_text(rows))

    def test_focus_rows_golden(self):
        rows = self.gg.focus_rows(self.cg, qid(5), "tree", WIDTH)
        assert_golden(self, "focus_tree_5", rows_text(rows))

    def test_render_show_golden(self):
        assert_golden(self, "show_pr5", normalize(self.gg.render_show(self.cg, qid(5), WIDTH)) + "\n")
        assert_golden(self, "show_comment_c201",
                       normalize(self.gg.render_show(self.cg, f"{qid(5)}/c201", WIDTH)) + "\n")
        assert_golden(self, "show_person_alice", normalize(self.gg.render_show(self.cg, "@alice", WIDTH)) + "\n")
        # a stub whose url is never filled in by resolve_stubs -> "(stub - not fetched)" branch
        assert_golden(self, "show_stub_lib42", normalize(self.gg.render_show(self.cg, "other/lib#42", WIDTH)) + "\n")

    def test_render_show_comments_section_has_a_doubled_arrow_bug(self):
        """Not a golden file -- pins a suspected bug directly. render_show()'s "comments" section
        (gitgraph.py ~2443-2445) prepends "  -> " to a `tg` list whose entries already start with their
        own EDGE_LABEL arrow (e.g. "-> refs #4"), producing a doubled arrow like "  -> -> refs #4" for any
        comment that both belongs to the shown item and links elsewhere. Asserted here as CURRENT
        behaviour; see this file's suite-level report for the suggested fix."""
        text = self.gg.render_show(self.cg, qid(6), WIDTH)
        line = next(l for l in text.splitlines() if "nit: consider reusing" in l)
        self.assertIn("\u2192 \u2192 refs #4", line)  # "→ → refs #4"


class TestSegments(unittest.TestCase):
    """segments(): the (text, style) split every Row goes through before ansi_rows()/Tui.put_row()."""

    @classmethod
    def setUpClass(cls):
        cls.gg = testenv.load_module()
        cls.g = testenv.fixture_graph(cls.gg)
        cls.cg = cls.gg.apply_filters(cls.g, "linked", True, True)

    def test_item_head_segments(self):
        # a plain (non-draft/merged/closed) PR head: date / "#N [PR] " / title / "  @author  (meta)"
        r = self.gg.Row("2024-03-01 #5 [PR] fix: rebuild extent list on mount  @alice  (1 comments, 1 linked)",
                         qid(5))
        self.assertEqual(self.gg.segments(r, self.cg), [
            ("2024-03-01 ", "meta"),
            ("#5 [PR] ", "pr"),
            ("fix: rebuild extent list on mount", ""),
            ("  @alice  (1 comments, 1 linked)", "meta"),
        ])

    def test_item_head_segments_style_varies_with_state(self):
        cases = {
            qid(3): "draft",    # [PR][draft]
            qid(6): "merged",   # [PR][merged]
            qid(4): "closed",   # [I][closed]
            qid(1): "issue",    # plain open issue
        }
        for nid, style in cases.items():
            n = self.cg.nodes[nid]
            text = self.gg.item_label(self.cg, n, WIDTH)
            r = self.gg.Row(text, nid)
            segs = self.gg.segments(r, self.cg)
            head_style = next(st for tx, st in segs if st in
                               ("issue", "pr", "draft", "merged", "closed"))
            self.assertEqual(head_style, style, nid)

    def test_comment_head_segments(self):
        r = self.gg.Row('+0d 01-10 o @dave "cc @carol, have you seen this on your rig?"', f"{qid(4)}/c101")
        segs = self.gg.segments(r, self.cg)
        self.assertEqual(segs, [
            ("+0d 01-10 ", "meta"),
            ("o @dave ", "comment"),
            ('"cc @carol, have you seen this on your rig?"', ""),
        ])

    def test_edge_label_segments_on_a_tree_row(self):
        r = self.gg.Row("├─ → closes 2024-01-10 #4 [I][closed] Stale metadata after crash  @dave  "
                         "(1 comments, 1 linked)", qid(4))
        segs = self.gg.segments(r, self.cg)
        self.assertEqual(segs[0], ("├─ ", "pre"))
        self.assertEqual(segs[1], ("→ closes ", "closes"))
        self.assertEqual(segs[2], ("2024-01-10 ", "meta"))
        self.assertEqual(segs[3], ("#4 [I][closed] ", "closed"))

    def test_cited_by_edge_label_style(self):
        r = self.gg.Row("└─ ← cited-by 2023-12-01 #7 [I][closed] 중복 이슈 (구버전)  @eve", qid(7))
        segs = self.gg.segments(r, self.cg)
        self.assertIn(("← cited-by ", "in"), segs)

    def test_url_row_indent_is_not_part_of_the_underlined_segment(self):
        """Regression for commit 0.10.1: the leading indent before a URL row must be its own
        unstyled segment, never merged into the "url" (underlined) segment."""
        r = self.gg.Row("  https://github.com/test/repo/pull/5", qid(5), kind="url")
        segs = self.gg.segments(r, self.cg)
        self.assertEqual(segs, [
            ("  ", ""),
            ("https://github.com/test/repo/pull/5", "url"),
        ])
        for text, style in segs:
            if style == "url":
                self.assertFalse(text[:1].isspace(), "leading whitespace leaked into the url segment")

    def test_url_row_with_no_indent_has_no_leading_empty_segment(self):
        r = self.gg.Row("https://github.com/test/repo/pull/5", qid(5), kind="url")
        segs = self.gg.segments(r, self.cg)
        self.assertEqual(segs, [("https://github.com/test/repo/pull/5", "url")])

    def test_link_row_segments_are_a_single_link_styled_segment(self):
        r = self.gg.Row("   ├─ mentions @alice", qid(1), "@alice", "link")
        self.assertEqual(self.gg.segments(r, self.cg), [("   ├─ mentions @alice", "link")])

    def test_head_row_segments(self):
        r = self.gg.Row("== [1] 7 items (5 open), 3 linked comments ==", kind="head")
        self.assertEqual(self.gg.segments(r, self.cg), [("== [1] 7 items (5 open), 3 linked comments ==", "head")])

    def test_conn_row_segments(self):
        r = self.gg.Row("|\\", None, None, "conn")
        self.assertEqual(self.gg.segments(r, self.cg), [("|\\", "link")])

    def test_person_row_segments(self):
        r = self.gg.Row("@alice  (2 mentions)", "@alice")
        self.assertEqual(self.gg.segments(r, self.cg), [("@alice  (2 mentions)", "person")])


if __name__ == "__main__":
    unittest.main()
