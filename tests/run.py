#!/usr/bin/env python3
"""Test runner for gg (gitgraph).

    python3 tests/run.py                 # py_compile -> unit -> golden -> smoke; one summary line; exit 1 on any failure
    python3 tests/run.py unit            # only the stdlib-unittest suite (py_compile still runs first)
    python3 tests/run.py golden          # only tests whose dotted test id contains "golden" (see below)
    python3 tests/run.py smoke           # only the pty end-to-end test (tests/tui_smoke.py)
    python3 tests/run.py -k PATTERN      # unit+golden tests whose id contains PATTERN (no stage given -> smoke is skipped)
    python3 tests/run.py unit -k PATTERN # filter within one explicit stage
    python3 -m unittest tests.test_parse.TestRefs.test_fence   # a single test, plain stdlib unittest

"unit" and "golden" are both ordinary unittest.TestCase files under tests/ (test_*.py), discovered the
same way; the split is a naming convention, not a different mechanism — a test whose class or method
name contains "golden" (case-insensitive; e.g. comparing rendered output against tests/golden/*.txt)
counts as "golden", everything else discovered under tests/ counts as "unit". Both stages print
"no <stage> tests found" and succeed (exit 0) when nothing matches yet, so a fresh checkout with zero
test_*.py files still passes: new tests just need to exist, no registration.

"smoke" always runs tests/tui_smoke.py as a subprocess, unaffected by -k (it is not a unittest suite).
Until it is fixture-ized (a later phase), it still needs a real `gh` login and a cached repo
(GITGRAPH_REPOS=owner/name) — so `smoke` can legitimately fail outside such an environment; that is a
property of tui_smoke.py, not of this runner.

The first step, always, is `python3 -m py_compile gitgraph.py` (0.10.3 shipped a syntax error to
users) — if it fails, every later step is skipped, since nothing downstream could import the module.
"""
import argparse
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
GITGRAPH_PY = ROOT / "gitgraph.py"
SMOKE_PY = TESTS_DIR / "tui_smoke.py"


def step_py_compile():
    print("== py_compile ==")
    r = subprocess.run([sys.executable, "-m", "py_compile", str(GITGRAPH_PY)])
    ok = r.returncode == 0
    print("ok   py_compile" if ok else "FAIL py_compile")
    return ok


def _discover(pattern):
    """Every TestCase under tests/test_*.py, flattened to a list, optionally filtered by a
    case-insensitive substring of its dotted test id."""
    loader = unittest.TestLoader()
    # top_level_dir == start_dir so unittest does not demand tests/__init__.py to consider it "importable".
    suite = loader.discover(str(TESTS_DIR), pattern="test_*.py", top_level_dir=str(TESTS_DIR))
    tests = []

    def flatten(s):
        for t in s:
            if isinstance(t, unittest.TestSuite):
                flatten(t)
            else:
                tests.append(t)

    flatten(suite)
    if pattern:
        pattern = pattern.lower()
        tests = [t for t in tests if pattern in t.id().lower()]
    return tests


def _run(tests, label):
    print(f"== {label} ==")
    if not tests:
        print(f"ok   no {label} tests found")
        return True
    sys.stdout.flush()
    result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(unittest.TestSuite(tests))
    return result.wasSuccessful()


def step_unit(k=None):
    return _run([t for t in _discover(k) if "golden" not in t.id().lower()], "unit")


def step_golden(k=None):
    return _run([t for t in _discover(k) if "golden" in t.id().lower()], "golden")


def step_smoke():
    print("== smoke ==")
    r = subprocess.run([sys.executable, str(SMOKE_PY)])
    ok = r.returncode == 0
    print("ok   smoke" if ok else "FAIL smoke")
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", nargs="?", choices=["unit", "golden", "smoke"],
                    help="run only this stage (py_compile always still runs first)")
    ap.add_argument("-k", metavar="PATTERN", help="case-insensitive substring of the dotted test id (unit/golden only)")
    a = ap.parse_args(argv)

    if not step_py_compile():
        print("\nFAIL: py_compile (skipping the rest)")
        return 1

    results = {}
    if a.stage == "unit":
        results["unit"] = step_unit(a.k)
    elif a.stage == "golden":
        results["golden"] = step_golden(a.k)
    elif a.stage == "smoke":
        results["smoke"] = step_smoke()
    elif a.k:
        results["unit"] = step_unit(a.k)
        results["golden"] = step_golden(a.k)
    else:
        results["unit"] = step_unit()
        results["golden"] = step_golden()
        results["smoke"] = step_smoke()

    failed = [name for name, ok in results.items() if not ok]
    print()
    if failed:
        print(f"FAIL: {', '.join(failed)}")
        return 1
    print(f"all good ({', '.join(results)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
