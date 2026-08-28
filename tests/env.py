"""Test isolation helpers for gg (gitgraph).

Every test — unit or subprocess/pty — runs inside a throwaway HOME built by make_home(), preloaded with
the fixture repo (tests/fixtures/repo.json) already "fetched" into ~/.cache/gitgraph, so the whole
pipeline (load_items -> build_graph -> render) runs with no `gh` process and no network:

    from env import make_home, child_env, load_module, fixture_graph, FIXTURE_REPO, FIXTURE_ME

    # subprocess / pty tests (tests/tui_smoke.py style):
    home = make_home()
    env = child_env(home)
    subprocess.run([sys.executable, "gitgraph.py", "graph", "--max-age", "100000"], env=env)

    # in-process unit tests:
    gg = load_module()          # imports gitgraph with HOME pointed at a fresh fixture home
    g = fixture_graph(gg)       # Graph built from the fixture, via the real build_graph()

load_module() MUST run before anything else imports `gitgraph`: the module reads CONFIG / CACHE_DIR /
ME / THEME at import time, so HOME (and the GITGRAPH_* env vars) have to be set first. Repeated calls
are cheap — the module and its fixture HOME are created once per process and cached.

The absolute rule this file exists to enforce: no test ever reads or writes the real
~/.cache/gitgraph, ~/.config/gitgraph or ~/gitgraph-todo.md. Every path written here is built from the
temp `home` argument directly (never from os.path.expanduser), and load_module() asserts afterwards
that gitgraph.CACHE_DIR really did land under that temp home before anything else runs.
"""
import json
import os
import sys
import tempfile
import time

FIXTURE_REPO = "test/repo"
FIXTURE_ME = "alice"

# Big enough that a fixture cache "fetched" years ago still reads as fresh under any --max-age used in
# the tests (see load_items(): `time.time() - fetched_at < max_age_min * 60`). We still stamp fetched_at
# with the real current time in make_home() (see below) so this is a belt-and-suspenders margin, not
# something the tests actually rely on.
FIXTURE_MAX_AGE_MIN = 10 ** 8

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIXTURE_PATH = os.path.join(_HERE, "fixtures", "repo.json")
_FAKES_DIR = os.path.join(_HERE, "fakes")


def _load_fixture():
    with open(_FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _cache_filename(kind, repo, state=""):
    return f"{kind}__{repo.replace('/', '__')}{'__' + state if state else ''}.json"


def make_home(tmpdir=None):
    """Build an isolated HOME (a fresh tempfile.mkdtemp() unless `tmpdir` is given) with the fixture
    repo already cached under <home>/.cache/gitgraph — items__test__repo__open.json plus one
    stubs__*.json per external repo the fixture references — so load_items() returns it without a
    fetch. Returns the HOME path.

    fetched_at is stamped with the real current time (not a value baked into the fixture file): the
    fixture's own dates (created/updated/comments/...) are what must stay fixed for byte-identical
    output, but fetched_at only feeds the cache-freshness check and is never rendered, so making it
    "just now" is what actually keeps that check passing forever, on any machine, on any day.
    """
    home = tmpdir or tempfile.mkdtemp(prefix="gg-test-home-")
    cache_dir = os.path.join(home, ".cache", "gitgraph")
    config_dir = os.path.join(home, ".config", "gitgraph")
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(config_dir, exist_ok=True)

    fixture = _load_fixture()
    now = time.time()

    items_path = os.path.join(cache_dir, _cache_filename("items", FIXTURE_REPO, "open"))
    with open(items_path, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": now, "repo": FIXTURE_REPO, "state": "open", "items": fixture["items"]}, f)
    os.chmod(items_path, 0o600)

    for repo, entries in fixture.get("stubs", {}).items():
        stub_cache = {num: dict(v, fetched_at=now) for num, v in entries.items()}
        stubs_path = os.path.join(cache_dir, _cache_filename("stubs", repo))
        with open(stubs_path, "w", encoding="utf-8") as f:
            json.dump(stub_cache, f)
        os.chmod(stubs_path, 0o600)

    os.chmod(cache_dir, 0o700)
    os.chmod(config_dir, 0o700)
    return home


def child_env(home, **extra):
    """env dict for subprocess/pty tests: an isolated HOME, tests/fakes put first on PATH (ahead of any
    real `gh`), fixed TZ/TERM/LANG, and the GITGRAPH_* variables that make the fixture usable with no
    `gh` and no AI CLI (auto_translate off, a "me" that matches the fixture). Deliberately built from
    scratch rather than a copy of os.environ — like the `env -i` used to verify this by hand — so a
    stray var from the developer's shell can never change what a test sees. `extra` overrides/adds on
    top of the defaults.
    """
    env = {
        "HOME": home,
        "PATH": _FAKES_DIR + os.pathsep + "/usr/bin" + os.pathsep + "/bin",
        "TZ": "UTC",
        "TERM": "xterm-256color",
        "LANG": "en_US.UTF-8",
        "GITGRAPH_REPOS": FIXTURE_REPO,
        "GITGRAPH_ME": FIXTURE_ME,
        "GITGRAPH_AUTO_TRANSLATE": "false",
        "GITGRAPH_THEME": "dark",
    }
    env.update(extra)
    return env


_module = None  # cached gitgraph module, imported once per process by load_module()


def load_module():
    """Set HOME (and the rest of child_env()) to a fresh fixture home, import gitgraph, and return the
    module — for in-process unit tests. gitgraph reads CONFIG_PATH / CACHE_DIR / ME / THEME at import
    time, so this must run before anything else imports it; if `gitgraph` is already in sys.modules
    (e.g. a test imported it directly, or called this out of order) that would silently keep whatever
    HOME was active back then — possibly the developer's real one — so this refuses to proceed instead.

    Cached: the first call does the import and pays for it once; every later call in the same process
    returns the same module object without touching the filesystem again.
    """
    global _module
    if _module is not None:
        return _module
    home = make_home()
    for k, v in child_env(home).items():
        os.environ[k] = v
    assert "gitgraph" not in sys.modules, (
        "gitgraph was already imported before tests.env.load_module() ran; its CONFIG/CACHE_DIR/ME/THEME "
        "globals are fixed at import time, so this HOME would not take effect. Make sure load_module() "
        "is the first thing that imports gitgraph."
    )
    repo_root = os.path.dirname(_HERE)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    import gitgraph  # noqa: E402  (import must happen after HOME is set — see docstring)

    assert gitgraph.CACHE_DIR.startswith(home), (
        f"gitgraph.CACHE_DIR ({gitgraph.CACHE_DIR!r}) is not under the test HOME ({home!r}); "
        "refusing to continue rather than risk touching a real cache."
    )
    _module = gitgraph
    return _module


def fixture_items():
    """The fixture repo's items as load_items() would return them (for assemble_graph tests)."""
    return _load_fixture()["items"]


def fixture_graph(gg, **opts):
    """Build and return the Graph for the fixture repo via the real build_graph(), with max_age set so
    high nothing is ever refetched. `gg` is the module from load_module() (its HOME must already have
    the fixture cache written by make_home(), which load_module() does). `opts` are forwarded to
    build_graph() — e.g. state="all" — after pulling out `state` (default "open", matching the one
    cache file make_home() writes) and `max_age_min` (default FIXTURE_MAX_AGE_MIN).
    """
    state = opts.pop("state", "open")
    max_age_min = opts.pop("max_age_min", FIXTURE_MAX_AGE_MIN)
    return gg.build_graph([FIXTURE_REPO], state, max_age_min, **opts)
