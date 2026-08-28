"""Unit tests for the markdown rendering used by the TUI's main panel: render_table() (markdown tables
-> aligned box lines, added in 0.11.0), render_markdown() (prose wrapped to the panel width, tables left
unwrapped so they scroll sideways with H/L) and md_segments() (one line -> (text, style) pairs, the half
of the display contract that gives markdown its colour).

Every assertion below was written by running the function first and recording what it actually returns.

Run: python3 -m unittest tests.test_markdown -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as testenv  # noqa: E402

gg = testenv.load_module()


class TestRenderTable(unittest.TestCase):
    def test_header_rule_and_right_alignment(self):
        out = gg.render_table(["| a | bbb |", "|---|---:|", "| 1 | 2 |"])
        self.assertEqual(out, ["│ a │ bbb │",
                               "├───┼─────┤",
                               "│ 1 │   2 │"])

    def test_columns_are_sized_by_display_width_not_char_count(self):
        # "이름" is 2 chars but 4 columns; "한글칸" is 3 chars / 6 columns and sets the width.
        out = gg.render_table(["| 이름 | v |", "|:---:|---|", "| 한글칸 | x |"])
        self.assertEqual(out, ["│  이름  │ v │",
                               "├────────┼───┤",
                               "│ 한글칸 │ x │"])
        # every line is the same number of columns wide — that is what "aligned" means here
        self.assertEqual({gg.dw(l) for l in out}, {gg.dw(out[0])})

    def test_centre_alignment_pads_both_sides(self):
        out = gg.render_table(["| ab |", "|:--:|", "| x |"])
        self.assertEqual(out[-1], "│ x  │")   # 1 wide in a 2-wide column: left pad 0, right pad 1

    def test_ragged_rows_are_padded_to_the_widest_row(self):
        out = gg.render_table(["| a | b | c |", "| 1 |"])
        self.assertEqual(out, ["│ a │ b │ c │",
                               "│ 1 │   │   │"])

    def test_without_a_separator_row_there_is_no_header_rule(self):
        out = gg.render_table(["| a | b |", "| 1 | 2 |"])
        self.assertNotIn("├", "".join(out))

    def test_max_col_caps_a_wide_column(self):
        out = gg.render_table(["| " + "x" * 20 + " | b |", "|---|---|", "| y | z |"], max_col=8)
        self.assertEqual(out[0], "│ xxxxxxx… │ b │")
        self.assertEqual(gg.dw(out[0]), gg.dw(out[-1]))

    def test_single_cell_table_does_not_crash(self):
        self.assertEqual(gg.render_table(["| only |"]), ["│ only │"])


class TestRenderMarkdown(unittest.TestCase):
    def test_prose_wraps_and_a_table_is_left_unwrapped(self):
        md = "para one is long enough to wrap here maybe\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\ntail"
        out = gg.render_markdown(md, 20)
        self.assertEqual(out, ["para one is long",
                               "enough to wrap here",
                               "maybe",
                               "│ a │ b │",
                               "├───┼───┤",
                               "│ 1 │ 2 │",
                               "",
                               "tail"])

    def test_a_pipe_table_inside_a_code_fence_stays_verbatim(self):
        out = gg.render_markdown("before\n```\n| not | a table |\n```\nafter", 40)
        self.assertEqual(out, ["before", "```", "| not | a table |", "```", "after"])

    def test_a_lone_pipe_line_is_prose_not_a_table(self):
        # a table needs two consecutive '|' lines
        out = gg.render_markdown("| lonely |", 40)
        self.assertEqual(out, ["| lonely |"])

    def test_table_lines_can_be_wider_than_the_window(self):
        md = "| " + "a" * 30 + " | " + "b" * 30 + " |\n|---|---|\n| 1 | 2 |"
        out = gg.render_markdown(md, 20)
        self.assertTrue(max(gg.dw(l) for l in out) > 20, "tables must not be wrapped (H/L scroll instead)")

    def test_empty_text_yields_no_lines(self):
        # "".splitlines() is empty, so there is no block to wrap at all
        self.assertEqual(gg.render_markdown("", 20), [])


class TestMdSegments(unittest.TestCase):
    def test_headings_quotes_and_fences_are_whole_line_styles(self):
        self.assertEqual(gg.md_segments("# head"), [("# head", "md_h")])
        self.assertEqual(gg.md_segments("###### deep"), [("###### deep", "md_h")])
        self.assertEqual(gg.md_segments("> quote"), [("> quote", "md_quote")])
        self.assertEqual(gg.md_segments("```py"), [("```py", "md_code")])

    def test_in_code_overrides_everything(self):
        self.assertEqual(gg.md_segments("# not a heading", in_code=True), [("# not a heading", "md_code")])

    def test_bullet_is_replaced_and_inline_styles_split(self):
        self.assertEqual(gg.md_segments("- bullet **b** `c`"),
                         [("• ", "fold"), ("bullet ", ""), ("b", "md_bold"), (" ", ""), ("`c`", "md_code")])

    def test_numbered_bullet_keeps_its_marker(self):
        self.assertEqual(gg.md_segments("3. num"), [("3. ", "fold"), ("num", "")])

    def test_indented_line_is_not_a_bullet(self):
        self.assertEqual(gg.md_segments("    indented code"), [("    indented code", "")])

    def test_link_becomes_title_plus_dim_target_and_bare_url_is_a_url(self):
        self.assertEqual(gg.md_segments("see [x](http://e.com) and https://z.io"),
                         [("see ", ""), ("x", "url"), (" (http://e.com)", "meta"),
                          (" and ", ""), ("https://z.io", "url")])

    def test_bold_markers_are_dropped_from_the_text(self):
        segs = gg.md_segments("**loud**")
        self.assertEqual(segs, [("loud", "md_bold")])

    def test_plain_line_is_one_unstyled_segment(self):
        self.assertEqual(gg.md_segments("plain"), [("plain", "")])

    def test_segments_never_lose_visible_characters(self):
        # markers (** and the bullet) are the only thing allowed to disappear
        for line in ["plain text", "a `code` b", "see https://x.io now", "> q", "# h"]:
            self.assertEqual("".join(t for t, _ in gg.md_segments(line)), line)


if __name__ == "__main__":
    unittest.main()
