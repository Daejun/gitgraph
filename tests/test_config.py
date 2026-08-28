"""Unit tests for the settings layer: CONFIG_KEYS as the single source of truth (CLAUDE.md: "a new
setting is one entry there, read with cfg(key)") and the precedence CLI option > GITGRAPH_* env >
~/.config/gitgraph/config.json > default.

Precedence is checked through subprocesses, not in-process: gitgraph resolves CONFIG / ENV_REPOS / ME /
THEME at import time, so a value can only be varied by starting a new process with a different
environment. Each run gets its own isolated HOME from env.child_env().

Run: python3 -m unittest tests.test_config -v
"""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as testenv  # noqa: E402

gg = testenv.load_module()

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GITGRAPH = os.path.join(REPO_ROOT, "gitgraph.py")


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.home = testenv.make_home()
        self.config_path = os.path.join(self.home, ".config", "gitgraph", "config.json")

    def run_gg(self, *args, **envextra):
        env = testenv.child_env(self.home, **envextra)
        return subprocess.run([sys.executable, GITGRAPH, *args], env=env,
                              capture_output=True, text=True, timeout=120, cwd=self.home)

    def config_file(self):
        if not os.path.exists(self.config_path):
            return None
        with open(self.config_path) as f:
            return json.load(f)

    def value_of(self, key, out):
        """The value column of one `gg config` line."""
        for line in out.splitlines():
            if line.startswith(key.ljust(12) + " = "):
                return line.split("=", 1)[1].split("[")[0].strip()
        self.fail(f"{key} not listed in `gg config` output:\n{out}")

    def source_of(self, key, out):
        """The [env] / [config] / [default] marker of one `gg config` line."""
        for line in out.splitlines():
            if line.startswith(key.ljust(12) + " = "):
                return line.split("[", 1)[1].split("]")[0].strip()
        self.fail(f"{key} not listed in `gg config` output:\n{out}")


class TestSelfDocumenting(ConfigCase):
    def test_every_key_in_config_keys_is_listed_with_its_help(self):
        r = self.run_gg("config")
        self.assertEqual(r.returncode, 0, r.stderr)
        for key, (envvar, default, help_) in gg.CONFIG_KEYS.items():
            self.assertIn(key, r.stdout)
            self.assertIn(help_[:30], r.stdout, f"{key}: help text missing from `gg config`")

    def test_the_file_path_is_printed(self):
        r = self.run_gg("config")
        self.assertIn(".config/gitgraph/config.json", r.stdout)

    def test_an_unknown_key_is_rejected(self):
        r = self.run_gg("config", "nosuchkey", "1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown key", r.stderr)
        self.assertIsNone(self.config_file(), "a rejected key must not create the config file")


class TestPrecedence(ConfigCase):
    def test_default_when_nothing_is_set(self):
        # `border` has no GITGRAPH_* value in child_env(), so it shows its built-in default
        r = self.run_gg("config", **{"GITGRAPH_BORDER": ""})
        self.assertEqual(self.value_of("border", r.stdout), gg.CONFIG_KEYS["border"][1])
        self.assertEqual(self.source_of("border", r.stdout), "default")

    def test_config_file_beats_the_default(self):
        self.run_gg("config", "border", "double")
        self.assertEqual(self.config_file(), {"border": "double"})
        r = self.run_gg("config")
        self.assertEqual(self.value_of("border", r.stdout), "double")
        self.assertEqual(self.source_of("border", r.stdout), "config")

    def test_env_beats_the_config_file(self):
        self.run_gg("config", "border", "double")
        r = self.run_gg("config", GITGRAPH_BORDER="bold")
        self.assertEqual(self.value_of("border", r.stdout), "bold")
        self.assertEqual(self.source_of("border", r.stdout), "env")

    def test_cli_option_beats_env_and_file(self):
        # theme is the setting with all three layers plus a CLI flag; `gg show` prints the theme-styled
        # output, so use `gg config` for the two lower layers and the CLI for the top one.
        self.run_gg("config", "theme", "light")
        r = self.run_gg("config", GITGRAPH_THEME="basic")
        self.assertEqual(self.value_of("theme", r.stdout), "basic")
        # the CLI layer: --theme changes the module's THEME even though env says basic
        probe = ("import sys; sys.argv=['gg','graph','--theme','dark','--max-age','100000','-t','none',"
                 "'--color','never']; import gitgraph; gitgraph.main(sys.argv[1:]); "
                 "print('THEME=' + gitgraph.THEME, file=sys.stderr)")
        r2 = subprocess.run([sys.executable, "-c", probe], env=testenv.child_env(self.home, GITGRAPH_THEME="basic"),
                            capture_output=True, text=True, timeout=120, cwd=REPO_ROOT)
        self.assertIn("THEME=dark", r2.stderr, r2.stderr[-500:])


class TestStoreAndUnset(ConfigCase):
    def test_set_persists_to_the_isolated_config_file(self):
        r = self.run_gg("config", "lang", "English")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("lang = English", r.stdout)
        self.assertEqual(self.config_file(), {"lang": "English"})
        self.assertTrue(self.config_path.startswith(self.home), "must never write the real config")

    def test_reading_one_key_prints_just_the_value(self):
        self.run_gg("config", "lang", "English")
        r = self.run_gg("config", "lang")
        self.assertEqual(r.stdout.strip(), "English")

    def test_a_multi_word_value_is_joined(self):
        self.run_gg("config", "me", "alice, bob")
        self.assertEqual(self.config_file(), {"me": "alice, bob"})

    def test_unset_removes_the_key_and_falls_back(self):
        self.run_gg("config", "border", "double")
        r = self.run_gg("config", "unset", "border")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.config_file(), {})
        r2 = self.run_gg("config", **{"GITGRAPH_BORDER": ""})
        self.assertEqual(self.source_of("border", r2.stdout), "default")

    def test_unset_of_an_unknown_key_is_rejected(self):
        r = self.run_gg("config", "unset", "nosuchkey")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown key", r.stderr)

    def test_the_real_config_file_is_never_touched(self):
        real = os.path.expanduser("~/.config/gitgraph/config.json")
        before = os.path.exists(real) and os.stat(real).st_mtime
        self.run_gg("config", "lang", "Klingon")
        after = os.path.exists(real) and os.stat(real).st_mtime
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
