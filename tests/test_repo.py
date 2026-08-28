"""Unit tests for gg's repo-identity helpers (CLAUDE.md "Repo discovery" -- every repo id carries its
host through the whole pipeline): split_repo, qualify, repo_host, make_repo, the remote-URL regex
_REMOTE_RE, and unfork().

No network / `gh` calls: unfork() is the only one of these that shells out (through parent_repo() ->
graphql() -> `gh api graphql`), so its tests monkeypatch gg.parent_repo directly rather than touching
the network, per the task's instruction ("either monkeypatch that call or skip that function").

Run: python3 -m unittest tests.test_repo -v
"""
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as testenv  # noqa: E402

gg = testenv.load_module()


class TestSplitRepo(unittest.TestCase):
    def test_bare_owner_name_is_github_com(self):
        self.assertEqual(gg.split_repo("owner/name"), ("github.com", "owner", "name"))

    def test_host_owner_name_form(self):
        self.assertEqual(gg.split_repo("ghe.example.com/owner/name"),
                          ("ghe.example.com", "owner", "name"))

    def test_name_containing_a_slash_stays_joined(self):
        # split_repo joins everything past the second "/" back into `name` -- a name segment can
        # itself contain slashes (this mirrors make_repo's host/owner/name join for Enterprise repos).
        self.assertEqual(gg.split_repo("ghe.example.com/owner/name/extra"),
                          ("ghe.example.com", "owner", "name/extra"))


class TestMakeRepo(unittest.TestCase):
    def test_github_com_host_is_dropped(self):
        self.assertEqual(gg.make_repo("github.com", "owner", "name"), "owner/name")

    def test_enterprise_host_is_kept(self):
        self.assertEqual(gg.make_repo("ghe.example.com", "owner", "name"),
                          "ghe.example.com/owner/name")


class TestRoundTrip(unittest.TestCase):
    def test_split_then_make_round_trips_for_github_com(self):
        for repo in ("owner/name", "a/b"):
            host, owner, name = gg.split_repo(repo)
            self.assertEqual(gg.make_repo(host, owner, name), repo)

    def test_split_then_make_round_trips_for_enterprise_host(self):
        for repo in ("ghe.example.com/owner/name", "ghe.example.com/eng/tools"):
            host, owner, name = gg.split_repo(repo)
            self.assertEqual(gg.make_repo(host, owner, name), repo)


class TestQualify(unittest.TestCase):
    def test_bare_repo_on_github_com_host_stays_bare(self):
        self.assertEqual(gg.qualify("owner/name", "github.com"), "owner/name")

    def test_bare_repo_on_enterprise_host_gets_qualified(self):
        self.assertEqual(gg.qualify("owner/name", "ghe.example.com"),
                          "ghe.example.com/owner/name")

    def test_already_qualified_repo_is_unchanged(self):
        self.assertEqual(gg.qualify("ghe.example.com/owner/name", "ghe.example.com"),
                          "ghe.example.com/owner/name")

    def test_already_qualified_repo_unchanged_even_against_a_different_host(self):
        # qualify only checks whether `repo` already has >=3 segments; it does not cross-check
        # that segment against the `host` argument.
        self.assertEqual(gg.qualify("other.example.com/owner/name", "ghe.example.com"),
                          "other.example.com/owner/name")


class TestRepoHost(unittest.TestCase):
    def test_bare_repo_is_github_com(self):
        self.assertEqual(gg.repo_host("owner/name"), "github.com")

    def test_qualified_repo_returns_its_host(self):
        self.assertEqual(gg.repo_host("ghe.example.com/owner/name"), "ghe.example.com")


