"""Unit tests for the display-width helpers gg uses everywhere text is measured in terminal columns
instead of Python len() (CLAUDE.md's "Terminal width" convention): dw, trunc, clip, slice_cols,
split_width, wrap, char_at.

Every assertion here was checked against the real function's actual output first (see the task's
"assert current, real behaviour" rule) — none of it is inferred from docstrings alone.

Run: python3 -m unittest tests.test_width -v
"""
import os
import sys
import unicodedata
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as testenv  # noqa: E402

gg = testenv.load_module()

# A combining acute accent (U+0301): unicodedata.east_asian_width == 'A' but gg._cw() special-cases
# unicodedata.combining() first, so this is display-width 0 regardless of east-asian-width.
COMBINING_ACUTE = "́"

# Tricky strings used across several tests below: ASCII, Hangul syllables, individual (compatibility)
# jamo, Hanja, a combining mark, and mixes of wide + narrow.
TRICKY = [
    "",
    "a",
    "hello",
    "한글",           # 2 precomposed Hangul syllables
    "한",
    "ㅏㄱ",           # standalone compatibility jamo (also east-asian-width W, not just composed syllables)
    "가나다라마바사아자차카타파하",  # long Hangul
    "漢字",           # Hanja / CJK ideographs
    "e" + COMBINING_ACUTE,
    "café",
    "Hello, 世界!",   # mixed ASCII + CJK
    "a한b글c",
    "   spaced   out   ",
]


class TestDw(unittest.TestCase):
    def test_empty_string_is_zero(self):
        self.assertEqual(gg.dw(""), 0)

    def test_ascii_is_one_column_per_char(self):
        self.assertEqual(gg.dw("hello"), 5)

    def test_hangul_syllable_is_two_columns(self):
        self.assertEqual(gg.dw("한"), 2)
        self.assertEqual(gg.dw("한글"), 4)

    def test_standalone_jamo_is_also_wide(self):
        # Individual (compatibility) jamo like ㅏ/ㄱ carry east_asian_width == 'W' just like composed
        # syllables, not just when combined into a syllable block.
        self.assertEqual(unicodedata.east_asian_width("ㅏ"), "W")
        self.assertEqual(gg.dw("ㅏ"), 2)
        self.assertEqual(gg.dw("ㅏㄱ"), 4)

    def test_hanja_and_cjk_ideographs_are_wide(self):
        self.assertEqual(gg.dw("漢字"), 4)

    def test_combining_mark_is_zero_width(self):
        # unicodedata.east_asian_width(COMBINING_ACUTE) == 'A' (ambiguous), which would normally be
        # narrow (1) under gg's rule, but gg._cw() checks unicodedata.combining() first and returns 0.
        self.assertEqual(unicodedata.combining(COMBINING_ACUTE), 230)
        self.assertEqual(gg.dw(COMBINING_ACUTE), 0)
        self.assertEqual(gg.dw("e" + COMBINING_ACUTE), 1)

    def test_mixed_ascii_and_cjk(self):
        # "Hello, 世界!" = 8 ASCII/punct chars (1 col each) + 2 wide chars (2 cols each) = 12
        self.assertEqual(gg.dw("Hello, 世界!"), 12)

    def test_dw_never_negative_over_the_tricky_table(self):
        for s in TRICKY:
            self.assertGreaterEqual(gg.dw(s), 0, repr(s))


class TestTrunc(unittest.TestCase):
    def test_short_string_is_unchanged(self):
        self.assertEqual(gg.trunc("hello", 10), "hello")

    def test_collapses_whitespace_and_strips(self):
        self.assertEqual(gg.trunc("  a   b  \n c ", 20), "a b c")

    def test_none_becomes_empty_string(self):
        self.assertEqual(gg.trunc(None, 10), "")

    def test_truncation_uses_character_count_not_display_width(self):
        # trunc(s, n) is `s[:n-1] + "…"` when len(s) > n -- a plain character-count cut, not a
        # display-width-aware one. A CJK string whose *character* count is <= n is left whole even
        # though its display width is well over n columns (see test_width_can_exceed_n_for_cjk below).
        s = "가나다라마바사아자차"  # 10 Hangul syllables = 10 chars, 20 display columns
        self.assertEqual(len(s), 10)
        self.assertEqual(gg.trunc(s, 10), s)  # len(s) == n -> not truncated
        self.assertEqual(gg.dw(gg.trunc(s, 10)), 20)  # ...but the display width is double n

    def test_truncates_at_n_minus_one_chars_plus_ellipsis(self):
        self.assertEqual(gg.trunc("abcdefghij", 5), "abcd…")
        self.assertEqual(gg.trunc("가나다라마바사아자차", 5), "가나다라…")

    def test_width_can_exceed_n_for_cjk(self):
        # Documented (suspected-bug-adjacent) behaviour: trunc's `n` bounds len(), not dw(), so the
        # result's display width is not bounded by n for wide-char-heavy text.
        out = gg.trunc("가나다라마바사아자차", 9)
        self.assertEqual(out, "가나다라마바사아…")  # s[:8] (8 chars) + ellipsis = 9 chars
        self.assertLessEqual(len(out), 9)
        self.assertGreater(gg.dw(out), 9)


