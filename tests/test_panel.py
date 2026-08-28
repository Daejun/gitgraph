"""Unit tests for gg.Panel — the cursor/scroll/tab state each TUI panel owns (gitgraph.py:2647).

Panel is where every "the cursor jumped somewhere silly after the rows changed" bug would live, so the
rules are pinned here: which rows a cursor may land on (Row.kind in LIST_KINDS and non-blank text),
moving past decoration rows, the viewport following the cursor unless a wheel scroll set `free`, and
set_rows(keep=True) keeping the selection on the same node when the row list is rebuilt (which the TUI
does on every background AI result — see Tui.refresh_all_rows()).

No curses, no screen: Panel is pure state. rect is (y, x, h, w) of the content area; only h matters here.

Run: python3 -m unittest tests.test_panel -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as testenv  # noqa: E402

gg = testenv.load_module()


def rows(*specs):
    """specs: (text, nid, kind) or (text, nid, kind, jump) -> [Row]."""
    out = []
    for s in specs:
        text, nid, kind = s[0], s[1], s[2]
        r = gg.Row(text, nid, kind=kind)
        if len(s) > 3:
            r.jump = s[3]
        out.append(r)
    return out


def panel(rs, h=4, scroll_only=False):
    p = gg.Panel("test", "Test", scroll_only=scroll_only)
    p.rows = list(rs)
    p.rect = (0, 0, h, 40)
    return p


ITEM_ROWS = rows(("#1 first", "r#1", ""),
                 ("  ↳ note", None, "note"),          # decoration: not selectable
                 ("#2 second", "r#2", ""),
                 ("", None, ""),                      # blank: not selectable
                 ("#3 third", "r#3", ""))


class TestValid(unittest.TestCase):
    def test_list_kinds_are_selectable_and_others_are_not(self):
        p = panel(rows(("a", "n1", ""), ("b", "n2", "link"), ("c", "n3", "mention"),
                       ("d", "n4", "sec"), ("e", "n5", "note"), ("f", "n6", "head")))
        self.assertEqual([i for i in range(len(p.rows)) if p.valid(i)], [0, 1, 2, 3])

    def test_blank_text_is_never_selectable(self):
        p = panel(rows(("   ", "n1", ""), ("x", "n2", "")))
        self.assertFalse(p.valid(0))
        self.assertTrue(p.valid(1))

    def test_out_of_range_index_is_not_valid(self):
        p = panel(ITEM_ROWS)
        self.assertFalse(p.valid(-1))
        self.assertFalse(p.valid(len(p.rows)))


class TestMove(unittest.TestCase):
    def test_move_skips_decoration_and_blank_rows(self):
        p = panel(ITEM_ROWS)
        p.move(1)
        self.assertEqual(p.cur, 2)      # 1 is a note row
        p.move(1)
        self.assertEqual(p.cur, 4)      # 3 is blank

    def test_move_stops_at_the_last_selectable_row(self):
        p = panel(ITEM_ROWS)
        p.move(99)
        self.assertEqual(p.cur, 4)
        p.move(99)
        self.assertEqual(p.cur, 4)

    def test_move_backwards_stops_at_the_first(self):
        p = panel(ITEM_ROWS)
        p.cur = 4
        p.move(-99)
        self.assertEqual(p.cur, 0)

    def test_move_counts_selectable_rows_not_indexes(self):
        p = panel(ITEM_ROWS)
        p.move(2)
        self.assertEqual(p.cur, 4)      # two selectable steps: 0 -> 2 -> 4

    def test_move_clears_free(self):
        p = panel(ITEM_ROWS)
        p.free = True
        p.move(1)
        self.assertFalse(p.free)

    def test_scroll_only_panel_moves_the_viewport_not_a_cursor(self):
        p = panel(ITEM_ROWS, scroll_only=True)
        p.move(2)
        self.assertEqual((p.top, p.cur), (2, 0))
        p.move(-99)
        self.assertEqual(p.top, 0, "top never goes negative")


class TestSettle(unittest.TestCase):
    def test_cursor_is_pulled_onto_a_selectable_row(self):
        p = panel(ITEM_ROWS)
        p.cur = 1                       # a note row
        p.settle()
        self.assertEqual(p.cur, 2)      # forwards first

    def test_cursor_falls_back_to_a_previous_row_when_nothing_follows(self):
        p = panel(rows(("#1", "r#1", ""), ("  ↳ note", None, "note")))
        p.cur = 1
        p.settle()
        self.assertEqual(p.cur, 0)

    def test_viewport_follows_the_cursor_down_and_up(self):
        p = panel(ITEM_ROWS, h=2)
        p.cur = 4
        p.settle()
        self.assertEqual(p.top, 3)      # cur must be the last visible line
        p.cur = 0
        p.settle()
        self.assertEqual(p.top, 0)

    def test_free_keeps_the_view_where_a_wheel_scroll_left_it(self):
        p = panel(ITEM_ROWS, h=2)
        p.top, p.cur, p.free = 3, 0, True
        p.settle()
        self.assertEqual(p.top, 3, "a wheel scroll may leave the cursor off screen")

    def test_top_is_clamped_so_the_last_page_is_full(self):
        p = panel(ITEM_ROWS, h=3)
        p.top, p.free = 99, True        # free: the cursor must not drag the view back
        p.settle()
        self.assertEqual(p.top, len(p.rows) - 3)

    def test_empty_rows_and_zero_height_do_not_crash(self):
        p = panel([], h=0)
        p.settle()
        self.assertEqual((p.cur, p.top), (0, 0))
        p2 = panel(ITEM_ROWS, h=0)
        p2.cur = 4
        p2.settle()                     # h is clamped to 1 internally
        self.assertEqual(p2.top, 4)

    def test_settle_on_a_scroll_only_panel_clamps_top_only(self):
        p = panel(ITEM_ROWS, h=2, scroll_only=True)
        p.top, p.cur = 99, 0
        p.settle()
        self.assertEqual((p.top, p.cur), (3, 0))


class TestFindAndKeep(unittest.TestCase):
    def test_find_matches_only_selectable_item_kinds(self):
        p = panel(rows(("a", "n1", ""), ("b", "n1", "link"), ("c", "n1", "mention")))
        self.assertEqual(p.find("n1"), 0)
        p2 = panel(rows(("b", "n9", "link"),))
        self.assertIsNone(p2.find("n9"), "link rows are not jump targets for find()")

    def test_find_picks_the_hit_nearest_the_cursor(self):
        p = panel(rows(("a", "dup", ""), ("b", "x", ""), ("c", "dup", "")))
        p.cur = 2
        self.assertEqual(p.find("dup"), 2)
        p.cur = 0
        self.assertEqual(p.find("dup"), 0)
        self.assertEqual(p.find("dup", near=2), 2)

    def test_goto_nid_moves_the_cursor_and_reports_success(self):
        p = panel(ITEM_ROWS)
        self.assertTrue(p.goto_nid("r#3"))
        self.assertEqual(p.cur, 4)
        self.assertFalse(p.goto_nid("nope"))
        self.assertEqual(p.cur, 4, "a failed jump leaves the cursor alone")

    def test_set_rows_keeps_the_selection_on_the_same_node(self):
        p = panel(ITEM_ROWS)
        p.cur = 4                                   # #3
        p.set_rows(rows(("new", "new", ""), ("#3 third", "r#3", "")))
        self.assertEqual(p.cur, 1, "the same node, at its new index")

    def test_set_rows_keep_false_leaves_the_index_for_settle_to_fix(self):
        # set_rows() itself never moves the cursor when keep=False; settle() (called before every draw)
        # is what pulls a now-out-of-range index back into the new list.
        p = panel(ITEM_ROWS)
        p.cur = 4
        p.set_rows(rows(("#9", "r#9", "")), keep=False)
        self.assertEqual(p.cur, 4)
        p.settle()
        self.assertEqual(p.cur, 0)

    def test_set_rows_leaves_the_cursor_when_the_node_is_gone(self):
        p = panel(ITEM_ROWS)
        p.cur = 4
        p.set_rows(rows(("only", "other", "")))
        p.settle()
        self.assertEqual(p.cur, 0, "settle() clamps a stale index back into range")


if __name__ == "__main__":
    unittest.main()