class TestRemoteRe(unittest.TestCase):
    def _groups(self, url):
        m = gg._REMOTE_RE.match(url)
        return m.groupdict() if m else None

    def test_https_url(self):
        self.assertEqual(self._groups("https://github.com/owner/name.git"),
                          {"host": "github.com", "owner": "owner", "name": "name"})

    def test_https_url_without_dot_git_suffix(self):
        self.assertEqual(self._groups("https://github.com/owner/name"),
                          {"host": "github.com", "owner": "owner", "name": "name"})

    def test_https_url_with_trailing_slash(self):
        self.assertEqual(self._groups("https://github.com/owner/name/"),
                          {"host": "github.com", "owner": "owner", "name": "name"})

    def test_https_url_with_embedded_credentials(self):
        self.assertEqual(self._groups("https://x-access-token@github.com/owner/name.git"),
                          {"host": "github.com", "owner": "owner", "name": "name"})

    def test_ssh_scp_style_url(self):
        self.assertEqual(self._groups("git@github.com:owner/name.git"),
                          {"host": "github.com", "owner": "owner", "name": "name"})

    def test_ssh_scp_style_url_without_dot_git(self):
        self.assertEqual(self._groups("git@github.com:owner/name"),
                          {"host": "github.com", "owner": "owner", "name": "name"})

    def test_ssh_url_scheme(self):
        self.assertEqual(self._groups("ssh://git@github.com/owner/name.git"),
                          {"host": "github.com", "owner": "owner", "name": "name"})

    def test_ssh_url_scheme_with_port(self):
        self.assertEqual(self._groups("ssh://git@ghe.example.com:2222/owner/name.git"),
                          {"host": "ghe.example.com", "owner": "owner", "name": "name"})

    def test_ssh_config_alias_host(self):
        # An alias like "gh-work" (resolved to a real hostname elsewhere, by resolve_ssh_alias() via
        # `ssh -G`, not by this regex) is captured verbatim as the "host" group.
        self.assertEqual(self._groups("git@gh-work:owner/name.git"),
                          {"host": "gh-work", "owner": "owner", "name": "name"})

    def test_enterprise_https_url(self):
        self.assertEqual(self._groups("https://ghe.example.com/owner/name.git"),
                          {"host": "ghe.example.com", "owner": "owner", "name": "name"})

    def test_git_protocol_scheme_is_not_matched(self):
        # git:// is not one of the schemes _REMOTE_RE understands (only https://, ssh://, and the
        # scp-like user@host: form) -- current behaviour, not something this suite fixes.
        self.assertIsNone(self._groups("git://github.com/owner/name.git"))

    def test_bare_owner_name_with_no_host_is_not_matched(self):
        self.assertIsNone(self._groups("owner/name"))

    def test_unrelated_scheme_is_not_matched(self):
        self.assertIsNone(self._groups("ftp://example.com/owner/name"))

    def test_owner_or_name_with_dots_and_dashes(self):
        self.assertEqual(self._groups("https://github.com/my-org/my.repo.name.git"),
                          {"host": "github.com", "owner": "my-org", "name": "my.repo.name"})


class TestUnfork(unittest.TestCase):
    """unfork() calls parent_repo(), which shells out to `gh api graphql` -- monkeypatched here so
    these tests need no network or `gh` (see module docstring)."""

    def test_non_fork_is_returned_unchanged(self):
        with mock.patch.object(gg, "parent_repo", return_value=None):
            self.assertEqual(gg.unfork(["owner/name"]), ["owner/name"])

    def test_fork_is_replaced_by_its_parent(self):
        with mock.patch.object(gg, "parent_repo", side_effect=lambda r: {"fork/a": "upstream/a"}.get(r)):
            self.assertEqual(gg.unfork(["fork/a"]), ["upstream/a"])

    def test_mix_of_fork_and_non_fork_preserves_order(self):
        parents = {"fork/a": "upstream/a"}
        with mock.patch.object(gg, "parent_repo", side_effect=lambda r: parents.get(r)):
            self.assertEqual(gg.unfork(["other/x", "fork/a"]), ["other/x", "upstream/a"])

    def test_duplicate_parent_is_not_added_twice(self):
        with mock.patch.object(gg, "parent_repo", return_value="upstream/a"):
            self.assertEqual(gg.unfork(["fork/a", "fork/b"]).count("upstream/a"), 1)

    def test_two_forks_of_one_parent_collapse_into_that_parent(self):
        # 0.18.0: the second fork used to survive under its own name, because its parent was already
        # in the output list and it fell through to the "not a fork" branch.
        with mock.patch.object(gg, "parent_repo", return_value="upstream/a"):
            self.assertEqual(gg.unfork(["fork/a", "fork/b"]), ["upstream/a"])

    def test_already_seen_repo_is_not_duplicated(self):
        with mock.patch.object(gg, "parent_repo", return_value=None):
            self.assertEqual(gg.unfork(["owner/name", "owner/name"]), ["owner/name"])



class TestRemoteUrls(unittest.TestCase):
    """github_remotes() reads the *configured* URL: `git remote -v` prints it after applying any
    url.<base>.insteadOf rewrite, which would hide a GitHub remote behind an internal mirror path."""

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="gg-remote-test-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}
        subprocess.run(["git", "init", "-q", self.tmp], check=True, capture_output=True, env=self.env)

    def git(self, *a):
        subprocess.run(["git", "-C", self.tmp] + list(a), check=True, capture_output=True, env=self.env)

    def test_remote_urls_are_the_configured_ones(self):
        self.git("remote", "add", "origin", "https://github.com/me/fork.git")
        self.git("remote", "add", "upstream", "git@github.com:them/proj.git")
        self.assertEqual(dict(gg.remote_urls(self.tmp)),
                         {"origin": "https://github.com/me/fork.git",
                          "upstream": "git@github.com:them/proj.git"})

    def test_a_rewritten_remote_is_still_recognised(self):
        self.git("remote", "add", "origin", "https://github.com/them/proj.git")
        self.git("config", "url./srv/mirror/proj.git.insteadOf", "https://github.com/them/proj.git")
        self.assertEqual(gg.github_remotes(self.tmp), [(0, "them/proj")])

    def test_a_directory_that_is_not_a_repo_gives_nothing(self):
        self.assertEqual(gg.remote_urls(os.path.join(self.tmp, "nope")), [])

if __name__ == "__main__":
    unittest.main()
