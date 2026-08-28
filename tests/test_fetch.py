"""Unit tests for the fetch layer, driven by the scripted fake in tests/fakes/gh (FAKE_GH_MODE=script) —
no network and no real `gh`: account discovery, the multi-account fallback for private repos, retries on
transient network errors, the incremental refresh past --max-age, and the per-repo caches.

The fake answers `gh auth status`, `gh auth token` and `gh api graphql` from a JSON fixture the test
writes; its token format encodes which account made a call, which is how the fallback tests know which
account actually got the data.

Run: python3 -m unittest tests.test_fetch -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as testenv  # noqa: E402

gg = testenv.load_module()


def node(number, title="t", state="OPEN", body="", updated="2026-08-10T00:00:00Z", is_pr=False,
         comments=(), crossrefs=(), closes=()):
    """A GraphQL issue/PR node in the shape _norm_item() consumes."""
    n = {
        "number": number, "title": title, "state": state, "body": body,
        "createdAt": "2026-08-01T00:00:00Z", "updatedAt": updated,
        "url": f"https://github.com/test/repo/{'pull' if is_pr else 'issues'}/{number}",
        "author": {"login": "alice"}, "labels": {"nodes": []},
        "comments": {"totalCount": len(comments), "nodes": [
            {"databaseId": 1000 + i, "url": "u", "author": {"login": "bob"}, "body": c,
             "createdAt": "2026-08-02T00:00:00Z"} for i, c in enumerate(comments)]},
        "timelineItems": {"nodes": list(crossrefs)},
    }
    if is_pr:
        n["isDraft"] = False
        n["reviews"] = {"nodes": []}
        n["closingIssuesReferences"] = {"nodes": list(closes)}
    return n


class FetchCase(unittest.TestCase):
    """Scripted fake gh + a private CACHE_DIR holding the fixture repo."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gg-fetch-test-")
        home = testenv.make_home(self.tmp)
        self._old_cache = gg.CACHE_DIR
        gg.CACHE_DIR = os.path.join(home, ".cache", "gitgraph")
        self.fixture_path = os.path.join(self.tmp, "gh-fixture.json")
        self.gh_log = os.path.join(self.tmp, "gh.log")
        self._old_env = {k: os.environ.get(k) for k in
                         ("FAKE_GH_MODE", "FAKE_GH_FIXTURE", "FAKE_GH_LOG", "FAKE_GH_ACCOUNTS",
                          "FAKE_GH_DENY", "FAKE_GH_TRANSIENT_N")}
        os.environ.update(FAKE_GH_MODE="script", FAKE_GH_FIXTURE=self.fixture_path, FAKE_GH_LOG=self.gh_log)
        gg._acct_pref = None      # accounts.json lives in this test's CACHE_DIR, not the previous one
        gg._acct_hint = {}        # and the git-config guess is per run
        self.set_accounts({"github.com": ["alice"]})
        self.write_fixture({})
        self.addCleanup(self._restore)

    def _restore(self):
        gg.CACHE_DIR = self._old_cache
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        gg._accounts, gg._acct_pref, gg._acct_hint = None, None, {}
        gg._tokens.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_accounts(self, mapping):
        os.environ["FAKE_GH_ACCOUNTS"] = json.dumps(mapping)
        gg._accounts = None          # gh_accounts() caches per process
        gg._tokens.clear()

    def write_fixture(self, repos):
        with open(self.fixture_path, "w", encoding="utf-8") as f:
            json.dump({"repos": repos}, f)

    def gh_calls(self):
        if not os.path.exists(self.gh_log):
            return []
        with open(self.gh_log, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def cache_file(self, kind, repo, state=""):
        return gg._cache_path(kind, repo, state)


class TestAccounts(FetchCase):
    def test_accounts_are_parsed_active_first(self):
        self.set_accounts({"github.com": ["alice", "carol"], "ghe.example.com": ["dave"]})
        self.assertEqual(gg.gh_accounts("github.com"), ["alice", "carol"])
        self.assertEqual(gg.gh_accounts("ghe.example.com"), ["dave"])
        self.assertEqual(gg.gh_accounts()[0], "alice", "github.com comes first across hosts")

    def test_hosts_are_derived_from_the_same_output(self):
        self.set_accounts({"github.com": ["alice"], "ghe.example.com": ["dave"]})
        self.assertEqual(gg.gh_hosts(), {"github.com", "ghe.example.com"})

    def test_token_lookup_is_cached_per_host_and_user(self):
        self.assertEqual(gg.gh_token("alice", "github.com"), "TOKEN::github.com::alice")
        before = len(self.gh_calls())
        gg.gh_token("alice", "github.com")
        self.assertEqual(len(self.gh_calls()), before, "the second lookup comes from _tokens")

    def test_an_unknown_account_has_no_token(self):
        self.assertIsNone(gg.gh_token("nobody", "github.com"))


class TestGraphqlFallback(FetchCase):
    def setUp(self):
        super().setUp()
        self.write_fixture({"test/repo": {"issues": {"1": node(1, "hello")}, "pulls": {}}})

    def test_data_comes_back_for_the_active_account(self):
        d = gg.graphql(gg.Q_ISSUES, {"owner": "test", "name": "repo", "after": None, "states": ["OPEN"]})
        self.assertEqual(d["repository"]["issues"]["nodes"][0]["title"], "hello")
        self.assertEqual([c["account"] for c in self.gh_calls() if c.get("query_head")], ["alice"])

    def test_a_private_repo_falls_back_to_the_other_account(self):
        self.set_accounts({"github.com": ["alice", "carol"]})
        os.environ["FAKE_GH_DENY"] = json.dumps(["alice"])
        d = gg.graphql(gg.Q_ISSUES, {"owner": "test", "name": "repo", "after": None, "states": ["OPEN"]})
        self.assertEqual(d["repository"]["issues"]["nodes"][0]["title"], "hello")
        tried = [c["account"] for c in self.gh_calls() if c.get("query_head")]
        self.assertEqual(tried, ["alice", "carol"], "the active account is tried first, then the other")

    def test_the_working_account_is_remembered_for_later_queries(self):
        """0.18.0: the account that could see the repo is moved to the front of the *shared* account
        list (_prefer_account), so a private repo costs one gh process per query, not two. Before that,
        gh_accounts() handed out a fresh copy and the reorder was thrown away every time.
        """
        self.set_accounts({"github.com": ["alice", "carol"]})
        os.environ["FAKE_GH_DENY"] = json.dumps(["alice"])
        gg.graphql(gg.Q_ISSUES, {"owner": "test", "name": "repo", "after": None, "states": ["OPEN"]})
        self.assertEqual(gg.gh_accounts("github.com")[0], "carol")
        gg.graphql(gg.Q_ISSUES, {"owner": "test", "name": "repo", "after": None, "states": ["OPEN"]})
        tried = [c["account"] for c in self.gh_calls() if c.get("query_head")]
        self.assertEqual(tried, ["alice", "carol", "carol"], "the second query goes straight to carol")

    def test_the_working_account_is_remembered_across_runs(self):
        """The active gh account is often not the one a private repo is shared with, so which account
        could see it is written to accounts.json — a new process then starts with that one instead of
        spending a round trip rediscovering it (measured: 2 wasted queries per cold start)."""
        self.set_accounts({"github.com": ["alice", "carol"]})
        os.environ["FAKE_GH_DENY"] = json.dumps(["alice"])
        gg.graphql(gg.Q_ISSUES, {"owner": "test", "name": "repo", "after": None, "states": ["OPEN"]})
        path = os.path.join(gg.CACHE_DIR, "accounts.json")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"github.com": {"test/repo": "carol"}})

        gg._accounts, gg._acct_pref = None, None       # a fresh process reads it back
        gg._tokens.clear()
        before = len(self.gh_calls())
        gg.graphql(gg.Q_ISSUES, {"owner": "test", "name": "repo", "after": None, "states": ["OPEN"]})
        tried = [c["account"] for c in self.gh_calls()[before:] if c.get("query_head")]
        self.assertEqual(tried, ["carol"], "the denied account must not be tried again")

    def test_the_memory_is_per_repo(self):
        self.set_accounts({"github.com": ["alice", "carol"]})
        os.environ["FAKE_GH_DENY"] = json.dumps(["alice"])
        gg.graphql(gg.Q_ISSUES, {"owner": "test", "name": "repo", "after": None, "states": ["OPEN"]})
        self.write_fixture({"test/repo": {"issues": {"1": node(1, "hello")}, "pulls": {}},
                            "other/side": {"issues": {"2": node(2, "hi")}, "pulls": {}}})
        os.environ["FAKE_GH_DENY"] = json.dumps(["carol"])      # the other repo is the other way round
        gg.graphql(gg.Q_ISSUES, {"owner": "other", "name": "side", "after": None, "states": ["OPEN"]})
        with open(os.path.join(gg.CACHE_DIR, "accounts.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["github.com"], {"test/repo": "carol", "other/side": "alice"})

    def test_a_stale_memory_falls_back_and_is_corrected(self):
        path = os.path.join(gg.CACHE_DIR, "accounts.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"github.com": {"test/repo": "carol"}}, f)
        gg._acct_pref = None
        self.set_accounts({"github.com": ["alice", "carol"]})
        os.environ["FAKE_GH_DENY"] = json.dumps(["carol"])      # access changed hands
        gg.graphql(gg.Q_ISSUES, {"owner": "test", "name": "repo", "after": None, "states": ["OPEN"]})
        tried = [c["account"] for c in self.gh_calls() if c.get("query_head")]
        self.assertEqual(tried, ["carol", "alice"], "the remembered one first, then the fallback")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["github.com"]["test/repo"], "alice", "corrected for next time")

    def test_not_found_everywhere_names_the_host_and_the_accounts(self):
        self.set_accounts({"github.com": ["alice", "carol"]})
        os.environ["FAKE_GH_DENY"] = json.dumps(["alice", "carol"])
        with self.assertRaises(gg.GhError) as cm:
            gg.graphql(gg.Q_ISSUES, {"owner": "test", "name": "repo", "after": None, "states": ["OPEN"]})
        msg = str(cm.exception)
        self.assertIn("github.com", msg)
        self.assertIn("alice", msg)
        self.assertIn("carol", msg)


class TestGitAccountHint(FetchCase):
    """Which gh account a checkout uses is usually written in its own git config — gg reads it so the
    very first run (before accounts.json exists) does not spend a round trip on the wrong account."""

    def git_repo(self, remote=None, **config):
        """A real `git init` repo (git only reads config from something it recognises as a repo), in its
        own directory so a discovery walk of one test's repo never wanders into another's."""
        d = os.path.join(self.tmp, f"co{len(os.listdir(self.tmp))}", "checkout")
        os.makedirs(d)
        subprocess.run(["git", "init", "-q", d], check=True, capture_output=True)
        if remote:
            subprocess.run(["git", "-C", d, "remote", "add", "origin", remote], check=True, capture_output=True)
        for key, value in config.items():
            subprocess.run(["git", "-C", d, "config", key.replace("__", "."), value],
                           check=True, capture_output=True)
        return d

    def setUp(self):
        super().setUp()
        self.set_accounts({"github.com": ["alice", "carol"]})

    def test_credential_username(self):
        d = self.git_repo(**{"credential.https://github.com.username": "carol"})
        self.assertEqual(gg.git_account_hint(d, "github.com"), "carol")

    def test_a_gh_credential_helper_that_names_an_account(self):
        # what `gh auth setup-git -u LOGIN` writes into a checkout
        helper = '!f() { test "$1" = get && { echo username=carol; echo "password=$(gh auth token -u carol)"; }; }; f'
        d = self.git_repo(**{"credential.https://github.com.helper": helper})
        self.assertEqual(gg.git_account_hint(d, "github.com"), "carol")

    def test_a_user_in_the_remote_url(self):
        d = self.git_repo()
        self.assertEqual(gg.git_account_hint(d, "github.com", "https://carol@github.com/test/repo.git"), "carol")

    def test_nothing_to_go_on(self):
        d = self.git_repo()
        self.assertIsNone(gg.git_account_hint(d, "github.com"))

    def test_an_account_gh_does_not_know_is_ignored(self):
        d = self.git_repo(**{"credential.https://github.com.username": "stranger"})
        self.assertIsNone(gg.git_account_hint(d, "github.com"))

    def test_a_hint_from_another_host_does_not_leak(self):
        d = self.git_repo(**{"credential.https://ghe.example.com.username": "carol"})
        self.assertIsNone(gg.git_account_hint(d, "github.com"))

    def test_the_hint_is_used_for_the_first_query(self):
        d = self.git_repo("https://github.com/test/repo.git",
                          **{"credential.https://github.com.username": "carol"})
        self.write_fixture({"test/repo": {"issues": {"1": node(1, "hello")}, "pulls": {}}})
        os.environ["FAKE_GH_DENY"] = json.dumps(["alice"])
        gg.seed_account_hints(["test/repo"], d)
        gg.graphql(gg.Q_ISSUES, {"owner": "test", "name": "repo", "after": None, "states": ["OPEN"]})
        tried = [c["account"] for c in self.gh_calls() if c.get("query_head")]
        self.assertEqual(tried, ["carol"], "the denied active account must not be tried at all")

    def test_the_hint_also_covers_repos_it_references(self):
        d = self.git_repo("https://github.com/test/repo.git",
                          **{"credential.https://github.com.username": "carol"})
        gg.seed_account_hints(["test/repo"], d)
        self.write_fixture({"other/side": {"issues": {"9": node(9, "x")}, "pulls": {}}})
        os.environ["FAKE_GH_DENY"] = json.dumps(["alice"])
        gg.graphql(gg.Q_ISSUES, {"owner": "other", "name": "side", "after": None, "states": ["OPEN"]})
        tried = [c["account"] for c in self.gh_calls() if c.get("query_head")]
        self.assertEqual(tried, ["carol"], "a stub repo of the same host gets the same first guess")

    def test_a_checkout_below_the_current_directory_is_found_too(self):
        """Standing next to the checkout, not in it: discovery already walks a couple of levels down,
        so the hint comes from there as well."""
        d = self.git_repo("https://github.com/test/repo.git",
                          **{"credential.https://github.com.username": "carol"})
        parent = os.path.dirname(d)                       # the checkout is one level below this
        gg._acct_pref, gg._acct_hint = None, {}
        gg.seed_account_hints(["test/repo"], parent)
        self.assertEqual(gg._pref_map()["github.com"]["test/repo"], "carol")

    def test_a_verified_memory_beats_the_git_hint(self):
        with open(os.path.join(gg.CACHE_DIR, "accounts.json"), "w", encoding="utf-8") as f:
            json.dump({"github.com": {"test/repo": "alice"}}, f)
        gg._acct_pref = None
        d = self.git_repo("https://github.com/test/repo.git",
                          **{"credential.https://github.com.username": "carol"})
        gg.seed_account_hints(["test/repo"], d)
        self.assertEqual(gg._pref_map()["github.com"]["test/repo"], "alice")

    def test_a_directory_with_no_checkout_is_skipped(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        gg.seed_account_hints(["test/repo"], empty)         # nothing to discover: must not raise
        self.assertNotIn("test/repo", gg._pref_map().get("github.com", {}))


class TestRetries(FetchCase):
    def setUp(self):
        super().setUp()
        self.write_fixture({"test/repo": {"issues": {"1": node(1, "hello")}, "pulls": {}}})
        patcher = mock.patch.object(time, "sleep")      # the backoff is 2s, 4s, 8s — not in a unit test
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_transient_error_is_retried_and_then_succeeds(self):
        os.environ["FAKE_GH_TRANSIENT_N"] = "2"
        d = gg.graphql(gg.Q_ISSUES, {"owner": "test", "name": "repo", "after": None, "states": ["OPEN"]})
        self.assertEqual(d["repository"]["issues"]["nodes"][0]["title"], "hello")
        self.assertEqual(len([c for c in self.gh_calls() if c.get("query_head")]), 3, "2 failures + 1 success")
        self.assertEqual(self.sleep.call_count, 2)

    def test_giving_up_explains_what_to_check(self):
        os.environ["FAKE_GH_TRANSIENT_N"] = str(gg.GH_RETRIES + 5)
        with self.assertRaises(gg.GhError) as cm:
            gg.graphql(gg.Q_ISSUES, {"owner": "test", "name": "repo", "after": None, "states": ["OPEN"]})
        msg = str(cm.exception)
        self.assertIn("cannot reach github.com", msg)
        self.assertIn("HTTPS_PROXY", msg)
        self.assertEqual(len([c for c in self.gh_calls() if c.get("query_head")]), gg.GH_RETRIES + 1)


class TestListAndFetch(FetchCase):
    def setUp(self):
        super().setUp()
        self.write_fixture({"test/repo": {
            "issues": {"1": node(1, "issue one", updated="2026-08-10T00:00:00Z"),
                       "2": node(2, "issue two", updated="2026-08-11T00:00:00Z")},
            "pulls": {"5": node(5, "pr five", is_pr=True, updated="2026-08-12T00:00:00Z")}}})

    def test_list_open_is_a_light_number_and_timestamp_query(self):
        listing = gg.list_open("test/repo")
        self.assertEqual(listing, {(False, 1): "2026-08-10T00:00:00Z",
                                   (False, 2): "2026-08-11T00:00:00Z",
                                   (True, 5): "2026-08-12T00:00:00Z"})

    def test_fetch_items_normalises_bodies_and_comments(self):
        self.write_fixture({"test/repo": {
            "issues": {"1": node(1, "issue one", body="see #5", comments=["first", "second"])}, "pulls": {}}})
        items = gg.fetch_items("test/repo", False, [1])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "issue one")
        self.assertEqual(items[0]["body"], "see #5")
        self.assertEqual([c["body"] for c in items[0]["comments"]], ["first", "second"])
        self.assertEqual(items[0]["repo"], "test/repo")

    def test_a_missing_number_is_simply_absent(self):
        self.assertEqual(gg.fetch_items("test/repo", False, [999]), [])

    def test_fetch_repo_returns_issues_and_prs(self):
        items = gg.fetch_repo("test/repo", "open")
        self.assertEqual({(it["is_pr"], it["number"]) for it in items}, {(False, 1), (False, 2), (True, 5)})


class TestRefreshItems(FetchCase):
    def setUp(self):
        super().setUp()
        self.write_fixture({"test/repo": {
            "issues": {"1": node(1, "issue one", updated="2026-08-20T00:00:00Z"),   # moved
                       "3": node(3, "brand new", updated="2026-08-20T00:00:00Z")},  # new
            "pulls": {}}})
        self.cached = [
            {"repo": "test/repo", "number": 1, "is_pr": False, "title": "old title", "state": "OPEN",
             "draft": False, "body": "", "created": "2026-08-01T00:00:00Z", "updated": "2026-08-10T00:00:00Z",
             "url": "u", "author": "alice", "labels": [], "comments": [], "comments_total": 0,
             "crossrefs": [], "closes": []},
            {"repo": "test/repo", "number": 2, "is_pr": False, "title": "closed since", "state": "OPEN",
             "draft": False, "body": "", "created": "2026-08-01T00:00:00Z", "updated": "2026-08-11T00:00:00Z",
             "url": "u", "author": "alice", "labels": [], "comments": [], "comments_total": 0,
             "crossrefs": [], "closes": []},
        ]

    def test_only_changed_and_new_items_are_refetched(self):
        items, changed, dropped = gg.refresh_items("test/repo", self.cached)
        self.assertEqual((changed, dropped), (2, 1), "#1 moved and #3 is new; #2 is no longer open")
        by_num = {it["number"]: it for it in items}
        self.assertEqual(set(by_num), {1, 3})
        self.assertEqual(by_num[1]["title"], "issue one", "the changed item was refetched")

    def test_an_unchanged_item_is_kept_from_the_cache_without_a_fetch(self):
        self.write_fixture({"test/repo": {"issues": {"1": node(1, "issue one", updated="2026-08-10T00:00:00Z")},
                                          "pulls": {}}})
        items, changed, dropped = gg.refresh_items("test/repo", self.cached[:1])
        self.assertEqual((changed, dropped), (0, 0))
        self.assertEqual(items[0]["title"], "old title", "kept verbatim from the cache")


class TestLoadItems(FetchCase):
    def setUp(self):
        super().setUp()
        self.write_fixture({"test/repo": {"issues": {"1": node(1, "from github")}, "pulls": {}}})

    def test_a_fresh_cache_is_used_without_any_gh_call(self):
        items, fetched_at = gg.load_items(testenv.FIXTURE_REPO, "open", testenv.FIXTURE_MAX_AGE_MIN)
        self.assertTrue(items)
        self.assertEqual(self.gh_calls(), [], "a fresh cache must not reach gh at all")

    def test_past_max_age_the_incremental_path_runs_and_rewrites_the_cache(self):
        path = self.cache_file("items", "test/repo", "open")
        with open(path, "w") as f:
            json.dump({"fetched_at": time.time() - 3600, "repo": "test/repo", "state": "open",
                       "items": []}, f)
        items, fetched_at = gg.load_items("test/repo", "open", 1)
        self.assertEqual([it["title"] for it in items], ["from github"])
        self.assertTrue(self.gh_calls())
        with open(path) as f:
            self.assertEqual(len(json.load(f)["items"]), 1, "the refreshed items are written back")

    def test_refresh_forces_a_full_fetch_even_with_a_fresh_cache(self):
        gg.load_items(testenv.FIXTURE_REPO, "open", testenv.FIXTURE_MAX_AGE_MIN)
        self.write_fixture({testenv.FIXTURE_REPO: {"issues": {"1": node(1, "replaced")}, "pulls": {}}})
        items, _ = gg.load_items(testenv.FIXTURE_REPO, "open", testenv.FIXTURE_MAX_AGE_MIN, refresh=True)
        self.assertEqual([it["title"] for it in items], ["replaced"])

    def test_the_cache_file_is_private(self):
        gg.load_items("test/repo", "open", 0, refresh=True)
        path = self.cache_file("items", "test/repo", "open")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


class TestResolveStubs(FetchCase):
    def setUp(self):
        super().setUp()
        self.write_fixture({"other/side": {"issues": {"42": node(42, "referenced issue")},
                                           "pulls": {"43": node(43, "referenced pr", is_pr=True)}}})

    def test_titles_and_kinds_come_back_and_are_cached(self):
        got = gg.resolve_stubs("other/side", [42, 43], 60)
        self.assertEqual(got[42]["title"], "referenced issue")
        self.assertTrue(got[43]["is_pr"])
        before = len(self.gh_calls())
        again = gg.resolve_stubs("other/side", [42], 60)
        self.assertEqual(again[42]["title"], "referenced issue")
        self.assertEqual(len(self.gh_calls()), before, "the stub cache serves the second lookup")

    def test_a_number_that_does_not_exist_is_marked_missing(self):
        got = gg.resolve_stubs("other/side", [999], 60)
        self.assertTrue(got[999].get("missing"))


if __name__ == "__main__":
    unittest.main()
