"""Unit tests for the local-data layer (gitgraph.py "local data" section, added in 0.9.1): what gg keeps
under ~/.cache/gitgraph, how `gg cache` lists and clears it, and what cache_hygiene() deletes or trims at
start-up. Caches hold private-repo bodies, so the permission bits (dir 0700, files 0600) are part of the
contract and are asserted here too.

Safety: every test either runs in a subprocess with an isolated HOME (env.child_env) or points the
module's CACHE_DIR / CONFIG_PATH at a throwaway directory for the duration of the test — cache_hygiene()
DELETES files, and it must never be pointed at the developer's real cache. Each test asserts that first.

Run: python3 -m unittest tests.test_cache -v
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as testenv  # noqa: E402

gg = testenv.load_module()

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GITGRAPH = os.path.join(REPO_ROOT, "gitgraph.py")


class CacheDirCase(unittest.TestCase):
    """Points gg.CACHE_DIR / gg.CONFIG_PATH at a fresh throwaway dir for the duration of one test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gg-cache-test-")
        self.cache = os.path.join(self.tmp, ".cache", "gitgraph")
        os.makedirs(self.cache)
        self._old = (gg.CACHE_DIR, gg.CONFIG_PATH)
        gg.CACHE_DIR = self.cache
        gg.CONFIG_PATH = os.path.join(self.tmp, ".config", "gitgraph", "config.json")
        self.addCleanup(self._restore)
        # the point of the whole file: never operate on the real cache
        self.assertTrue(gg.CACHE_DIR.startswith(tempfile.gettempdir()))
        self.assertNotIn(os.path.expanduser("~/.cache/gitgraph"), gg.CACHE_DIR)

    def _restore(self):
        gg.CACHE_DIR, gg.CONFIG_PATH = self._old
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, data="x", age_days=0):
        path = os.path.join(self.cache, name)
        with open(path, "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        if age_days:
            t = time.time() - age_days * 86400
            os.utime(path, (t, t))
        return path


class TestCacheKind(unittest.TestCase):
    def test_every_documented_prefix_is_classified(self):
        for prefix, (group, purpose) in gg.CACHE_KINDS.items():
            self.assertEqual(gg.cache_kind(prefix + "test__repo.json")[0], group)
            self.assertTrue(purpose, f"{prefix} has no purpose text for `gg cache`")

    def test_repo_data_is_the_items_group(self):
        self.assertEqual(gg.cache_kind("items__test__repo__open.json")[0], "items")
        self.assertEqual(gg.cache_kind("stubs__test__repo.json")[0], "items")

    def test_an_unknown_file_is_reported_as_other_not_a_crash(self):
        self.assertEqual(gg.cache_kind("mystery.dat"), ("other", ""))


class TestFmtBytes(unittest.TestCase):
    def test_units(self):
        self.assertEqual(gg.fmt_bytes(0), "0B")
        self.assertEqual(gg.fmt_bytes(999), "999B")
        self.assertEqual(gg.fmt_bytes(1024), "1.0K")
        self.assertEqual(gg.fmt_bytes(1024 * 1024 * 3), "3.0M")
        self.assertEqual(gg.fmt_bytes(1024 ** 3 * 2), "2.0G")


class TestCacheFiles(CacheDirCase):
    def test_lists_files_with_size_group_and_purpose(self):
        self.write("items__test__repo__open.json", "{}")
        self.write("tui.log", "log line\n")
        os.makedirs(os.path.join(self.cache, "subdir"))       # directories are ignored
        listed = {f[0]: f for f in gg.cache_files()}
        self.assertEqual(set(listed), {"items__test__repo__open.json", "tui.log"})
        self.assertEqual(listed["tui.log"][5], "logs")
        self.assertEqual(listed["items__test__repo__open.json"][5], "items")

    def test_missing_cache_dir_is_empty_not_an_error(self):
        gg.CACHE_DIR = os.path.join(self.tmp, "does-not-exist")
        self.assertEqual(gg.cache_files(), [])


class TestSecure(CacheDirCase):
    def test_file_becomes_0600(self):
        p = self.write("state.json", "{}")
        os.chmod(p, 0o644)
        gg.secure(p)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_missing_file_is_ignored(self):
        gg.secure(os.path.join(self.cache, "nope.json"))       # must not raise


class TestHygiene(CacheDirCase):
    def test_directory_and_files_end_up_private(self):
        p = self.write("items__test__repo__open.json", "{}")
        os.chmod(p, 0o644)
        os.chmod(self.cache, 0o755)
        gg.cache_hygiene()
        self.assertEqual(os.stat(self.cache).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_stale_repo_data_is_deleted_and_fresh_data_survives(self):
        old = self.write("items__old__repo__open.json", "{}", age_days=gg.ITEMS_KEEP_DAYS + 1)
        stale_stub = self.write("stubs__old__repo.json", "{}", age_days=gg.ITEMS_KEEP_DAYS + 1)
        fresh = self.write("items__new__repo__open.json", "{}")
        gg.cache_hygiene()
        self.assertFalse(os.path.exists(old))
        self.assertFalse(os.path.exists(stale_stub))
        self.assertTrue(os.path.exists(fresh))

    def test_only_the_items_group_expires(self):
        ai = self.write("summaries.json", "{}", age_days=gg.ITEMS_KEEP_DAYS + 5)
        log = self.write("tui.log", "old\n", age_days=gg.ITEMS_KEEP_DAYS + 5)
        gg.cache_hygiene()
        self.assertTrue(os.path.exists(ai), "AI caches are capped by size, never by age")
        self.assertTrue(os.path.exists(log))

    def test_oversized_log_is_cut_back_to_its_tail(self):
        path = self.write("tui.log", "A" * (gg.LOG_MAX + 200_000))
        gg.cache_hygiene()
        size = os.path.getsize(path)
        self.assertLessEqual(size, gg.LOG_MAX // 2)
        self.assertGreater(size, 0)

    def test_oversized_ai_cache_keeps_the_newest_entries(self):
        # trimmed only when BOTH the file is > 2MB and it holds more than AI_MAX_ENTRIES entries
        n = gg.AI_MAX_ENTRIES + 50
        d = {f"k{i:06d}": "v" * 90 for i in range(n)}
        path = self.write("translations.json", d)
        self.assertGreater(os.path.getsize(path), 2_000_000)
        gg.cache_hygiene()
        with open(path) as f:
            kept = json.load(f)
        self.assertEqual(len(kept), gg.AI_MAX_ENTRIES)
        self.assertNotIn("k000000", kept, "oldest entries are the ones dropped")
        self.assertIn(f"k{n - 1:06d}", kept)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_a_small_ai_cache_is_left_alone(self):
        path = self.write("summaries.json", {"a": "1"})
        gg.cache_hygiene()
        with open(path) as f:
            self.assertEqual(json.load(f), {"a": "1"})


class TestCacheCli(unittest.TestCase):
    """`gg cache` / `gg cache clear …` through a subprocess with an isolated HOME."""

    def setUp(self):
        self.home = testenv.make_home()
        self.cache = os.path.join(self.home, ".cache", "gitgraph")
        self.assertTrue(os.path.isdir(self.cache))

    def run_gg(self, *args):
        r = subprocess.run([sys.executable, GITGRAPH, *args], env=testenv.child_env(self.home),
                           capture_output=True, text=True, timeout=120, cwd=self.home)
        return r

    def test_cache_lists_the_fixture_repo_data(self):
        r = self.run_gg("cache")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("items__test__repo__open.json", r.stdout)
        self.assertIn("items ", r.stdout)
        self.assertIn("clear: gg cache clear all | items | ai | logs | owner/name", r.stdout)

    def test_clear_by_repo_name_removes_only_that_repo(self):
        with open(os.path.join(self.cache, "summaries.json"), "w") as f:
            f.write("{}")
        r = self.run_gg("cache", "clear", "test/repo")
        self.assertEqual(r.returncode, 0, r.stderr)
        left = os.listdir(self.cache)
        self.assertFalse([n for n in left if n.startswith("items__test__repo")])
        self.assertIn("summaries.json", left)

    def test_clear_ai_removes_only_the_ai_group(self):
        for n in ("summaries.json", "translations.json"):
            with open(os.path.join(self.cache, n), "w") as f:
                f.write("{}")
        self.run_gg("cache", "clear", "ai")
        left = os.listdir(self.cache)
        self.assertNotIn("summaries.json", left)
        self.assertTrue([n for n in left if n.startswith("items__")], "repo data must survive")

    def test_clear_all_empties_the_directory(self):
        self.run_gg("cache", "clear", "all")
        self.assertEqual([n for n in os.listdir(self.cache) if os.path.isfile(os.path.join(self.cache, n))], [])

    def test_the_real_home_is_never_touched(self):
        real = os.path.expanduser("~/.cache/gitgraph")
        before = sorted(os.listdir(real)) if os.path.isdir(real) else None
        self.run_gg("cache")
        after = sorted(os.listdir(real)) if os.path.isdir(real) else None
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