class TestCharAt(unittest.TestCase):
    def test_ascii_columns(self):
        self.assertEqual(gg.char_at("abc", 0), "a")
        self.assertEqual(gg.char_at("abc", 2), "c")

    def test_wide_char_occupies_two_columns(self):
        s = "한글"
        self.assertEqual(gg.char_at(s, 0), "한")
        self.assertEqual(gg.char_at(s, 1), "한")
        self.assertEqual(gg.char_at(s, 2), "글")
        self.assertEqual(gg.char_at(s, 3), "글")

    def test_beyond_end_is_empty(self):
        self.assertEqual(gg.char_at("abc", 3), "")
        self.assertEqual(gg.char_at("abc", 100), "")

    def test_negative_column_is_empty(self):
        self.assertEqual(gg.char_at("abc", -1), "")

    def test_empty_string_is_always_empty(self):
        self.assertEqual(gg.char_at("", 0), "")

    def test_combining_mark_is_never_returned(self):
        # The combining mark has width 0, so no display column ever lands "on" it.
        s = "e" + COMBINING_ACUTE + "f"
        self.assertEqual(gg.char_at(s, 0), "e")
        self.assertEqual(gg.char_at(s, 1), "f")


class TestSliceCols(unittest.TestCase):
    def test_basic_ascii_slice(self):
        self.assertEqual(gg.slice_cols("hello", 1, 3), "el")

    def test_whole_char_included_on_any_overlap(self):
        # Unlike clip(), slice_cols keeps a whole character as soon as its column span overlaps
        # [c0, c1) at all -- it never pads a partial overlap with a space.
        s = "한글AB"  # columns: 한=[0,2) 글=[2,4) A=[4,5) B=[5,6)
        self.assertEqual(gg.slice_cols(s, 1, 3), "한글")   # overlaps both 한 and 글 by one column each
        self.assertEqual(gg.slice_cols(s, 0, 1), "한")     # overlaps 한 by one column
        self.assertEqual(gg.slice_cols(s, 3, 4), "글")     # overlaps 글's last column only

    def test_shorter_than_window_returns_whole_string(self):
        self.assertEqual(gg.slice_cols("ab", 0, 10), "ab")

    def test_c0_greater_than_c1_is_empty(self):
        self.assertEqual(gg.slice_cols("hello", 3, 1), "")

    def test_negative_c0_still_bounds_at_start(self):
        self.assertEqual(gg.slice_cols("한글", -2, 2), "한")


class TestClip(unittest.TestCase):
    def test_basic_ascii_window(self):
        self.assertEqual(gg.clip("hello world", 6, 5), "world")

    def test_half_wide_char_becomes_a_space(self):
        # clip() never emits half of a wide character: a window boundary that lands inside one
        # produces a literal " " for that column instead of a mangled/partial glyph.
        s = "한글AB"  # 한=[0,2) 글=[2,4) A=[4,5) B=[5,6)
        self.assertEqual(gg.clip(s, 1, 1), " ")     # only the 2nd column of 한
        self.assertEqual(gg.clip(s, 1, 3), " 글")   # 2nd col of 한 (padded) + all of 글
        self.assertEqual(gg.clip(s, 3, 2), " A")    # 2nd col of 글 (padded) + A

    def test_clip_never_splits_a_wide_char_into_a_half_cell_glyph(self):
        # For every start/width pair over a wide-char string, every char in the result is either
        # a literal " " (the padding for a cut wide char) or a full, untouched original character --
        # never something synthesized from half of a multi-column glyph.
        s = "한글AB가나"
        for start in range(0, 8):
            for width in range(0, 8):
                out = gg.clip(s, start, width)
                for ch in out:
                    self.assertTrue(ch == " " or ch in s, f"clip({s!r},{start},{width}) produced {ch!r}")

    def test_invariant_dw_of_clip_is_at_most_width(self):
        # dw(clip(s, a, b)) <= b, for a table of tricky strings and a spread of (start, width) pairs.
        # Only non-negative widths are checked here: with a negative width dw() is always 0 while the
        # width itself is negative, so 0 <= negative_width is false -- see
        # TestClipEdgeWidths.test_negative_width_returns_empty_but_breaks_the_naive_invariant below.
        for s in TRICKY:
            n = max(len(s), 1)
            for start in range(-2, n + 3):
                for width in range(0, n + 4):
                    out = gg.clip(s, start, width)
                    self.assertLessEqual(gg.dw(out), width,
                                         f"clip({s!r}, {start}, {width}) = {out!r} has dw {gg.dw(out)} > {width}")

    def test_string_shorter_than_the_window_is_returned_whole(self):
        self.assertEqual(gg.clip("ab", 0, 10), "ab")

    def test_start_beyond_string_end_is_empty(self):
        self.assertEqual(gg.clip("abc", 10, 5), "")


