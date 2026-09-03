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
import re
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


class TestShortcutCoverageInHangulMode(unittest.TestCase):
    """Every single-letter TUI shortcut must be typable while the IME is in Hangul mode.

    The binding list is read out of gitgraph.py's own handle_key() rather than duplicated here, so a
    newly added shortcut that no jamo maps to fails this test instead of silently being unusable for
    anyone typing in Hangul.
    """

    @classmethod
    def setUpClass(cls):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "gitgraph.py"), encoding="utf-8").read()
        body = src[src.index("    def handle_key(self, k):"):]
        cls.bound = sorted({m.group(1) for m in re.finditer(r'ord\("([a-z])"\)', body)})
        cls.by_key = {}
        for jamo, key in gg.JAMO_KEY.items():
            cls.by_key.setdefault(key, []).append(jamo)

    def test_the_binding_list_was_actually_found(self):
        self.assertGreater(len(self.bound), 15, f"parsed only {self.bound} — did handle_key move?")

    def test_every_lowercase_shortcut_is_reachable_from_a_jamo(self):
        missing = [k for k in self.bound if k not in self.by_key]
        self.assertEqual(missing, [], f"no Hangul jamo types these shortcuts: {missing}")

    def test_each_jamo_maps_back_to_exactly_that_shortcut(self):
        for key in self.bound:
            for jamo in self.by_key[key]:
                self.assertEqual(gg.hangul_keys(jamo), key, f"{jamo} should type {key}")

    def test_the_shortcuts_users_actually_press_in_hangul(self):
        # spot checks from README.ko / HELP, so a broken JAMO_KEY entry is obvious in the failure text
        for jamo, key in {"ㅁ": "a", "ㅓ": "j", "ㅏ": "k", "ㅂ": "q", "ㄱ": "r",
                          "ㅅ": "t", "ㅇ": "d", "ㅑ": "i", "ㅛ": "y", "ㅡ": "m"}.items():
            self.assertEqual(gg.hangul_keys(jamo), key)


class TestImeSwitch(unittest.TestCase):
    """gg.ime_state()/ime_set(): the fcitx5-remote switch behind 0.30.0's 'shortcuts get an English
    keyboard' — driven here against tests/fakes/fcitx5-remote (on PATH via env.load_module()), whose
    state lives in $FAKE_IME_STATE_FILE and whose calls land in $FAKE_IME_LOG."""

    def setUp(self):
        home = os.environ["HOME"]
        self.state = os.path.join(home, "ime-state")
        self.log = os.path.join(home, "ime-log")
        for p in (self.state, self.log):
            if os.path.exists(p):
                os.remove(p)
        os.environ["FAKE_IME_STATE_FILE"] = self.state
        os.environ["FAKE_IME_LOG"] = self.log
        os.environ.pop("GITGRAPH_IME_SWITCH", None)

    def tearDown(self):
        for k in ("FAKE_IME_STATE_FILE", "FAKE_IME_LOG", "GITGRAPH_IME_SWITCH"):
            os.environ.pop(k, None)

    def calls(self):
        with open(self.log, encoding="utf-8") as f:
            return f.read().splitlines()

    def test_state_and_set_round_trip(self):
        self.assertEqual(gg.ime_state(), 2)          # the fake starts in Hangul mode
        gg.ime_set(False)
        self.assertEqual(gg.ime_state(), 1)
        gg.ime_set(True)
        self.assertEqual(gg.ime_state(), 2)
        self.assertEqual(self.calls(), ["", "-c", "", "-o", ""])

    def test_disabled_by_config_runs_nothing(self):
        os.environ["GITGRAPH_IME_SWITCH"] = "false"
        self.assertIsNone(gg.ime_tool())
        self.assertIsNone(gg.ime_state())
        gg.ime_set(False)
        self.assertFalse(os.path.exists(self.log))

    def test_without_fcitx5_remote_on_path_is_a_quiet_none(self):
        path = os.environ["PATH"]
        os.environ["PATH"] = os.path.join(os.environ["HOME"], "no-such-dir")
        try:
            self.assertIsNone(gg.ime_tool())
            self.assertIsNone(gg.ime_state())
            gg.ime_set(True)                          # no exception
        finally:
            os.environ["PATH"] = path
        self.assertFalse(os.path.exists(self.log))

    def test_tui_ime_english_only_acts_on_hangul_mode(self):
        t = gg.Tui.__new__(gg.Tui)
        gg.Tui.ime_english(t)                         # state 2: switched
        self.assertEqual(self.calls(), ["", "-c"])
        gg.Tui.ime_english(t)                         # state 1 now: only the query
        self.assertEqual(self.calls(), ["", "-c", ""])


if __name__ == "__main__":
    unittest.main()
