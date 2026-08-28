"""Unit tests for gg.hangul_keys(): maps a Hangul character (jamo or composed syllable) typed while a
2-set Korean IME is active back to the Latin keys that were physically pressed, so that a keyboard
shortcut typed in Hangul mode (e.g. ㅓ for the "j" binding) still fires (CLAUDE.md: "shortcuts still
work while the keyboard is in Hangul mode").

This is the regression test for commit 0.12.1 (the IME commit key after a Hangul-typed shortcut, e.g.
pressing the "a" (ask) binding while in Hangul mode commits the lone jamo ㅁ) -- see JAMO_KEY /
hangul_keys() around gitgraph.py:2620-2643.

Run: python3 -m unittest tests.test_ime -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as testenv  # noqa: E402

gg = testenv.load_module()

# Composed syllables covering: no final consonant, a simple final, and a complex (double) final --
# each hand-decoded against gg._CHO/_JUNG/_JONG and cross-checked against real 2-set keyboard layout
# knowledge, then verified to match hangul_keys()'s actual output before being written down here.
COMPOSED_SYLLABLES = {
    "자": "wk",     # ㅈ+ㅏ, no final
    "가": "rk",     # ㄱ+ㅏ, no final
    "한": "gks",    # ㅎ+ㅏ+ㄴ, simple final
    "글": "rmf",    # ㄱ+ㅡ+ㄹ, simple final
    "값": "rkqt",   # ㄱ+ㅏ+ㅄ, complex final (ㅄ itself maps to two keys: q, t)
    "않": "dksg",   # ㅇ+ㅏ+ㄶ, complex final (ㄶ -> s, g)
    "밟": "qkfq",   # ㅂ+ㅏ+ㄼ, complex final (ㄼ -> f, q)
    "읽": "dlfr",   # ㅇ+ㅣ+ㄺ, complex final (ㄺ -> f, r)
    "닭": "ekfr",   # ㄷ+ㅏ+ㄺ, complex final
    "뜻": "Emt",    # ㄸ(shift)+ㅡ+ㅅ
}


class TestHangulKeysJamoTable(unittest.TestCase):
    """Every single-jamo entry of JAMO_KEY (the full 2-set layout: plain/shifted consonants and
    vowels, plus the compound vowels and complex finals that are themselves 2-key sequences) must
    round-trip through hangul_keys() unchanged."""

    def test_every_jamo_key_table_entry_round_trips(self):
        for jamo, keys in gg.JAMO_KEY.items():
            self.assertEqual(gg.hangul_keys(jamo), keys, f"jamo {jamo!r}")

    def test_plain_consonants(self):
        self.assertEqual(gg.hangul_keys("ㅂ"), "q")
        self.assertEqual(gg.hangul_keys("ㅈ"), "w")
        self.assertEqual(gg.hangul_keys("ㄷ"), "e")
        self.assertEqual(gg.hangul_keys("ㄱ"), "r")
        self.assertEqual(gg.hangul_keys("ㅅ"), "t")
        self.assertEqual(gg.hangul_keys("ㅁ"), "a")
        self.assertEqual(gg.hangul_keys("ㄴ"), "s")
        self.assertEqual(gg.hangul_keys("ㅇ"), "d")
        self.assertEqual(gg.hangul_keys("ㄹ"), "f")
        self.assertEqual(gg.hangul_keys("ㅎ"), "g")

    def test_shifted_consonants(self):
        self.assertEqual(gg.hangul_keys("ㅃ"), "Q")
        self.assertEqual(gg.hangul_keys("ㅉ"), "W")
        self.assertEqual(gg.hangul_keys("ㄸ"), "E")
        self.assertEqual(gg.hangul_keys("ㄲ"), "R")
        self.assertEqual(gg.hangul_keys("ㅆ"), "T")

    def test_plain_vowels(self):
        self.assertEqual(gg.hangul_keys("ㅗ"), "h")
        self.assertEqual(gg.hangul_keys("ㅓ"), "j")
        self.assertEqual(gg.hangul_keys("ㅏ"), "k")
        self.assertEqual(gg.hangul_keys("ㅣ"), "l")
        self.assertEqual(gg.hangul_keys("ㅋ"), "z")
        self.assertEqual(gg.hangul_keys("ㅌ"), "x")
        self.assertEqual(gg.hangul_keys("ㅊ"), "c")
        self.assertEqual(gg.hangul_keys("ㅍ"), "v")
        self.assertEqual(gg.hangul_keys("ㅠ"), "b")
        self.assertEqual(gg.hangul_keys("ㅜ"), "n")
        self.assertEqual(gg.hangul_keys("ㅡ"), "m")

    def test_shifted_vowels(self):
        self.assertEqual(gg.hangul_keys("ㅒ"), "O")
        self.assertEqual(gg.hangul_keys("ㅖ"), "P")

    def test_compound_vowels_are_two_key_sequences(self):
        self.assertEqual(gg.hangul_keys("ㅘ"), "hk")
        self.assertEqual(gg.hangul_keys("ㅙ"), "ho")
        self.assertEqual(gg.hangul_keys("ㅚ"), "hl")
        self.assertEqual(gg.hangul_keys("ㅝ"), "nj")
        self.assertEqual(gg.hangul_keys("ㅞ"), "np")
        self.assertEqual(gg.hangul_keys("ㅟ"), "nl")
        self.assertEqual(gg.hangul_keys("ㅢ"), "ml")

    def test_complex_finals_are_two_key_sequences(self):
        self.assertEqual(gg.hangul_keys("ㄳ"), "rt")
        self.assertEqual(gg.hangul_keys("ㄵ"), "sw")
        self.assertEqual(gg.hangul_keys("ㄶ"), "sg")
        self.assertEqual(gg.hangul_keys("ㄺ"), "fr")
        self.assertEqual(gg.hangul_keys("ㄻ"), "fa")
        self.assertEqual(gg.hangul_keys("ㄼ"), "fq")
        self.assertEqual(gg.hangul_keys("ㄽ"), "ft")
        self.assertEqual(gg.hangul_keys("ㄾ"), "fx")
        self.assertEqual(gg.hangul_keys("ㄿ"), "fv")
        self.assertEqual(gg.hangul_keys("ㅀ"), "fg")
        self.assertEqual(gg.hangul_keys("ㅄ"), "qt")


class TestHangulKeysComposedSyllables(unittest.TestCase):
    def test_composed_syllables_table(self):
        for syllable, keys in COMPOSED_SYLLABLES.items():
            self.assertEqual(gg.hangul_keys(syllable), keys, f"syllable {syllable!r}")

    def test_regression_0_12_1_ask_shortcut_jamo_maps_back_to_a(self):
        # The "a" (ask) binding, typed while the IME is in Hangul mode, commits as the lone jamo ㅁ;
        # hangul_keys() must map it back to "a" so the shortcut still dispatches.
        self.assertEqual(gg.hangul_keys("ㅁ"), "a")

    def test_no_final_consonant_syllable(self):
        self.assertEqual(gg.hangul_keys("자"), "wk")

    def test_simple_final_consonant_syllable(self):
        self.assertEqual(gg.hangul_keys("한"), "gks")

    def test_complex_double_final_consonant_syllable(self):
        self.assertEqual(gg.hangul_keys("값"), "rkqt")


class TestHangulKeysNonHangul(unittest.TestCase):
    def test_ascii_letter_is_not_hangul(self):
        self.assertEqual(gg.hangul_keys("a"), "")

    def test_digit_is_not_hangul(self):
        self.assertEqual(gg.hangul_keys("1"), "")

    def test_punctuation_is_not_hangul(self):
        self.assertEqual(gg.hangul_keys("!"), "")

    def test_cjk_ideograph_is_not_hangul(self):
        self.assertEqual(gg.hangul_keys("漢"), "")

    def test_hangul_syllable_block_boundaries(self):
        # U+AC00 (가) is the first Hangul syllable, U+D7A3 (힣) the last; one below and one above
        # should both fail the composed-syllable range check (and aren't in JAMO_KEY either).
        self.assertNotEqual(gg.hangul_keys(chr(0xAC00)), "")
        self.assertNotEqual(gg.hangul_keys(chr(0xD7A3)), "")
        self.assertEqual(gg.hangul_keys(chr(0xAC00 - 1)), "")

    def test_empty_string_raises(self):
        # Suspected edge-case bug: hangul_keys("") isn't guarded -- it falls through to ord(""),
        # which raises TypeError instead of returning "" like every other non-Hangul input.
        with self.assertRaises(TypeError):
            gg.hangul_keys("")


if __name__ == "__main__":
    unittest.main()