class TestClipEdgeWidths(unittest.TestCase):
    def test_zero_width_is_always_empty(self):
        self.assertEqual(gg.clip("한글AB", 2, 0), "")
        self.assertEqual(gg.clip("hello", 0, 0), "")

    def test_negative_width_returns_empty_but_breaks_the_naive_invariant(self):
        # width < 0 -> start+width < start, so no character's column range can satisfy
        # `col + w > start + width`... in practice every char is skipped and the result is "".
        # dw("") == 0, which is NOT <= a negative width -- the invariant only holds for width >= 0.
        out = gg.clip("한글AB", 2, -1)
        self.assertEqual(out, "")
        self.assertGreater(gg.dw(out), -1)  # 0 > -1: the invariant would be violated if width could be negative

    def test_negative_start_clips_from_the_beginning(self):
        self.assertEqual(gg.clip("한글AB", -1, 3), "한")


class TestSplitWidth(unittest.TestCase):
    def test_short_string_returns_whole_head_and_empty_rest(self):
        self.assertEqual(gg.split_width("ab", 10), ("ab", ""))

    def test_splits_at_the_exact_column_boundary(self):
        self.assertEqual(gg.split_width("hello world", 5), ("hello", " world"))

    def test_never_splits_a_wide_char_in_half(self):
        # width=3 lands inside "글" (columns [2,4)); the whole char goes to `rest`, not half to `head`.
        s = "한글AB"
        head, rest = gg.split_width(s, 3)
        self.assertEqual(head, "한")
        self.assertEqual(rest, "글AB")
        self.assertEqual(gg.dw(head), 2)

    def test_zero_width_returns_everything_as_rest(self):
        self.assertEqual(gg.split_width("한글AB", 0), ("", "한글AB"))

    def test_negative_width_returns_everything_as_rest(self):
        self.assertEqual(gg.split_width("한글AB", -1), ("", "한글AB"))

    def test_head_plus_rest_reconstructs_the_original(self):
        for s in TRICKY:
            for width in range(0, 12):
                head, rest = gg.split_width(s, width)
                self.assertEqual(head + rest, s, f"split_width({s!r}, {width})")


class TestWrap(unittest.TestCase):
    def test_short_line_is_not_wrapped(self):
        self.assertEqual(gg.wrap("hello", 20), ["hello"])

    def test_none_input_yields_one_empty_line(self):
        self.assertEqual(gg.wrap(None, 20), [""])

    def test_empty_string_yields_one_empty_line(self):
        self.assertEqual(gg.wrap("", 20), [""])

    def test_preserves_existing_blank_lines(self):
        self.assertEqual(gg.wrap("line1\nline2\n\nline4", 20), ["line1", "line2", "", "line4"])

    def test_ascii_word_wrap_is_greedy(self):
        out = gg.wrap("hello world this is a test of wrapping", 10)
        self.assertEqual(out, ["hello", "world this", "is a test", "of", "wrapping"])
        for line in out:
            self.assertLessEqual(gg.dw(line), 10)

    def test_cjk_wraps_by_display_width_not_char_count(self):
        # No spaces at all: wrap must fall through to the hard split_width path per line (5 wide chars
        # = 10 columns fit exactly per line).
        out = gg.wrap("가나다라마바사아자차카타파하", 10)
        self.assertEqual(out, ["가나다라마", "바사아자차", "카타파하"])
        for line in out:
            self.assertLessEqual(gg.dw(line), 10)
        self.assertEqual("".join(out), "가나다라마바사아자차카타파하")

    def test_width_is_clamped_to_a_minimum_of_8(self):
        # wrap() does `width = max(width, 8)` before anything else, so asking for width=3 behaves like
        # width=8: a 5-char/5-column ASCII word still fits on one line.
        self.assertEqual(gg.wrap("short", 3), ["short"])

    def test_unbreakable_long_token_is_hard_split(self):
        url = "https://example.com/" + "a" * 87
        out = gg.wrap(url, 20)
        for line in out:
            self.assertLessEqual(gg.dw(line), 20)
        self.assertEqual("".join(out), url)


if __name__ == "__main__":
    unittest.main()
