"""Proves the test harness itself (tests/env.py) is safe and correct before anything else relies on it:

  - make_home() builds an isolated HOME under the system temp dir, never the real one.
  - load_module() imports gitgraph with CACHE_DIR landing inside that temp HOME.
  - fixture_graph() reproduces the exact node/edge counts of tests/fixtures/repo.json.
  - the pipeline never spawns a `gh` process for this fixture (huge --max-age, fully self-consistent
    references — see tests/env.py's make_home() docstring).

Run directly: python3 -m unittest tests.test_env_selfcheck -v
"""
import os
import pwd
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as testenv  # noqa: E402

# Captured before any test mutates os.environ["HOME"], so we always know what the *real* HOME was.
# The developer's real home, read from the password database rather than $HOME: by the time this module
# is imported alongside the other test modules, one of them has already called load_module(), which
# repoints $HOME at a temp dir — so $HOME here would compare a temp home against itself.
_REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir


class TestMakeHomeIsolation(unittest.TestCase):
    def test_creates_a_fresh_dir_under_the_system_temp_dir(self):
        home = testenv.make_home()
        self.assertTrue(home.startswith(tempfile.gettempdir()),
                         f"{home!r} is not under {tempfile.gettempdir()!r}")
        self.assertNotEqual(home, _REAL_HOME)

    def test_two_calls_without_tmpdir_get_different_homes(self):
        self.assertNotEqual(testenv.make_home(), testenv.make_home())

    def test_honours_an_explicit_tmpdir(self):
        with tempfile.TemporaryDirectory() as d:
            home = testenv.make_home(tmpdir=d)
            self.assertEqual(home, d)
            self.assertTrue(os.path.isfile(os.path.join(d, ".cache", "gitgraph",
                                                          "items__test__repo__open.json")))

    def test_writes_only_inside_the_returned_home(self):
        home = testenv.make_home()
        cache_dir = os.path.join(home, ".cache", "gitgraph")
        names = sorted(os.listdir(cache_dir))
        self.assertIn("items__test__repo__open.json", names)
        # one stubs__*.json per external repo the fixture references (see repo.json's "stubs" section)
        self.assertIn("stubs__other__lib.json", names)
        self.assertIn("stubs__ghe.example.com__eng__tools.json", names)
        for name in names:
            self.assertTrue(os.path.realpath(os.path.join(cache_dir, name)).startswith(os.path.realpath(home)))


class TestLoadModule(unittest.TestCase):
    def test_gitgraph_imports_with_cache_dir_inside_the_temp_home(self):
        gg = testenv.load_module()
        home = os.environ["HOME"]
        self.assertNotEqual(home, _REAL_HOME)
        self.assertTrue(home.startswith(tempfile.gettempdir()))
        self.assertTrue(gg.CACHE_DIR.startswith(home), f"CACHE_DIR={gg.CACHE_DIR!r} home={home!r}")
        self.assertTrue(gg.CONFIG_PATH.startswith(home))
        self.assertEqual(gg.ME, [testenv.FIXTURE_ME])

    def test_repeated_calls_are_cheap_and_return_the_same_module(self):
        gg1 = testenv.load_module()
        gg2 = testenv.load_module()
        self.assertIs(gg1, gg2)


class TestFixtureGraph(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gg = testenv.load_module()
        cls.g = testenv.fixture_graph(cls.gg)

    def test_node_and_edge_counts(self):
        # Empirically verified against tests/fixtures/repo.json (see PLAN-tests.md Phase 1): 6 real items
        # (#1-#6) + 3 reference-only stubs (a same-repo crossref-only stub #7, plus other/lib#42 and
        # ghe.example.com/eng/tools#301, both pre-populated in stubs__*.json so no `gh` call is needed)
        # = 9 item nodes; 4 comment nodes; 2 mentioned persons (alice, carol).
        self.assertEqual(len(self.g.nodes), 15)
        self.assertEqual(len(self.g.edges), 14)
        kinds = {}
        for n in self.g.nodes.values():
            kinds[n.kind] = kinds.get(n.kind, 0) + 1
        self.assertEqual(kinds, {"item": 9, "comment": 4, "person": 2})

    def test_stub_items_were_resolved_without_a_gh_call(self):
        # title/state present means resolve_stubs()'s cache hit worked (a bare stub would have title=None)
        for nid in ("test/repo#7", "other/lib#42", "ghe.example.com/eng/tools#301"):
            n = self.g.nodes[nid]
            self.assertTrue(n.stub, nid)
            self.assertIsNotNone(n.title, f"{nid} was not resolved from the pre-populated stub cache")
            self.assertIsNotNone(n.state, nid)

    def test_closes_shadows_the_parsed_ref_for_the_same_pair(self):
        # PR #5's body says "Closes #4" (a parseable ref) *and* its closes[] field names #4: build_graph
        # must keep only the "closes" edge, per CLAUDE.md's "closes가 ref를 가리는 규칙".
        self.assertIn(("test/repo#5", "test/repo#4", "closes"), self.g.edges)
        self.assertNotIn(("test/repo#5", "test/repo#4", "ref"), self.g.edges)


class TestInboxSectionsNonEmpty(unittest.TestCase):
    """The fixture must exercise every one of the Inbox's turn/mention/waiting/stale buckets (Phase 1's
    hard requirement) — computed the same way the real TUI does, via Tui.home_sections(), without
    needing a curses screen (home_sections only touches self.g/self.cg/self.o/self.me/self.todo)."""

    @classmethod
    def setUpClass(cls):
        gg = testenv.load_module()
        g = testenv.fixture_graph(gg)
        cg = gg.apply_filters(g, "linked", True, True)
        t = gg.Tui.__new__(gg.Tui)
        t.g, t.cg, t.o, t.me, t.todo = g, cg, {"width": 60, "days": 7}, [testenv.FIXTURE_ME], []
        cls.sections = gg.Tui.home_sections(t)

    def test_buckets_are_not_empty(self):
        for key in ("turn", "mention", "waiting", "stale"):
            self.assertTrue(self.sections[key], f"Inbox section {key!r} is empty")


class TestNoGhProcessIsSpawned(unittest.TestCase):
    def test_fixture_graph_never_calls_subprocess_run_with_gh(self):
        gg = testenv.load_module()

        def guard(args, *a, **kw):
            cmd = args[0] if isinstance(args, (list, tuple)) else args
            self.fail(f"subprocess.run() was called with {args!r} — the fixture must never need `gh`")

        with mock.patch.object(gg.subprocess, "run", side_effect=guard):
            g = testenv.fixture_graph(gg)
        self.assertEqual(len(g.nodes), 15)  # build actually ran (not skipped) and finished normally


if __name__ == "__main__":
    unittest.main()
