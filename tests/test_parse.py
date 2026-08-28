"""Unit tests for gg's reference/mention parsing layer (CLAUDE.md "Graph model" -- edges come from
parsing bodies and comments): parse_refs_ctx, parse_refs, parse_mentions, _plausible_small_ref,
snippet, _clean_lines.

Table-driven per the task: every assertion was checked against the real function first, then written
down -- including a couple of behaviours that look surprising (documented as such, not "fixed").

Run: python3 -m unittest tests.test_parse -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as testenv  # noqa: E402

gg = testenv.load_module()

DEFAULT_REPO = "test/repo"          # github.com host
ENTERPRISE_REPO = "ghe.example.com/eng/tools"  # Enterprise host


class TestCleanLines(unittest.TestCase):
    def test_strips_code_fences(self):
        text = "before\n```\nfenced #5 line\nmore fenced\n```\nafter"
        lines = gg._clean_lines(text)
        joined = "\n".join(lines)
        self.assertNotIn("fenced", joined)
        self.assertIn("before", joined)
        self.assertIn("after", joined)

    def test_drops_kernel_log_timestamp_lines(self):
        lines = gg._clean_lines("normal line\n[  123.456789] BUG: something bad #99\nanother normal line")
        self.assertEqual(lines, ["normal line", "another normal line"])

    def test_drops_tainted_and_pid_lines(self):
        text = "\n".join([
            "intro",
            "Tainted: G W    5.10.0 #1",
            "PID: 1234 Comm: kworker referencing #17",
            "outro",
        ])
        self.assertEqual(gg._clean_lines(text), ["intro", "outro"])

    def test_none_text_yields_no_lines(self):
        self.assertEqual(gg._clean_lines(None), [])

    def test_empty_text_yields_one_empty_line(self):
        # "".splitlines() == [] in plain Python, but gg._clean_lines keeps the (non-noise) result of
        # FENCE_RE.sub on an empty string, which itself splits to [].
        self.assertEqual(gg._clean_lines(""), [])


class TestPlausibleSmallRef(unittest.TestCase):
    def _match(self, line):
        return next(gg.REF_RE.finditer(line))

    def test_owner_repo_hash_num_is_always_plausible_even_when_small(self):
        m = self._match("owner/repo#5")
        self.assertTrue(gg._plausible_small_ref("owner/repo#5", m))

    def test_ordinal_use_after_a_noun_is_not_a_reference(self):
        m = self._match("overwrite #5")
        self.assertFalse(gg._plausible_small_ref("overwrite #5", m))

    def test_after_a_reference_word_is_a_reference(self):
        m = self._match("see PR #5")
        self.assertTrue(gg._plausible_small_ref("see PR #5", m))

    def test_bare_hash_at_line_start_is_not_a_reference(self):
        m = self._match("#5 at start")
        self.assertFalse(gg._plausible_small_ref("#5 at start", m))

    def test_bare_url_with_no_preceding_words_is_plausible(self):
        # is_url=True short-circuits to True when there is no preceding word at all on the line.
        m = self._match("#5")  # any match object with a start() will do for this codepath
        self.assertTrue(gg._plausible_small_ref("#5", m, is_url=True))

    def test_case_insensitive_reference_word(self):
        m = self._match("See PR #5")
        self.assertTrue(gg._plausible_small_ref("See PR #5", m))


class TestSnippet(unittest.TestCase):
    def test_short_line_is_returned_stripped_of_quote_markers(self):
        self.assertEqual(gg.snippet("  > quoted short line #3  ", 20, 22), "quoted short line #3")

    def test_collapses_internal_whitespace(self):
        self.assertEqual(gg.snippet("a   b\tc", 0, 1), "a b c")

    def test_long_line_is_truncated_around_the_match_with_ellipses(self):
        long_line = "word " * 60 + "TARGET" + " word" * 60
        start = long_line.index("TARGET")
        end = start + len("TARGET")
        snip = gg.snippet(long_line, start, end)
        self.assertTrue(snip.startswith("…"))
        self.assertTrue(snip.endswith("…"))
        self.assertIn("TARGET", snip)
        # window is [start - SNIPPET_CHARS//2, end + SNIPPET_CHARS//2), so its span is SNIPPET_CHARS
        # plus however long the match itself is, plus the two ellipsis characters.
        self.assertEqual(len(snip), gg.SNIPPET_CHARS + (end - start) + 2)
        self.assertLess(len(snip), len(long_line))  # actually shorter than the untruncated line

    def test_match_near_the_very_start_has_no_leading_ellipsis(self):
        long_line = "TARGET" + " word" * 60
        snip = gg.snippet(long_line, 0, 6)
        self.assertFalse(snip.startswith("…"))
        self.assertTrue(snip.endswith("…"))


class TestParseRefsCtx(unittest.TestCase):
    def test_code_fence_hash_number_is_ignored(self):
        text = "before the fence\n```\noverwrite #5 in code\n```\nsee PR #7 after"
        refs = gg.parse_refs(text, DEFAULT_REPO)
        self.assertEqual(refs, [(DEFAULT_REPO, 7)])

    def test_kernel_log_and_stack_trace_lines_are_ignored(self):
        text = "\n".join([
            "[  123.456789] referencing #99 in a kernel log",
            "PID: 1234 Comm: kworker referencing #17",
            "Tainted: G W  referencing #22",
            "see issue #14 in normal prose",
        ])
        self.assertEqual(gg.parse_refs(text, DEFAULT_REPO), [(DEFAULT_REPO, 14)])

    def test_small_number_ordinal_is_not_a_reference(self):
        self.assertEqual(gg.parse_refs("overwrite #5 happened again", DEFAULT_REPO), [])

    def test_small_number_after_reference_word_is_a_reference(self):
        self.assertEqual(gg.parse_refs("see PR #5 for details", DEFAULT_REPO), [(DEFAULT_REPO, 5)])

    def test_owner_repo_hash_num_qualifies_against_default_host(self):
        # default_repo is on github.com, so a bare "foo/bar#1" stays unqualified (already github.com).
        self.assertEqual(gg.parse_refs("see foo/bar#1", DEFAULT_REPO), [("foo/bar", 1)])

    def test_owner_repo_hash_num_qualifies_against_enterprise_host(self):
        # default_repo is on an Enterprise host, so a bare "foo/bar#1" is qualified onto that host.
        self.assertEqual(gg.parse_refs("see foo/bar#1", ENTERPRISE_REPO),
                          [("ghe.example.com/foo/bar", 1)])

    def test_full_github_url(self):
        text = "see https://github.com/foo/bar/issues/99 please"
        self.assertEqual(gg.parse_refs(text, DEFAULT_REPO), [("foo/bar", 99)])

    def test_full_pull_url(self):
        text = "see https://github.com/foo/bar/pull/12 please"
        self.assertEqual(gg.parse_refs(text, DEFAULT_REPO), [("foo/bar", 12)])

    def test_enterprise_host_url_resolves_to_that_host(self):
        text = "see https://ghe.example.com/eng/tools/pull/5 please"
        self.assertEqual(gg.parse_refs(text, DEFAULT_REPO), [("ghe.example.com/eng/tools", 5)])

    def test_small_number_in_url_still_needs_a_reference_word(self):
        # "please see" precedes the URL -> "see" is a reference word -> included.
        text = "please see https://ghe.example.com/eng/tools/pull/5 today"
        self.assertEqual(gg.parse_refs(text, DEFAULT_REPO), [("ghe.example.com/eng/tools", 5)])

    def test_dependabot_redirect_host_normalizes_to_github_com(self):
        text = "see https://redirect.github.com/foo/bar/issues/8 please"
        self.assertEqual(gg.parse_refs(text, DEFAULT_REPO), [("foo/bar", 8)])

    def test_www_prefixed_host_normalizes_to_github_com(self):
        text = "see https://www.github.com/foo/bar/issues/8 please"
        self.assertEqual(gg.parse_refs(text, DEFAULT_REPO), [("foo/bar", 8)])

    def test_duplicate_refs_are_deduplicated_keeping_first_snippet(self):
        text = "see PR #7 today. later, refer to PR #7 again."
        refs = gg.parse_refs_ctx(text, DEFAULT_REPO)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0][0], (DEFAULT_REPO, 7))
        self.assertIn("see PR #7 today", refs[0][1])

    def test_number_zero_is_excluded(self):
        # build_graph()/item_id() presumably never wants a #0; parse_refs_ctx filters r[1] > 0.
        self.assertEqual(gg.parse_refs("see PR #0 today", DEFAULT_REPO), [])

    def test_returned_context_is_the_sentence_around_the_reference(self):
        text = "Unrelated first paragraph here.\n\nThis paragraph mentions see PR #7 in the middle of it."
        refs = gg.parse_refs_ctx(text, DEFAULT_REPO)
        self.assertEqual(len(refs), 1)
        (repo, num), ctx = refs[0]
        self.assertEqual((repo, num), (DEFAULT_REPO, 7))
        self.assertIn("see PR #7", ctx)
        self.assertNotIn("Unrelated first paragraph", ctx)

    def test_markdown_table_and_heading_lines_are_each_their_own_paragraph(self):
        # parse_refs_ctx treats a hard-wrapped paragraph as one sentence source; lines starting with
        # "#", "|", "- ", "* ", "```" each break out on their own instead of joining the prose above.
        text = "prose before\n# heading mentioning PR #5\nprose after"
        refs = gg.parse_refs_ctx(text, DEFAULT_REPO)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0][0], (DEFAULT_REPO, 5))
        self.assertNotIn("prose before", refs[0][1])
        self.assertNotIn("prose after", refs[0][1])


class TestParseMentions(unittest.TestCase):
    def test_simple_mention(self):
        self.assertEqual(gg.parse_mentions("hi @alice, welcome"), ["alice"])

    def test_single_char_login_is_excluded(self):
        self.assertEqual(gg.parse_mentions("hi @a there"), [])

    def test_all_digit_login_is_excluded(self):
        self.assertEqual(gg.parse_mentions("see @12 there"), [])

    def test_login_with_a_digit_but_not_all_digits_is_included(self):
        self.assertEqual(gg.parse_mentions("see @1a2 there"), ["1a2"])

    def test_mention_inside_backticks_is_excluded(self):
        self.assertEqual(gg.parse_mentions("use `@code` as a placeholder"), [])

    def test_mention_inside_a_path_is_excluded(self):
        self.assertEqual(gg.parse_mentions("see path/@user for details"), [])

    def test_mention_preceded_by_a_word_char_is_excluded(self):
        self.assertEqual(gg.parse_mentions("word@user here"), [])

    def test_mention_preceded_by_punctuation_is_included(self):
        self.assertEqual(gg.parse_mentions("(@user) and [@other]"), ["user", "other"])

    def test_hyphenated_login_is_included(self):
        self.assertEqual(gg.parse_mentions("@User-Name ok"), ["User-Name"])

    def test_duplicate_mentions_are_deduplicated_case_insensitively_keeping_first_case(self):
        self.assertEqual(gg.parse_mentions("@Alice hi @alice again"), ["Alice"])

    def test_mentions_inside_a_fenced_code_block_are_ignored(self):
        text = "before @alice\n```\n@bob inside fence\n```\nafter @carol"
        self.assertEqual(gg.parse_mentions(text), ["alice", "carol"])

    def test_mention_longer_than_39_chars_never_matches(self):
        # GitHub logins cap at 39 chars; MENTION_RE's login group is capped at 1+38=39 chars, but with
        # no boundary the regex simply fails to match anywhere in an all-word-char run longer than
        # that, rather than matching a truncated 39-char prefix -- documented current behaviour.
        text = "@" + "x" * 47
        self.assertEqual(gg.parse_mentions(text), [])


if __name__ == "__main__":
    unittest.main()
