#!/usr/bin/env python3
"""gg (gitgraph) - GitHub issue / PR / comment / @mention relation graph rendered as ASCII.

Usage:
  gg [777]                                the TUI (default), optionally starting on #777 (also owner/repo#777, @login)
  gg tutorial                             the TUI with the guided tour
  gg graph [777] [--hops 2]               text graph: overview of open items, or the neighbourhood of one item
  gg show 777                             details of one node
  gg ask 4563 "why does it mention #3859?"   # one-shot question to claude with the item as context
  gg update                               update this installation from GitHub
  gg ai [NAME]                            list / pick the AI CLI (claude, codex, gemini, grok, …)
  gg config [KEY [VALUE]]                 show / set persistent settings (~/.config/gitgraph/config.json)
  gg todo                                 print the markdown of everything marked with m in the tui
  gg todo done|remove ID                  tick off / delete a mark (ID: 750, #750, owner/name#750, comment url); clear-done drops ticked ones
  gg check [-r owner/name]                diagnose: gh accounts for the host, access, open counts, GraphQL fields
  gg mcp                                  MCP stdio server for Claude Code in another window (claude mcp add -s user gg -- gg mcp)
  gg review 123                           a PR's changed files and diff, from a worktree of the local checkout
  gg cache [clear all|items|ai|logs|REPO] what is stored locally (issue/PR bodies, AI results, logs) and how to remove it

Only dependency: the `gh` CLI (authenticated). No pip packages.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from collections import defaultdict, deque
from datetime import datetime, timezone

VERSION = "0.24.0"
REPO_URL = "https://github.com/Daejun/gitgraph"
RAW_URL = "https://raw.githubusercontent.com/Daejun/gitgraph/main/gitgraph.py"
CACHE_DIR = os.path.expanduser("~/.cache/gitgraph")
CONFIG_PATH = os.path.expanduser("~/.config/gitgraph/config.json")
# key -> (env var, default, help)
CONFIG_KEYS = {
    "claude_bin": ("GITGRAPH_CLAUDE", "claude", "AI CLI used for translation / summaries / questions: claude, codex, "
                                               "gemini, grok, or any binary taking -p PROMPT — pick with `gg ai`"),
    "repos": ("GITGRAPH_REPOS", "", "default repos, comma separated (owner/name or host/owner/name)"),
    "me": ("GITGRAPH_ME", "", "logins that count as \"me\", comma separated (default: gh accounts)"),
    "lang": ("GITGRAPH_LANG", "Korean", "language for translations, summaries and answers"),
    "translate": ("GITGRAPH_TRANSLATE", "zh", "zh | all | none"),
    "tr_model": ("GITGRAPH_TR_MODEL", "haiku", "model for translation / summaries (claude only)"),
    "ask_model": ("GITGRAPH_ASK_MODEL", "sonnet", "model for `a` / `gg ask` (claude only)"),
    "batch": ("GITGRAPH_BATCH", "10", "tui: nodes per translate/summary call"),
    "ai_parallel": ("GITGRAPH_AI_PARALLEL", "3", "tui: how many AI CLI calls may run at the same time"),
    "retries": ("GITGRAPH_RETRIES", "3", "gh api retries on transient network errors"),
    "fetch_parallel": ("GITGRAPH_FETCH_PARALLEL", "8", "how many gh queries run at the same time when filling the cache"),
    "theme": ("GITGRAPH_THEME", "dark", "colour theme: dark | light | basic (8 colours, no dim — e.g. PuTTY)"),
    "todo_file": ("GITGRAPH_TODO", "~/gitgraph-todo.md", "markdown written from the marks made with m in the tui (for the next session)"),
    "side_width": ("GITGRAPH_SIDE_WIDTH", "0.4", "tui: fraction of the width for the side column"),
    "expand_focused": ("GITGRAPH_EXPAND_FOCUSED", "true", "tui: give the focused side panel more height (accordion)"),
    "expanded_weight": ("GITGRAPH_EXPANDED_WEIGHT", "2", "tui: how much taller the focused side panel is"),
    "screen_mode": ("GITGRAPH_SCREEN_MODE", "normal", "tui: normal | half | full (+ / _ cycle at runtime)"),
    "border": ("GITGRAPH_BORDER", "rounded", "tui: rounded | single | double | bold | hidden"),
    "worktree_keep_days": ("GITGRAPH_WORKTREE_KEEP_DAYS", "7", "review: drop a PR worktree unused for this many days"),
    "worktree_max": ("GITGRAPH_WORKTREE_MAX", "5", "review: how many PR worktrees to keep (oldest go first)"),
    "review_model": ("GITGRAPH_REVIEW_MODEL", "sonnet", "review: model for the review pass (claude only)"),
    "review_timeout": ("GITGRAPH_REVIEW_TIMEOUT", "900", "review: seconds one AI review call may take"),
    "review_verify": ("GITGRAPH_REVIEW_VERIFY", "on",
                      "review: check every finding in a call of its own that tries to disprove it (on | off)"),
    "review_verify_model": ("GITGRAPH_REVIEW_VERIFY_MODEL", "sonnet", "review: model for that check (claude only)"),
    "review_max_bytes": ("GITGRAPH_REVIEW_MAX_BYTES", "400000",
                         "review: split the diff by file and review the parts in parallel beyond this size"),
    "review_subjective": ("GITGRAPH_REVIEW_SUBJECTIVE", "auto",
                          "review: style/design remarks — auto (hidden while a confirmed defect stands) | always | never"),
    "review_files_width": ("GITGRAPH_REVIEW_FILES_WIDTH", "0.22", "review: fraction of the width for the Files column"),
    "review_findings_width": ("GITGRAPH_REVIEW_FINDINGS_WIDTH", "0.30", "review: fraction of the width for the Findings column"),
}


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


CONFIG = load_config()


def cfg(key):
    """CLI option (handled by the caller) > environment variable > config file > built-in default."""
    env, default, _ = CONFIG_KEYS[key]
    return os.environ.get(env) or str(CONFIG.get(key, "") or "") or default


def save_config():
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(CONFIG, f, indent=2, ensure_ascii=False)


def ai_cmd(args):
    """gg ai            list the AI CLIs gg knows, which are installed, which is selected; pick one by number
       gg ai NAME       select NAME (claude | codex | gemini | grok | any binary that takes -p PROMPT)"""
    import shutil
    known = ["claude", "codex", "gemini", "grok"]
    cur = cfg("claude_bin")
    if args:
        choice = args[0]
        if not shutil.which(choice):
            print(f"{choice}: not found in PATH", file=sys.stderr)
            return 1
        CONFIG["claude_bin"] = choice
        save_config()
        print(f"AI CLI = {choice}  ({AI_BACKENDS.get(ai_backend(choice), ('generic: -p PROMPT', ''))[0]})")
        return 0
    rows = []
    for name in known:
        path = shutil.which(name)
        how, login = AI_BACKENDS[name]
        rows.append((name, path, how, login))
    if cur not in known and shutil.which(cur):
        rows.append((cur, shutil.which(cur), "generic: -p PROMPT", ""))
    print("AI CLIs for translation / summaries / questions:\n")
    for i, (name, path, how, login) in enumerate(rows, 1):
        mark = "*" if name == cur else " "
        state = path or "not installed"
        print(f" {mark}{i}) {name:8} {state:32} {how}" + (f"   (login: {login})" if login and path else ""))
    print(f"\n* = current ({cur}). Choose a number to switch, Enter to keep: ", end="", flush=True)
    try:
        with open("/dev/tty") as tty:
            ans = tty.readline().strip()
    except OSError:
        ans = ""
    if ans.isdigit() and 1 <= int(ans) <= len(rows):
        name, path, how, login = rows[int(ans) - 1]
        if not path:
            print(f"{name} is not installed", file=sys.stderr)
            return 1
        CONFIG["claude_bin"] = name
        save_config()
        print(f"AI CLI = {name}")
    return 0


def config_cmd(args):
    """gg config            show everything and where each value comes from
       gg config KEY VALUE  store VALUE in ~/.config/gitgraph/config.json
       gg config KEY        show one value
       gg config unset KEY  remove it from the file"""
    if not args:
        for k, (env, default, help_) in CONFIG_KEYS.items():
            src = "env" if os.environ.get(env) else ("config" if CONFIG.get(k) else "default")
            print(f"{k:21} = {cfg(k) or '(empty)':24} [{src:7}]  {help_}")
        print(f"\nfile: {CONFIG_PATH}   (env var wins over the file; CLI options win over both)")
        return 0
    if args[0] == "unset":
        key = args[1] if len(args) > 1 else ""
        if key not in CONFIG_KEYS:
            print(f"unknown key {key!r}; keys: {', '.join(CONFIG_KEYS)}", file=sys.stderr)
            return 1
        CONFIG.pop(key, None)
    else:
        key = args[0]
        if key not in CONFIG_KEYS:
            print(f"unknown key {key!r}; keys: {', '.join(CONFIG_KEYS)}", file=sys.stderr)
            return 1
        if len(args) == 1:
            print(cfg(key))
            return 0
        CONFIG[key] = " ".join(args[1:])
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(CONFIG, f, indent=2, ensure_ascii=False)
    print(f"{key} = {cfg(key) or '(unset)'}   -> {CONFIG_PATH}")
    return 0
ENV_REPOS = [r.strip() for r in cfg("repos").split(",") if r.strip()]
PAGE = 50
ME = [m.strip().lower() for m in cfg("me").split(",") if m.strip()]
ENRICH_BATCH = int(cfg("batch"))   # tui: nodes per translate/summary call
AI_PARALLEL = max(1, int(cfg("ai_parallel") or 3))   # concurrent AI CLI calls


def log(msg):
    sys.stderr.write(f"[gitgraph] {msg}\n")
    sys.stderr.flush()


PROGRESS = None  # optional callable(phase, done, total, detail) — the TUI installs one


def progress(phase, done, total=None, detail=""):
    if PROGRESS:
        PROGRESS(phase, done, total, detail)


# --------------------------------------------------------------------------
# repo discovery: git repos under the directory gg was started in
# --------------------------------------------------------------------------
DEFAULT_HOST = "github.com"
_REMOTE_RE = re.compile(r"^(?:https?://(?:[^@/]+@)?|ssh://(?:[^@/]+@)?|[^@/]+@)?(?P<host>[\w.-]+)(?::\d+)?[:/]"
                        r"(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$")


def split_repo(repo):
    """'owner/name' -> (github.com, owner, name); 'ghe.example.com/owner/name' -> (host, owner, name)."""
    parts = repo.split("/")
    if len(parts) >= 3:
        return parts[0], parts[1], "/".join(parts[2:])
    return DEFAULT_HOST, parts[0], parts[1] if len(parts) > 1 else ""


def make_repo(host, owner, name):
    return f"{owner}/{name}" if host == DEFAULT_HOST else f"{host}/{owner}/{name}"


def qualify(repo, host):
    """A bare 'owner/name' seen inside a repo on `host` belongs to that host."""
    return repo if len(repo.split("/")) >= 3 or host == DEFAULT_HOST else f"{host}/{repo}"


def repo_host(repo):
    return split_repo(repo)[0]


def discover_repos(root, depth=2):
    """[(owner/name, dir)] for the repo containing root plus git repos up to `depth` levels below it."""
    dirs = []
    r = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        dirs.append(r.stdout.strip())
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        level = dirpath[len(root):].count(os.sep)
        if ".git" in dirnames or ".git" in filenames:
            dirs.append(dirpath)
            dirnames[:] = []
            continue
        if level >= depth:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
    best = {}   # repo -> dir (shallowest wins; a dir named like the repo wins ties)
    for d in dict.fromkeys(dirs):
        for rank_remote, repo in github_remotes(d):
            rank = (d.count(os.sep), rank_remote, os.path.basename(d) != split_repo(repo)[2], d)
            if repo not in best or rank < best[repo][0]:
                best[repo] = (rank, d)
    for repo, (_, d) in best.items():
        CHECKOUTS[repo] = d          # `gg review` needs the tree itself, not just the repo name
    return sorted(((repo, d) for repo, (rank, d) in best.items()), key=lambda x: best[x[0]][0])


def is_github_host(host):
    """github.com, any host with 'github' in its name (Enterprise), or a host gh is logged in to."""
    host = host.lower()
    return host == DEFAULT_HOST or "github" in host or host in gh_hosts()


_ssh_hosts = {}


def resolve_ssh_alias(host):
    """'gh-work' (an alias in ~/.ssh/config) -> its real HostName, via `ssh -G`; hosts with a dot are returned as is."""
    if "." in host or host in _ssh_hosts:
        return _ssh_hosts.get(host, host)
    real = host
    try:
        r = subprocess.run(["ssh", "-G", host], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            if line.startswith("hostname "):
                real = line.split(None, 1)[1].strip()
                break
    except (OSError, subprocess.TimeoutExpired):
        pass
    _ssh_hosts[host] = real
    return real


SKIPPED_REMOTES = []   # (dir, remote name, url, reason) — shown when nothing usable was found


_ACCT_IN_HELPER = re.compile(r"username=([A-Za-z0-9](?:[A-Za-z0-9-]*))|(?:auth token\s+)?-u\s+([A-Za-z0-9](?:[A-Za-z0-9-]*))")


def git_account_hint(d, host, url=""):
    """Which gh account this checkout uses for `host`, from its own git config: the standard
    credential.<host>.username, a gh credential helper that names one (`gh auth token -u LOGIN`, what
    `gh auth setup-git -u` writes), or a user@ in the remote URL. None when nothing says so.

    Worth reading because the active gh account is often not the one a private repo is shared with, and
    this is known before the first API call — the cache in accounts.json only learns it after one."""
    m = re.match(r"https?://([^@/]+)@", url or "")
    hinted = [m.group(1)] if m else []
    r = subprocess.run(["git", "-C", d, "config", "--get-regexp",
                        r"^credential\.https://" + re.escape(host) + r"\."], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        key, _, val = line.partition(" ")
        if key.endswith(".username"):
            hinted.insert(0, val.strip())
        elif key.endswith(".helper"):
            mm = _ACCT_IN_HELPER.search(val)
            if mm:
                hinted.append(mm.group(1) or mm.group(2))
    known = gh_accounts(host)
    return next((h for h in hinted if h in known), None)


def remote_urls(d):
    """[(remote name, url)] as *configured*. Not `git remote -v`, which prints the URL after applying
    any url.<base>.insteadOf rewrite — a checkout that rewrites github.com to an internal mirror would
    otherwise look like it has no GitHub remote at all."""
    r = subprocess.run(["git", "-C", d, "config", "--get-regexp", r"^remote\..*\.url$"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines():
        key, _, url = line.partition(" ")
        parts = key.split(".")
        if len(parts) >= 3 and url.strip():
            out.append((".".join(parts[1:-1]), url.strip()))
    return out


def github_remotes(d):
    """[(rank, repo)] for every remote of the git repo at d whose URL points at a GitHub host.
    rank: 0 = origin, 1 = a remote named github*, 2 = anything else."""
    out, seen = [], set()
    for name, url in remote_urls(d):
        m = _REMOTE_RE.match(url)
        if not m:
            SKIPPED_REMOTES.append((d, name, url, "URL not understood"))
            continue
        host = resolve_ssh_alias(m.group("host").lower())
        if not is_github_host(host):
            SKIPPED_REMOTES.append((d, name, url, f"host {host!r} is not github.com / github.* / a gh-logged-in host"))
            continue
        repo = make_repo(host, m.group("owner"), m.group("name"))
        if repo in seen:
            continue
        seen.add(repo)
        hint = git_account_hint(d, host, url)
        if hint:      # a first guess for graphql(); anything already verified in accounts.json wins
            _pref_map().setdefault(host, {}).setdefault(repo, hint)
        out.append((0 if name == "origin" else 1 if name.lower().startswith("github") else 2, repo))
    return sorted(out)


def choose_repos(cands):
    """Ask on the terminal which of several discovered repos to use. Returns a list."""
    home = os.path.expanduser("~")
    lines = [f"gg: several GitHub repos under {os.getcwd().replace(home, '~')}:"]
    for i, (repo, d) in enumerate(cands, 1):
        lines.append(f"  {i}) {repo}   ({d.replace(home, '~')})")
    lines.append("  a) all of them (first one is primary)")
    sys.stderr.write("\n".join(lines) + "\nchoose [1]: ")
    sys.stderr.flush()
    try:
        with open("/dev/tty") as tty:
            ans = tty.readline().strip().lower()
    except OSError:
        ans = ""
    if ans in ("a", "all"):
        return [c[0] for c in cands]
    picks = []
    for tok in re.split(r"[\s,]+", ans or "1"):
        if tok.isdigit() and 1 <= int(tok) <= len(cands):
            picks.append(cands[int(tok) - 1][0])
    return picks or [cands[0][0]]


_parent_cache = {}


def parent_repo(repo):
    """If repo is a fork, the repo it was forked from (owner/name or host/owner/name); else None."""
    if repo in _parent_cache:
        return _parent_cache[repo]
    host, owner, name = split_repo(repo)
    res = None
    try:
        d = graphql("query($o:String!,$n:String!){ repository(owner:$o,name:$n){ isFork parent{ nameWithOwner } } }",
                    {"o": owner, "n": name}, host)
        r = (d or {}).get("repository") or {}
        if r.get("isFork") and r.get("parent"):
            res = qualify(r["parent"]["nameWithOwner"], host)
    except GhError:
        pass
    _parent_cache[repo] = res
    return res


def unfork(repos):
    """Replace forks by their parents (a clone's `origin` is usually the fork; the issues/PRs live upstream)."""
    out = []
    for repo in repos:
        parent = parent_repo(repo)
        if parent:
            if repo in CHECKOUTS:
                CHECKOUTS.setdefault(parent, CHECKOUTS[repo])   # the PR lives upstream, the objects here
            if parent not in out:
                log(f"{repo} is a fork of {parent}; using {parent} (pass -r {repo} to look at the fork itself)")
                out.append(parent)
        elif repo not in out:
            out.append(repo)
    return out


def seed_account_hints(repos, d=None):
    """For repos named on the command line (or in $GITGRAPH_REPOS) there is no discovery step, so look
    for their checkouts the same way discovery does — the repo we are standing in (from any depth) plus
    the ones a couple of levels below — and read the account each one uses from its git config. Only a
    first guess: a verified account in accounts.json wins, and a wrong guess costs the one fallback it
    costs today. Skipped entirely once every repo has a remembered account (i.e. after the first run)."""
    if not repos:
        return
    if not all((_pref_map().get(repo_host(r)) or {}).get(r) for r in repos):
        for repo, checkout in discover_repos(d or os.getcwd()):
            host = repo_host(repo)
            hint = git_account_hint(checkout, host)
            if hint:
                _pref_map().setdefault(host, {}).setdefault(repo, hint)
    # what the primary repo uses is also the best guess for the repos it references (stubs)
    primary = repos[0]
    fav = (_pref_map().get(repo_host(primary)) or {}).get(primary)
    if fav:
        _acct_hint[repo_host(primary)] = fav


def resolve_repos(explicit=None, interactive=False):
    """-r > $GITGRAPH_REPOS > repos found under cwd (ask if several; forks -> their parent)."""
    if explicit:
        seed_account_hints(explicit)
        return list(explicit)
    if ENV_REPOS:
        seed_account_hints(ENV_REPOS)
        return ENV_REPOS
    cands = discover_repos(os.getcwd())
    cands = [(r, d) for r, d in cands]
    if not cands:
        home = os.path.expanduser("~")
        seen = "\n".join(f"  {d.replace(home, '~')}: {name} {url} — {why}" for d, name, url, why in SKIPPED_REMOTES[:12])
        raise ValueError(f"no GitHub repo found under {os.getcwd().replace(home, '~')} (this directory and 2 levels below)"
                         + (f"\n  remotes seen but skipped:\n{seen}" if seen else "\n  no git remotes found here")
                         + "\n  -> pass -r owner/name, set GITGRAPH_REPOS, or run inside the repo")
    if len(cands) == 1:
        picked = unfork([cands[0][0]])
        seed_account_hints(picked)
        return picked
    if interactive:
        picked = unfork(choose_repos(cands))
        seed_account_hints(picked)
        return picked
    raise ValueError("several repos under " + os.getcwd() + ": " + ", ".join(c[0] for c in cands)
                     + " — pass repos=[\"owner/name\", ...]")


# --------------------------------------------------------------------------
# gh access (with multi-account fallback for private repos)
# --------------------------------------------------------------------------
class GhError(Exception):
    pass


_accounts = None   # host -> [login, ...] (active first)
_tokens = {}


def gh_accounts(host=None):
    """Login names gh knows for `host` (active first); host=None -> every host, github.com first."""
    global _accounts
    if _accounts is None:
        r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
        by_host, cur = {}, None
        for line in (r.stdout + r.stderr).splitlines():
            m = re.search(r"Logged in to (\S+) account (\S+)", line)
            if m:
                cur = (m.group(1), m.group(2))
                by_host.setdefault(cur[0], []).append(cur[1])
            elif "Active account: true" in line and cur:
                by_host[cur[0]].remove(cur[1])
                by_host[cur[0]].insert(0, cur[1])
        _accounts = by_host
    if host:
        return list(_accounts.get(host, []))
    return list(_accounts.get(DEFAULT_HOST, [])) + [a for h, l in _accounts.items() if h != DEFAULT_HOST for a in l]


def gh_hosts():
    gh_accounts()
    return set(_accounts or {})


def gh_token(user, host=DEFAULT_HOST):
    key = (host, user)
    if key in _tokens:
        return _tokens[key]
    r = subprocess.run(["gh", "auth", "token", "-h", host, "-u", user], capture_output=True, text=True)
    tok = r.stdout.strip() or None
    _tokens[key] = tok
    return tok


# Bare "502|503|504" used to be in here, which made any error mentioning such a number transient —
# "Could not resolve to an issue or pull request with the number of 4503" then cost 2+4+8s of retries.
TRANSIENT_RE = re.compile(r"TLS handshake timeout|connection reset|i/o timeout|timeout|EOF|"
                          r"temporarily unavailable|no such host|HTTP 5\d\d|"
                          r"bad gateway|service unavailable|server error", re.I)
GH_RETRIES = int(cfg("retries"))


def gh_api(args, body, env):
    """`gh api …` with retries on transient network errors (TLS handshake timeout, resets, 5xx)."""
    for attempt in range(GH_RETRIES + 1):
        r = subprocess.run(["gh", "api"] + args, input=body, capture_output=True, text=True, env=env)
        err = (r.stderr or "").strip()
        if r.returncode == 0 or not TRANSIENT_RE.search(err) or attempt == GH_RETRIES:
            return r
        wait = 2 ** (attempt + 1)
        log(f"gh: {err.splitlines()[-1][:120]} — retrying in {wait}s ({attempt + 1}/{GH_RETRIES})")
        progress("fetch", 0, None, f"network error, retry {attempt + 1}/{GH_RETRIES} in {wait}s")
        time.sleep(wait)
    return r


_acct_pref = None       # {host: {repo: login}} — which account could actually see a repo, across runs
_acct_hint = {}         # {host: login} — this run's guess from git config, for repos we know nothing about


def _accounts_path():
    return os.path.join(CACHE_DIR, "accounts.json")     # resolved per call, like the other caches


def _pref_map():
    global _acct_pref
    if _acct_pref is None:
        _acct_pref = read_json(_accounts_path()) or {}
    return _acct_pref


def _query_repo(query, variables):
    """owner/name the query is about, for remembering which account can see it."""
    if variables and variables.get("owner") and variables.get("name"):
        return f"{variables['owner']}/{variables['name']}"
    m = re.search(r'repository\(owner:"([^"]+)",\s*name:"([^"]+)"\)', query)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _prefer_account(host, user, repo=None):
    """Remember the account that could see this repo: first in this process, then in the cache, so the
    next run does not spend a round trip discovering it again (the active gh account is often not the
    one a private repo is shared with)."""
    if _accounts and user in _accounts.get(host, []):
        _accounts[host].remove(user)
        _accounts[host].insert(0, user)
    if not repo or not user:
        return
    d = _pref_map().setdefault(host, {})
    if d.get(repo) != user:
        d[repo] = user
        try:
            write_json(_accounts_path(), _acct_pref)
        except OSError:
            pass


def graphql(query, variables=None, host=DEFAULT_HOST):
    """Run a GraphQL query through `gh api graphql` against `host` (github.com or a GitHub Enterprise host).

    If the active account gets NOT_FOUND (private repo not visible), retry
    with the other accounts registered for that host; the account that works
    is moved to the front for the rest of the process.
    """
    accts = gh_accounts(host) or [None]
    repo = _query_repo(query, variables)
    fav = (_pref_map().get(host) or {}).get(repo) or _acct_hint.get(host)
    if fav in accts and accts[0] != fav:                # the account that saw this repo last time
        accts = [fav] + [a for a in accts if a != fav]
    body = json.dumps({"query": query, "variables": variables or {}})
    last_err = None
    for i, acct in enumerate(accts):
        env = dict(os.environ)
        if acct:
            tok = gh_token(acct, host)
            if not tok:
                continue
            env["GH_TOKEN"] = tok
            env["GH_ENTERPRISE_TOKEN"] = tok
        r = gh_api(["graphql", "--hostname", host, "--input", "-"], body, env)
        try:
            data = json.loads(r.stdout) if r.stdout.strip() else {}
        except json.JSONDecodeError:
            data = {}
        errs = data.get("errors") or []
        if not errs and r.returncode == 0:
            if (_pref_map().get(host) or {}).get(repo) != acct:
                _prefer_account(host, acct, repo)
            return data.get("data", {})
        types = {e.get("type") for e in errs}
        partial = data.get("data") or {}
        if errs and types <= {"NOT_FOUND"} and any(v is not None for v in partial.values()):
            # partial NOT_FOUND (e.g. one alias in a stub batch): usable; a null repository is not
            if (_pref_map().get(host) or {}).get(repo) != acct:
                _prefer_account(host, acct, repo)
            return data.get("data", {})
        last_err = errs or r.stderr.strip() or f"gh exit {r.returncode}"
        if "NOT_FOUND" in types or "Could not resolve" in (r.stderr or ""):
            continue  # try next account
        if TRANSIENT_RE.search(r.stderr or ""):
            raise GhError(f"cannot reach {host}: {r.stderr.strip().splitlines()[-1][:160]}\n"
                          "  check: `gh api user` works? proxy needed (export HTTPS_PROXY=http://host:port)? "
                          "VPN/DNS? retries: GITGRAPH_RETRIES (default 3)")
        break
    if isinstance(last_err, list) and last_err and all(e.get("type") == "NOT_FOUND" for e in last_err):
        msg = last_err[0].get("message", "not found")
        raise GhError(f"{msg} on {host} with any gh account ({', '.join(gh_accounts(host)) or 'none logged in — gh auth login -h ' + host})")
    raise GhError(f"graphql failed: {last_err}")


# --------------------------------------------------------------------------
# fetch + cache
# --------------------------------------------------------------------------
COMMENT_FIELDS = "databaseId url author{login} body createdAt"
CROSSREF_FIELDS = """
timelineItems(first:100, itemTypes:[CROSS_REFERENCED_EVENT]){ nodes{
  ... on CrossReferencedEvent{ createdAt source{ __typename
    ... on Issue{ number title state createdAt author{login} repository{nameWithOwner} }
    ... on PullRequest{ number title state isDraft createdAt author{login} repository{nameWithOwner} } } } } }
"""
ISSUE_FIELDS = f"""
number title state body createdAt updatedAt url author{{login}} labels(first:10){{nodes{{name}}}}
comments(first:100){{ totalCount nodes{{ {COMMENT_FIELDS} }} }}
{CROSSREF_FIELDS}
"""
PR_FIELDS = ISSUE_FIELDS + f"""
isDraft closingIssuesReferences(first:20){{nodes{{number repository{{nameWithOwner}}}}}}
reviews(first:50){{ nodes{{ databaseId url author{{login}} body state createdAt
  comments(first:50){{ nodes{{ {COMMENT_FIELDS} }} }} }} }}
"""

Q_ISSUES = f"""
query($owner:String!,$name:String!,$after:String,$states:[IssueState!]) {{
  repository(owner:$owner,name:$name){{
    issues(first:{PAGE}, after:$after, states:$states, orderBy:{{field:CREATED_AT,direction:DESC}}){{
      pageInfo{{hasNextPage endCursor}} nodes{{ {ISSUE_FIELDS} }} }} }} }}
"""
Q_PRS = f"""
query($owner:String!,$name:String!,$after:String,$states:[PullRequestState!]) {{
  repository(owner:$owner,name:$name){{
    pullRequests(first:{PAGE}, after:$after, states:$states, orderBy:{{field:CREATED_AT,direction:DESC}}){{
      pageInfo{{hasNextPage endCursor}} nodes{{ {PR_FIELDS} }} }} }} }}
"""


def _login(a):
    return (a or {}).get("login") or "ghost"


def _norm_comment(c, kind, review_state=None):
    return {"id": f"c{c.get('databaseId')}", "author": _login(c.get("author")),
            "body": c.get("body") or "", "created": c.get("createdAt"),
            "url": c.get("url"), "kind": kind, "review_state": review_state}


def _norm_item(repo, n, is_pr):
    comments = [_norm_comment(c, "comment") for c in n["comments"]["nodes"]]
    if is_pr:
        for rv in n.get("reviews", {}).get("nodes", []):
            if (rv.get("body") or "").strip():
                comments.append(_norm_comment(rv, "review", rv.get("state")))
            for rc in rv.get("comments", {}).get("nodes", []):
                comments.append(_norm_comment(rc, "review_comment", rv.get("state")))
    comments.sort(key=lambda c: c["created"] or "")
    crossrefs = []
    for ev in n["timelineItems"]["nodes"]:
        s = (ev or {}).get("source") or {}
        if not s.get("number"):
            continue
        crossrefs.append({"repo": s["repository"]["nameWithOwner"], "number": s["number"],
                          "is_pr": s["__typename"] == "PullRequest", "title": s.get("title"),
                          "state": s.get("state"), "draft": s.get("isDraft", False),
                          "created": s.get("createdAt"), "author": _login(s.get("author")),
                          "when": ev.get("createdAt")})
    closes = []
    if is_pr:
        for c in n.get("closingIssuesReferences", {}).get("nodes", []):
            closes.append({"repo": c["repository"]["nameWithOwner"], "number": c["number"]})
    return {"repo": repo, "number": n["number"], "is_pr": is_pr, "title": n["title"],
            "state": n["state"], "draft": n.get("isDraft", False), "body": n.get("body") or "",
            "created": n["createdAt"], "updated": n["updatedAt"], "url": n["url"],
            "author": _login(n.get("author")), "labels": [l["name"] for l in n["labels"]["nodes"]],
            "comments": comments, "comments_total": n["comments"]["totalCount"],
            "crossrefs": crossrefs, "closes": closes}


Q_LIST = """
query($owner:String!,$name:String!,$after:String,$states:[IssueState!]) {
  repository(owner:$owner,name:$name){ issues(first:100, after:$after, states:$states,
    orderBy:{field:CREATED_AT,direction:DESC}){
    pageInfo{hasNextPage endCursor} nodes{ number updatedAt } } } }
"""
Q_LIST_PR = """
query($owner:String!,$name:String!,$after:String,$states:[PullRequestState!]) {
  repository(owner:$owner,name:$name){ pullRequests(first:100, after:$after, states:$states,
    orderBy:{field:CREATED_AT,direction:DESC}){
    pageInfo{hasNextPage endCursor} nodes{ number updatedAt } } } }
"""
ITEM_BATCH = 10             # numbers per query when only a few items changed
MAX_ITEM_BATCH = 25         # cold start: how many may share one query (bigger = fewer `gh` processes)
FETCH_PARALLEL = max(1, int(cfg("fetch_parallel") or 8))   # concurrent `gh api graphql` queries


def list_open(repo):
    """{(is_pr, number): updatedAt} for every open issue and PR — light queries (no bodies). The issue
    and pull-request connections are independent, so they are paged at the same time; only the pages
    within one connection have to be sequential (each needs the previous cursor)."""
    from concurrent.futures import ThreadPoolExecutor
    host, owner, name = split_repo(repo)

    def page_all(q, is_pr):
        res, after = {}, None
        while True:
            d = graphql(q, {"owner": owner, "name": name, "after": after, "states": ["OPEN"]}, host)
            conn = (d.get("repository") or {}).get("pullRequests" if is_pr else "issues")
            if conn is None:
                raise GhError(f"{repo}: repository not found on {host}")
            for n in conn["nodes"]:
                res[(is_pr, n["number"])] = n["updatedAt"]
            if not conn["pageInfo"]["hasNextPage"]:
                return res
            after = conn["pageInfo"]["endCursor"]

    out = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        for res in pool.map(lambda a: page_all(*a), ((Q_LIST, False), (Q_LIST_PR, True))):
            out.update(res)
    return out


def fetch_batch(repo, is_pr, numbers):
    """One query: the full records of these issue (or PR) numbers."""
    host, owner, name = split_repo(repo)
    fields = PR_FIELDS if is_pr else ISSUE_FIELDS
    kind = "pullRequest" if is_pr else "issue"
    aliases = " ".join(f"n{n}: {kind}(number:{n}){{ {fields} }}" for n in numbers)
    d = graphql(f'query {{ repository(owner:"{owner}", name:"{name}") {{ {aliases} }} }}', host=host)
    rep_ = d.get("repository") or {}
    return [_norm_item(repo, rep_[f"n{n}"], is_pr) for n in numbers if rep_.get(f"n{n}")]


def fetch_items(repo, is_pr, numbers, note="changed items", spread=False, on_batch=None):
    return fetch_groups(repo, [(is_pr, numbers)], note, spread, on_batch)


def fetch_groups(repo, groups, note="items", spread=False, on_batch=None):
    """Full records (bodies, comments, cross references) of the given issue or PR numbers: one query per
    batch of numbers, FETCH_PARALLEL queries in flight (each batch is an independent query, so this is
    where a cold start gets its speed — pagination cannot be parallelised, batches can).

    Every query is a `gh` process, and that costs ~0.4s before any network happens, so `spread` (the
    cold start) makes the batches as large as it can while still filling every parallel slot — a few
    big queries beat many small ones. The incremental refresh keeps ITEM_BATCH: there the item count is
    small and the batches are what limits the response size."""
    from concurrent.futures import ThreadPoolExecutor
    host, owner, name = split_repo(repo)
    total = sum(len(nums) for _, nums in groups)
    size = ITEM_BATCH
    if spread:      # one query per parallel slot rather than many small ones (each is a `gh` process)
        size = max(ITEM_BATCH, min(MAX_ITEM_BATCH, -(-total // FETCH_PARALLEL)))
    batches = [(is_pr, nums[i:i + size]) for is_pr, nums in groups for i in range(0, len(nums), size)]
    done = [0]

    def fetch(job):
        out = fetch_batch(repo, job[0], job[1])
        done[0] += len(job[1])
        progress("fetch", done[0], total, f"{repo}: {note}")
        if on_batch:
            on_batch(out)      # from a worker thread: the caller may draw what has arrived
        return out

    if len(batches) <= 1 or FETCH_PARALLEL <= 1:
        return [it for job in batches for it in fetch(job)]
    with ThreadPoolExecutor(max_workers=min(FETCH_PARALLEL, len(batches))) as pool:
        return [it for part in pool.map(fetch, batches) for it in part]   # in order; raises on failure


def refresh_items(repo, cached, on_batch=None):
    """Incremental update of the open items of a repo: only items whose updatedAt moved (or new ones) are fetched
    again; items that are no longer open are dropped. Returns (items, n_changed, n_dropped)."""
    listing = list_open(repo)
    by_key = {(it["is_pr"], it["number"]): it for it in cached}
    changed = [k for k, u in listing.items() if k not in by_key or (by_key[k].get("updated") or "") < u]
    dropped = [k for k in by_key if k not in listing]
    log(f"{repo}: {len(listing)} open, {len(changed)} changed, {len(dropped)} closed since the last fetch")
    progress("fetch", 0, len(changed) or None, f"{repo}: {len(changed)} changed")
    fresh = {}
    for is_pr in (False, True):
        nums = sorted(n for p, n in changed if p == is_pr)
        if nums:
            for it in fetch_items(repo, is_pr, nums, on_batch=on_batch):
                fresh[(it["is_pr"], it["number"])] = it
    items = [fresh.get(k) or by_key[k] for k in listing if k in fresh or k in by_key]
    items.sort(key=lambda it: it["created"], reverse=True)
    return items, len(changed), len(dropped)


def fetch_open_streaming(repo, on_batch=None):
    """Every open issue/PR of a repo, with the listing and the record fetch overlapped: each page of
    numbers (100 at a time, and the pages of a connection have to be walked in order) is handed to the
    record pool as soon as it lands, so the first records are already being fetched while the last
    pages are still being listed."""
    from concurrent.futures import ThreadPoolExecutor
    host, owner, name = split_repo(repo)
    futures, done, lock = [], [0], __import__("threading").Lock()
    pool = ThreadPoolExecutor(max_workers=FETCH_PARALLEL)

    def records(is_pr, batch):
        out = fetch_batch(repo, is_pr, batch)
        with lock:
            done[0] += len(batch)
            n = done[0]
        progress("fetch", n, None, f"{repo}: items")
        if on_batch:
            on_batch(out)
        return out

    def page_and_submit(q, is_pr):
        after = None
        while True:
            d = graphql(q, {"owner": owner, "name": name, "after": after, "states": ["OPEN"]}, host)
            conn = (d.get("repository") or {}).get("pullRequests" if is_pr else "issues")
            if conn is None:
                raise GhError(f"{repo}: repository not found on {host}")
            nums = sorted((n["number"] for n in conn["nodes"]), reverse=True)
            for i in range(0, len(nums), MAX_ITEM_BATCH):
                futures.append(pool.submit(records, is_pr, nums[i:i + MAX_ITEM_BATCH]))
            if not conn["pageInfo"]["hasNextPage"]:
                return
            after = conn["pageInfo"]["endCursor"]

    try:
        with ThreadPoolExecutor(max_workers=2) as lister:      # the two connections are independent
            list(lister.map(lambda a: page_and_submit(*a), ((Q_LIST, False), (Q_LIST_PR, True))))
        return [it for f in futures for it in f.result()]      # in submission order; raises on failure
    finally:
        pool.shutdown()


def fetch_repo(repo, state, on_batch=None):
    """Every issue/PR of a repo, for a cold cache. For the usual state="open" this lists the open
    numbers first (one cheap query per 100) and then pulls the records in parallel batches; a full
    `--state all` build still pages through the heavy connection query."""
    if state == "open":
        items = fetch_open_streaming(repo, on_batch)
        items.sort(key=lambda it: it["created"], reverse=True)
        if not items:
            log(f"{repo}: no open issues or PRs came back — run `gg check -r {repo}` to see why")
        return items
    host, owner, name = split_repo(repo)
    items = []
    for q, is_pr, states in ((Q_ISSUES, False, ["OPEN"]), (Q_PRS, True, ["OPEN"])):
        after = None
        while True:
            vars_ = {"owner": owner, "name": name, "after": after,
                     "states": states if state == "open" else None}
            data = graphql(q, vars_, host)
            if not data.get("repository"):
                raise GhError(f"{repo}: repository not found on {host} with any gh account "
                              f"({', '.join(gh_accounts(host)) or 'none logged in'})")
            conn = data["repository"]["issues" if not is_pr else "pullRequests"]
            for n in conn["nodes"]:
                items.append(_norm_item(repo, n, is_pr))
            log(f"{repo}: fetched {len(items)} items so far")
            progress("fetch", len(items), None, repo)
            if not conn["pageInfo"]["hasNextPage"]:
                break
            after = conn["pageInfo"]["endCursor"]
    if not items:
        log(f"{repo}: no {'open ' if state == 'open' else ''}issues or PRs came back — run `gg check -r {repo}` to see why")
    return items


def _cache_path(kind, repo, state=""):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{kind}__{repo.replace('/', '__')}{'__' + state if state else ''}.json")


def load_items(repo, state, max_age_min, refresh=False, on_batch=None):
    """Cached items of a repo. Within max_age they are used as they are; after that (state=open) only what changed on
    GitHub is fetched again; --refresh forces a full fetch."""
    p = _cache_path("items", repo, state)
    cached = None
    if not refresh and os.path.exists(p):
        with open(p) as f:
            d = json.load(f)
        if time.time() - d["fetched_at"] < max_age_min * 60:
            return d["items"], d["fetched_at"]
        cached = d["items"]
    if cached is not None and state == "open":
        try:
            items, _, _ = refresh_items(repo, cached, on_batch=on_batch)
            with open(p, "w") as f:
                json.dump({"fetched_at": time.time(), "repo": repo, "state": state, "items": items}, f)
            secure(p)
            return items, time.time()
        except GhError as e:
            log(f"{repo}: incremental refresh failed ({e}); fetching everything")
    items = fetch_repo(repo, state, on_batch=on_batch)
    with open(p, "w") as f:
        json.dump({"fetched_at": time.time(), "repo": repo, "state": state, "items": items}, f)
    secure(p)
    return items, time.time()


STUB_BATCH = 50


def resolve_stubs(repo, numbers, max_age_min):
    """Look up title/state for referenced-but-unfetched items of one repo, cached per repo."""
    return resolve_stubs_many({repo: numbers}, max_age_min).get(repo, {})


def resolve_stubs_many(by_repo, max_age_min):
    """{repo: numbers} -> {repo: {number: info}}, cached per repo under stubs__<repo>.json.

    Every repo's batches go through one pool, FETCH_PARALLEL queries at a time: a repo that references
    hundreds of closed items used to look them up STUB_BATCH at a time, one query after another, which
    was the last sequential stretch of a cold start (8s of a 21s build on a 584-item repo)."""
    from concurrent.futures import ThreadPoolExecutor
    now = time.time()
    caches, jobs = {}, []
    for repo, numbers in by_repo.items():
        cache = read_json(_cache_path("stubs", repo)) or {}
        caches[repo] = cache
        need = [n for n in numbers if str(n) not in cache
                or now - cache[str(n)].get("fetched_at", 0) > max_age_min * 60]
        for i in range(0, len(need), STUB_BATCH):
            jobs.append((repo, need[i:i + STUB_BATCH]))

    done = [0]

    def fetch(job):
        repo, batch = job
        host, owner, name = split_repo(repo)
        aliases = " ".join(
            f'n{n}: issueOrPullRequest(number:{n}){{ __typename '
            f'... on Issue{{ number title state createdAt body author{{login}} }} '
            f'... on PullRequest{{ number title state isDraft createdAt body author{{login}} }} }}'
            for n in batch)
        q = f'query {{ repository(owner:"{owner}", name:"{name}") {{ {aliases} }} }}'
        try:
            data = graphql(q, host=host)
        except GhError as e:
            log(f"stub resolve failed for {repo}: {e}")
            return repo, {}
        rep = (data or {}).get("repository") or {}
        out = {}
        for n in batch:
            s = rep.get(f"n{n}")
            if s:
                out[str(n)] = {"fetched_at": now, "is_pr": s["__typename"] == "PullRequest",
                               "title": s.get("title"), "state": s.get("state"),
                               "draft": s.get("isDraft", False), "created": s.get("createdAt"),
                               "author": _login(s.get("author")), "body": (s.get("body") or "")[:SUM_BODY_CHARS]}
            else:
                out[str(n)] = {"fetched_at": now, "missing": True}
        done[0] += len(batch)
        progress("stubs", done[0], sum(len(b) for _, b in jobs), repo)
        return repo, out

    if jobs:
        with ThreadPoolExecutor(max_workers=min(FETCH_PARALLEL, len(jobs))) as pool:
            for repo, part in pool.map(fetch, jobs):
                caches[repo].update(part)
        for repo in {r for r, _ in jobs}:
            path = _cache_path("stubs", repo)
            with open(path, "w") as f:
                json.dump(caches[repo], f)
            secure(path)
    out = {}
    for repo, numbers in by_repo.items():
        want = {str(n) for n in numbers}
        out[repo] = {int(k): v for k, v in caches[repo].items() if k in want}
    return out


# --------------------------------------------------------------------------
# graph model
# --------------------------------------------------------------------------
FENCE_RE = re.compile(r"```.*?```", re.S)
REF_RE = re.compile(r"(?<![\w/])(?:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(?P<num>\d+)\b")
URL_RE = re.compile(r"https?://(?P<host>[A-Za-z0-9.-]+)/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:issues|pull)/(?P<num>\d+)")
MENTION_RE = re.compile(r"(?<![\w/`])@(?P<login>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))(?![\w-])")
# lines that are pasted kernel logs / stack traces: "#14" there is a build number, not an issue
NOISE_LINE_RE = re.compile(
    r"^\s*(?:\[\s*\d+\.\d+\]|#\d+\s+0x[0-9a-f]+|.*\bPID:\s*\d+|.*\bTainted:|.*\bNot tainted\b"
    r"|.*\bHardware name:|.*\bCall Trace\b|.*\bRIP:|.*\bWARNING:.*\bat\b|.*\bBUG:"
    r"|.*\bPLATFORM\s+--|.*\bFSTYP\s+--|.*\bLinux/\w+ .*\d+\.\d+\.\d+)")
# "#N" with a tiny N is usually an ordinal ("overwrite #5", "attempt #2") unless a
# reference word precedes it or the repo is spelled out.
SMALL_REF = 20
REF_WORDS = {"pr", "prs", "issue", "issues", "pull", "see", "in", "of", "to", "by", "and", "with", "on",
             "fixes", "fix", "fixed", "closes", "close", "resolves", "resolved", "than", "from", "at",
             "ref", "refs", "cf", "via", "per", "like", "vs", "for", "as", "after", "before", "since"}


def _clean_lines(text):
    text = FENCE_RE.sub(" ", text or "")
    return [l for l in text.splitlines() if not NOISE_LINE_RE.match(l)]


def _plausible_small_ref(line, m, is_url=False):
    if not is_url and m.group("repo"):
        return True
    words = re.findall(r"[A-Za-z0-9_]+", line[:m.start()])
    if not words:
        return is_url  # a bare URL line is fine; a bare "#3" at line start is an ordinal
    return words[-1].lower() in REF_WORDS


SNIPPET_CHARS = 160


def snippet(line, start, end):
    """The sentence-ish context around line[start:end], collapsed to one line of ~SNIPPET_CHARS."""
    line = line.strip().lstrip("> ").strip()
    if len(line) <= SNIPPET_CHARS:
        return re.sub(r"\s+", " ", line)
    a = max(0, start - SNIPPET_CHARS // 2)
    b = min(len(line), end + SNIPPET_CHARS // 2)
    return ("…" if a else "") + re.sub(r"\s+", " ", line[a:b]).strip() + ("…" if b < len(line) else "")


def parse_refs_ctx(text, default_repo):
    """[((repo, num), snippet)] — every #N / owner/name#N / URL reference with the sentence it appears in."""
    out = []
    host = repo_host(default_repo)
    paras, cur = [], []
    for ln in _clean_lines(text):          # hard-wrapped markdown: a paragraph is one sentence source
        if ln.strip() and not ln.lstrip().startswith(("#", "|", "- ", "* ", "```")):
            cur.append(ln.strip().lstrip("> ").strip())
        else:
            if cur:
                paras.append(" ".join(cur))
                cur = []
            if ln.strip():
                paras.append(ln)
    if cur:
        paras.append(" ".join(cur))
    for raw in paras:
        line = raw
        for m in URL_RE.finditer(line):
            num = int(m.group("num"))
            if num <= SMALL_REF and not _plausible_small_ref(line, m, is_url=True):
                continue
            h = m.group("host").lower()
            if h.startswith("www.") or h.endswith(".github.com"):     # redirect.github.com (dependabot) etc.
                h = "github.com"
            out.append(((make_repo(h, *m.group("repo").split("/", 1)), num), snippet(raw, m.start(), m.end())))
        line = URL_RE.sub(" ", line)
        for m in REF_RE.finditer(line):
            num = int(m.group("num"))
            if num <= SMALL_REF and not _plausible_small_ref(line, m):
                continue
            out.append(((qualify(m.group("repo"), host) if m.group("repo") else default_repo, num),
                        snippet(raw, m.start(), m.end())))
    seen, res = set(), []
    for r, snip in out:
        if r[1] > 0 and r not in seen:
            seen.add(r)
            res.append((r, snip))
    return res


def parse_refs(text, default_repo):
    return [r for r, _ in parse_refs_ctx(text, default_repo)]


def parse_mentions(text):
    seen, res = set(), []
    for line in _clean_lines(text):
        for m in MENTION_RE.finditer(line):
            l = m.group("login")
            if len(l) < 2 or l.isdigit():
                continue  # "@5", "@D" from pasted logs; real logins are longer
            if l.lower() not in seen:
                seen.add(l.lower())
                res.append(l)
    return res


def ts(s):
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------
# translation (titles / comment first lines) through `claude -p`, cached
# --------------------------------------------------------------------------
HAN_RE = re.compile(r"[一-鿿㐀-䶿]")
HANGUL_RE = re.compile(r"[가-힣]")
TR_LANG = cfg("lang")
TR_MODEL = cfg("tr_model")
DEFAULT_TRANSLATE = cfg("translate")
CLAUDE_BIN = cfg("claude_bin")
IS_CLAUDE = os.path.basename(CLAUDE_BIN) == "claude"   # a variant binary keeps its own default model (no --model)


def model_label(model):
    return f"{os.path.basename(CLAUDE_BIN)} {model}" if IS_CLAUDE else os.path.basename(CLAUDE_BIN)
TR_BATCH = 60
PENDING_TEXT = "요약 중…" if TR_LANG.lower().startswith("korean") else "summarizing…"
TR_PROMPT = """Translate every string in the JSON array below into {lang}. Rules:
- Output ONLY a JSON array of strings, same length and same order, no code fence, no commentary.
- Keep technical terms in English as-is: mount, umount, fsck, inode, extent, zone, checkpoint, deadlock, \
hang, panic, assert, GC, journal, xfstests, generic/NNN, LTP, fio, KASAN, lockdep, syzkaller, DIO, fsync.
- Keep #numbers, file paths, function names, identifiers, digits and English words unchanged.
- Never use Chinese characters in the output. Glossary: 合入=머지, 用例=테스트 케이스, 告警=경고, 卡住=hang, \
死锁=deadlock, 复现=재현, 失败=실패, 挂起=suspend, 反向索引=reverse index, 记数器/计数器=counter, \
末端=near-full, 项目问题=프로젝트 이슈, 稳定性=안정성.
- Be concise: these are issue titles and the first line of comments.

{payload}"""


def needs_translation(text, mode):
    if not text or mode == "none" or HANGUL_RE.search(text):
        return False
    if mode == "zh":
        return bool(HAN_RE.search(text))
    return bool(re.search(r"[A-Za-z一-鿿]", text))


USAGE = {"calls": 0, "input": 0, "cache_read": 0, "cache_create": 0, "output": 0, "cost_usd": 0.0, "by": {}}
USAGE_T0 = time.time()


AI_BACKENDS = {   # name -> (how it is run non-interactively, login hint)
    "claude": ("Claude Code: claude -p --output-format json --model M PROMPT", "claude (then /login)"),
    "codex":  ("OpenAI Codex CLI: codex exec --skip-git-repo-check --ephemeral -s read-only -o FILE PROMPT", "codex login"),
    "gemini": ("Google Gemini CLI: gemini -p PROMPT", "gemini (sign in once)"),
    "grok":   ("xAI grok CLI: grok -p PROMPT", "grok (API key / sign in)"),
}


def ai_backend(binpath=None):
    """Which backend the configured AI CLI is: claude | cla | codex | gemini | grok | generic."""
    b = os.path.basename(binpath or CLAUDE_BIN).lower()
    for name in ("claude", "codex", "gemini", "grok"):
        if b == name or b.startswith(name + "-") or b.startswith(name + "_"):
            return name
    return "generic"


AI_FAILURES = []   # {"bin", "msg", "t"} appended when the AI CLI fails; the tui offers to switch


def switch_ai(name):
    """Use another AI CLI from now on (this process and the config file)."""
    global CLAUDE_BIN, IS_CLAUDE
    CLAUDE_BIN = name
    IS_CLAUDE = os.path.basename(name) == "claude"
    CONFIG["claude_bin"] = name
    save_config()


def ai_available():
    import shutil
    return bool(shutil.which(CLAUDE_BIN))


def installed_ais(exclude=None):
    import shutil
    return [n for n in AI_BACKENDS if n != exclude and shutil.which(n)]


def claude_call(prompt, model, phase, timeout=300, cwd=None, tools=()):
    """Run the configured AI CLI non-interactively; return the reply text and add its usage (claude only) to USAGE.

    cwd: the directory the CLI runs in — a PR worktree for review, so the model can read the code.
    tools: tool names it may use there (claude/codex only); empty means the text-only default.
    """
    try:
        return _ai_call(prompt, model, phase, timeout, cwd, tools)
    except Exception as e:  # noqa: BLE001
        AI_FAILURES.append({"bin": CLAUDE_BIN, "msg": str(e)[:200], "t": time.time()})
        raise


def _ai_call(prompt, model, phase, timeout=300, cwd=None, tools=()):
    kind = ai_backend()
    outfile = None
    stdin_text = None
    if kind == "claude":
        # no --bare: bare mode skips the stored login and answers "Not logged in"
        cmd = [CLAUDE_BIN, "-p", "--no-session-persistence", "--output-format", "json", "--model", model]
        if tools:
            cmd += ["--allowedTools"] + list(tools)
        if cwd:
            cmd += ["--add-dir", cwd]
        if tools or cwd:
            # --allowedTools and --add-dir are variadic, so a prompt after them is swallowed as one of
            # their values ("Input must be provided either through stdin or as a prompt argument").
            # stdin also keeps a review prompt — a whole diff — clear of ARG_MAX.
            stdin_text = prompt
        else:
            cmd.append(prompt)
    elif kind == "codex":
        os.makedirs(CACHE_DIR, exist_ok=True)
        outfile = os.path.join(CACHE_DIR, f"codex_last_{os.getpid()}_{int(time.time() * 1000)}.txt")
        cmd = [CLAUDE_BIN, "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only", "--color", "never",
               "-o", outfile, prompt]
    else:                                   # gemini, grok, anything else that takes -p PROMPT
        cmd = [CLAUDE_BIN, "-p", prompt]
    try:
        r = subprocess.run(cmd, input=stdin_text,
                           stdin=None if stdin_text is not None else subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout,
                           cwd=cwd or None, start_new_session=True)   # no controlling terminal: the child cannot touch our screen
    except FileNotFoundError:
        raise ValueError(f"{CLAUDE_BIN} not found (gg ai to pick an AI CLI)") from None
    if kind != "claude":
        text = ""
        if outfile:
            try:
                with open(outfile) as f:
                    text = f.read()
                os.remove(outfile)
            except OSError:
                pass
        text = (text or r.stdout or "").strip()
        if not text or r.returncode:
            err = (r.stderr or r.stdout or "").strip().splitlines()
            raise ValueError((err[-1] if err else f"{CLAUDE_BIN} exited {r.returncode}")[:300])
        rec = USAGE["by"].setdefault(phase, {"calls": 0, "input": 0, "cache_read": 0, "cache_create": 0, "output": 0, "cost_usd": 0.0})
        for tgt in (USAGE, rec):
            tgt["calls"] += 1
        return text
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise ValueError((r.stderr or r.stdout or "").strip()[:300] or f"{CLAUDE_BIN} exited {r.returncode}") from None
    u = d.get("usage") or {}
    rec = USAGE["by"].setdefault(phase, {"calls": 0, "input": 0, "cache_read": 0, "cache_create": 0, "output": 0, "cost_usd": 0.0})
    for tgt in (USAGE, rec):
        tgt["calls"] += 1
        tgt["input"] += u.get("input_tokens", 0)
        tgt["cache_read"] += u.get("cache_read_input_tokens", 0)
        tgt["cache_create"] += u.get("cache_creation_input_tokens", 0)
        tgt["output"] += u.get("output_tokens", 0)
        tgt["cost_usd"] += d.get("total_cost_usd", 0.0) or 0.0
    if d.get("is_error"):
        raise ValueError(str(d.get("result", ""))[:300])
    return d.get("result") or ""


def fmt_tokens(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def usage_line():
    u = USAGE
    if not u["calls"]:
        return "tokens: none yet"
    return (f"tokens {fmt_tokens(u['input'] + u['cache_read'] + u['cache_create'])} in / "
            f"{fmt_tokens(u['output'])} out · ${u['cost_usd']:.3f} · {u['calls']} calls")


def usage_report():
    el = int(time.time() - USAGE_T0)
    lines = [f"claude usage since start ({el // 60}m{el % 60:02d}s ago)", "",
             f"{'phase':12} {'calls':>5} {'input':>8} {'cache-rd':>9} {'cache-wr':>9} {'output':>8} {'cost':>9}"]
    for phase, r in sorted(USAGE["by"].items()):
        lines.append(f"{phase:12} {r['calls']:>5} {r['input']:>8} {r['cache_read']:>9} {r['cache_create']:>9} "
                     f"{r['output']:>8} ${r['cost_usd']:>8.4f}")
    u = USAGE
    lines.append(f"{'total':12} {u['calls']:>5} {u['input']:>8} {u['cache_read']:>9} {u['cache_create']:>9} "
                 f"{u['output']:>8} ${u['cost_usd']:>8.4f}")
    lines += ["", "input = fresh prompt tokens; cache-rd/wr = prompt-cache reads/writes (Claude Code's system prompt is",
              "cached, so cache-rd is large and cheap); cost = what `claude -p` reports (list price)."]
    return lines


import threading
_cache_lock = threading.Lock()


def cache_merge(path, updates):
    """Merge `updates` into the JSON dict at path — safe for several threads finishing at the same time."""
    with _cache_lock:
        d = read_json(path) or {}
        d.update(updates)
        write_json(path, d)


def claude_json(prompt, n, phase="translate"):
    """claude_call() whose reply must be a JSON array of n strings."""
    out = claude_call(prompt, TR_MODEL, phase)
    m = re.search(r"\[.*\]", out, re.S)
    arr = json.loads(m.group(0)) if m else None
    if not isinstance(arr, list) or len(arr) != n:
        raise ValueError(f"unexpected reply: {out[:200]!r}")
    return arr


SUM_BATCH_CHARS = 40000
SUM_BODY_CHARS = 4000
SUM_PROMPT = """Summarize each GitHub text in the JSON array below (kind = issue, pull request or comment) in ONE line \
of {lang}, at most 70 characters. For an issue/PR say what it is about (the problem or the change); for a comment say \
what it does: a finding, a question, a request, a decision, a status update, a measurement, an ack. \
Keep identifiers, #numbers, file names, function names and technical terms in English. Never use Chinese characters. \
Output ONLY a JSON array of strings, same length and same order as the input, no code fence, no commentary.

{payload}"""


def summarize_comments(entries, lang=TR_LANG):
    """entries: list of (key, payload). Returns key -> one-line summary; cached in summaries.json."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = os.path.join(CACHE_DIR, "summaries.json")
    cache = {}
    if os.path.exists(p):
        with open(p) as f:
            cache = json.load(f)
    out, need = {}, []
    for k, d in entries:
        ck = f"{lang}:{k}"
        if ck in cache:
            out[k] = cache[ck]
        else:
            need.append((k, d))
    batches, cur, size = [], [], 0
    for k, d in need:
        n = len(json.dumps(d, ensure_ascii=False))
        if cur and (size + n > SUM_BATCH_CHARS or len(cur) >= 40):
            batches.append(cur)
            cur, size = [], 0
        cur.append((k, d))
        size += n
    if cur:
        batches.append(cur)
    done = 0
    for bi, batch in enumerate(batches, 1):
        log(f"summarizing {len(batch)} comments in {lang} via {model_label(TR_MODEL)}")
        progress("summarize", done, len(need), f"batch {bi}/{len(batches)}, {len(batch)} comments")
        done += len(batch)
        prompt = SUM_PROMPT.format(lang=lang, payload=json.dumps([d for k, d in batch], ensure_ascii=False))
        try:
            arr = claude_json(prompt, len(batch), phase="summarize")
        except Exception as e:  # noqa: BLE001 - best effort
            log(f"summary failed, keeping excerpts: {e}")
            break
        new = {}
        for (k, d), sm in zip(batch, arr):
            if isinstance(sm, str) and sm.strip():
                new[f"{lang}:{k}"] = sm.strip()
                out[k] = sm.strip()
        cache_merge(p, new)
    return out


ASK_MODEL = cfg("ask_model")
ASK_MAX_CHARS = 90000
ASK_PROMPT = """You are helping a developer who is reading a GitHub {kind} in a terminal tool. The material below is \
what they see: the item (or the comment their cursor is on), its metadata, the full comment thread in order, and the \
issues/PRs it is linked to with the sentence that made each link. Answer the question from this material; when you \
rely on a specific comment or linked item, say which (author + date, or #number). If the material does not contain the \
answer, say so rather than guessing. Answer in {lang}; keep identifiers, code, file names, #numbers and technical terms \
in English. Be concise (under 300 words) unless the question asks for more.

=== {label} ===
{context}

=== question ===
{question}"""


def ask_context(g, nid):
    """(kind, label, text): the node under the cursor with everything around it, capped at ASK_MAX_CHARS."""
    n = g.nodes[nid]

    def item_block(it, body_chars, comment_chars, mark=None):
        parts = [f"{g.label_num(it)} {kind_tag(it)} {it.title}",
                 f"by @{it.author}, opened {short_date(it.created)}, updated {short_date(it.updated)}, "
                 f"labels: {', '.join(it.labels) or '-'}, {it.url or ''}", "", (it.body or "(no body)")[:body_chars]]
        cs = g.comments_of(it.id)
        if cs:
            parts.append(f"\n--- {len(cs)} comments, oldest first ---")
        for c in cs:
            tag = f" [{c.ckind}{' ' + (c.review_state or '') if c.review_state else ''}]" if c.ckind != "comment" else ""
            flag = "  <<< THE COMMENT THE USER IS LOOKING AT" if c.id == mark else ""
            parts += ["", f"--- comment by @{c.author}, {short_date(c.created)} ({rel_days(c, g)}){tag}{flag} ---",
                      c.body[:comment_chars]]
        links = []
        for src in [it] + cs:
            for m, t, o in g.adj[src.id]:
                if t == "comment" or g.nodes[m].kind == "person" or m == it.id:
                    continue
                other = g.nodes[m]
                why = g.ctx.get((src.id, m)) if o else g.ctx.get((m, it.id))
                why = f'"{why}"' if why else (f"» {other.summary}" if other.summary else "")
                links.append(f"- {EDGE_LABEL[(t, o)]}{node_label(g, other, 120)}" + (f"  — {why}" if why else ""))
        if links:
            parts += ["", "--- linked issues / PRs (how they are connected) ---"] + list(dict.fromkeys(links))[:40]
        return parts

    if n.kind == "comment":
        p = g.nodes.get(n.parent)
        parts = [f"The user's cursor is on this comment by @{n.author} ({short_date(n.created)}, {rel_days(n, g)} "
                 f"after the item opened):", "", n.body, ""]
        if p:
            parts += [f"=== the {'PR' if p.is_pr else 'issue'} it belongs to, with the whole thread ==="] + \
                     item_block(p, 4000, 2500, mark=n.id)
        return "comment", f"comment on {g.label_num(p) if p else '?'}", "\n".join(parts)
    if n.kind == "person":
        lines = [f"{n.id} appears in:"]
        for m, t, o in sorted(g.adj[nid], key=lambda e: -g.nodes[e[0]].time):
            if t == "mention":
                lines.append(f"- {node_label(g, g.nodes[m], 200)}")
        return "person", n.id, "\n".join(lines)
    kind = "pull request" if n.is_pr else "issue"
    text = "\n".join(item_block(n, 12000, 6000))
    if len(text) > ASK_MAX_CHARS:
        text = text[:ASK_MAX_CHARS] + "\n\n[... truncated ...]"
    return kind, f"{g.label_num(n)} {n.title}", text


def ask_claude(g, nid, question, model=ASK_MODEL, lang=TR_LANG):
    kind, label, context = ask_context(g, nid)
    prompt = ASK_PROMPT.format(kind=kind, lang=lang, label=label, context=context, question=question)
    out = claude_call(prompt, model, "ask", timeout=600).strip()
    if not out:
        raise ValueError("claude returned no text")
    return out


TR_FULL_CHARS = 12000
TR_FULL_PROMPT = """Translate the following GitHub {kind} text into {lang}. Keep the markdown structure, code blocks, \
identifiers, file names, function names, #numbers, @logins and URLs exactly as they are; translate only the prose. \
Output only the translation, no commentary.

{body}"""


def _split_prose(body):
    """[(is_prose, text)] — code fences and pasted log / trace lines are kept verbatim, the rest is prose."""
    parts, cur, in_fence, cur_prose = [], [], False, True

    def flush():
        if cur:
            parts.append((cur_prose, "\n".join(cur)))

    for ln in body.splitlines():
        fence = ln.strip().startswith("```")
        prose = not in_fence and not fence and not NOISE_LINE_RE.match(ln)
        if fence:
            if not in_fence:
                flush(); cur = []; cur_prose = False
                cur.append(ln); in_fence = True
            else:
                cur.append(ln); in_fence = False
                flush(); cur = []; cur_prose = True
            continue
        if prose != cur_prose:
            flush(); cur = []; cur_prose = prose
        cur.append(ln)
    flush()
    return parts


def translate_body(n, g, lang=TR_LANG):
    """Full-text translation of an item/comment body (see translate_text)."""
    kind = "comment" if n.kind == "comment" else ("pull request" if n.is_pr else "issue")
    return translate_text(n.body or "", kind, lang)


def translate_text(text, kind="issue", lang=TR_LANG):
    """Prose only (code fences and log lines stay), long bodies split into ~1,200-character chunks
    translated in parallel (AI_PARALLEL). Cached by text hash in translations_full.json."""
    body = (text or "")[:TR_FULL_CHARS]
    if not body.strip():
        return ""
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = os.path.join(CACHE_DIR, "translations_full.json")
    key = f"{lang}:{hashlib.sha1(body.encode('utf-8')).hexdigest()}"
    cached = (read_json(p) or {}).get(key)
    if cached:
        return cached
    pieces = _split_prose(body)
    prose_idx = [i for i, (is_p, t) in enumerate(pieces) if is_p and t.strip()]
    if not prose_idx:
        return body
    # group consecutive prose pieces into chunks of ~2500 chars
    chunks, cur, size = [], [], 0
    for i in prose_idx:
        t = pieces[i][1]
        if cur and size + len(t) > 1200:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += len(t)
    if cur:
        chunks.append(cur)

    def translate_chunk(idx_list):
        marked = "\n\n<<<SEG>>>\n\n".join(pieces[i][1] for i in idx_list)
        tr = claude_call(TR_FULL_PROMPT.format(kind=kind, lang=lang, body=marked) +
                         "\n\n(The text contains <<<SEG>>> separators between independent parts: keep every separator "
                         "exactly, in the same order.)", TR_MODEL, "translate", timeout=600).strip()
        got = [x.strip() for x in tr.split("<<<SEG>>>")]
        return got if len(got) == len(idx_list) else [tr] + [""] * (len(idx_list) - 1)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=AI_PARALLEL) as ex:
        results = list(ex.map(translate_chunk, chunks))
    translated = {}
    for idx_list, got in zip(chunks, results):
        for i, t in zip(idx_list, got):
            translated[i] = t
    out = "\n".join(translated.get(i, t) if is_p else t for i, (is_p, t) in enumerate(pieces))
    if out.strip():
        cache_merge(p, {key: out})
    return out


WHY_PROMPT = """Each entry below is a sentence from a GitHub issue, pull request or comment that mentions another item, \
given as "ref" (like #748). For each entry write, in {lang}, one sentence of at most 90 characters that says WHY that item \
is mentioned and what it is — e.g. "충돌 여부를 확인한 관련 PR", "이 버그를 고치는 PR", "같은 증상을 보고한 issue", "재현 절차 출처". \
Keep #numbers, identifiers and technical terms in English; never use Chinese characters. \
Output ONLY a JSON array of strings, same length and order as the input, no code fence.

{payload}"""


def around(ctx, ref, n=70):
    """The part of ctx within ~n characters of ref, cut at word boundaries (a short quote for narrow panels)."""
    i = ctx.find(ref)
    if i < 0:
        return trunc(ctx, 2 * n)
    a, b = max(0, i - n), min(len(ctx), i + len(ref) + n)
    if a > 0:
        sp = ctx.rfind(" ", 0, a + 12)
        a = sp + 1 if sp >= 0 and sp + 1 <= i else a
    if b < len(ctx):
        sp = ctx.find(" ", b - 12)
        b = sp if sp >= i + len(ref) else b
    return ("…" if a else "") + ctx[a:b].strip() + ("…" if b < len(ctx) else "")


WEAK_WHY = re.compile(r"^(선행(하는)?\s*)?(관련|연관|참조|언급)?\s*(된|한)?\s*(PR|issue|이슈|항목|커밋)?\s*$", re.I)


def weak_why(text):
    return not text or len(text) < 8 or bool(WEAK_WHY.match(text))


def summarize_whys(entries, lang=TR_LANG):
    """entries: [(key, {"ref":..., "sentence":...})] -> key -> phrase; cached in whys.json."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = os.path.join(CACHE_DIR, "whys.json")
    cache = {}
    if os.path.exists(p):
        with open(p) as f:
            cache = json.load(f)
    out, need = {}, []
    for k, d in entries:
        if f"{lang}:{k}" in cache:
            out[k] = cache[f"{lang}:{k}"]
        else:
            need.append((k, d))
    for i in range(0, len(need), 40):
        batch = need[i:i + 40]
        log(f"explaining {len(batch)} links in {lang} via {model_label(TR_MODEL)}")
        progress("summarize", i, len(need), f"link reasons, batch of {len(batch)}")
        try:
            arr = claude_json(WHY_PROMPT.format(lang=lang, payload=json.dumps([d for _, d in batch], ensure_ascii=False)),
                              len(batch), phase="summarize")
        except Exception as e:  # noqa: BLE001
            log(f"link reasons failed: {e}")
            break
        new = {}
        for (k, _), txt in zip(batch, arr):
            if isinstance(txt, str) and txt.strip():
                new[f"{lang}:{k}"] = txt.strip()
                out[k] = txt.strip()
        cache_merge(p, new)
    return out


def prepare_whys(g, pairs):
    """pairs: [(src id, dst id)] with a sentence in g.ctx -> fill g.why. Returns the number explained."""
    entries, keys = [], {}
    for src, dst in pairs:
        ctx = g.ctx.get((src, dst))
        if not ctx or (src, dst) in g.why:
            continue
        dn = g.nodes.get(dst)
        ref = g.label_num(dn) if dn else dst
        k = hashlib.sha1(f"{ref}|{ctx}".encode("utf-8")).hexdigest()
        keys.setdefault(k, []).append((src, dst))
        if len(entries) < len(keys):
            entries.append((k, {"ref": ref, "sentence": ctx}))
    if not entries:
        return 0
    res = summarize_whys(entries)
    for k, txt in res.items():
        for pair in keys[k]:
            g.why[pair] = txt
    return len(res)


def prepare_summaries(g):
    """Attach a one-line summary to every comment and item node of g that has a body. Returns the count."""
    g.summarized = 0
    entries, targets = {}, defaultdict(list)
    for n in g.nodes.values():
        if n.kind not in ("comment", "item") or not (n.body or "").strip():
            continue
        key = hashlib.sha1((n.kind + ":" + n.body).encode("utf-8")).hexdigest()
        if key not in entries:
            if n.kind == "comment":
                parent = g.nodes.get(n.parent)
                entries[key] = {"kind": "comment", "item": (parent.tr_title or parent.title) if parent else "",
                                "author": n.author, "body": n.body[:SUM_BODY_CHARS]}
            else:
                entries[key] = {"kind": "pull request" if n.is_pr else "issue", "item": n.tr_title or n.title or "",
                                "author": n.author, "body": n.body[:SUM_BODY_CHARS]}
        targets[key].append(n)
    if not entries:
        return 0
    sm = summarize_comments(list(entries.items()))
    for key, text in sm.items():
        for n in targets[key]:
            n.summary = text
    g.summarized = len(sm)
    return len(sm)


def translate_texts(texts, lang=TR_LANG):
    """text -> translation. Cached in ~/.cache/gitgraph/translations.json; new strings go to `claude -p`."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = os.path.join(CACHE_DIR, "translations.json")
    cache = {}
    if os.path.exists(p):
        with open(p) as f:
            cache = json.load(f)
    out, need = {}, []
    for t in dict.fromkeys(texts):
        k = f"{lang}:{t}"
        if k in cache:
            out[t] = cache[k]
        else:
            need.append(t)
    for i in range(0, len(need), TR_BATCH):
        batch = need[i:i + TR_BATCH]
        log(f"translating {len(batch)} strings to {lang} via {model_label(TR_MODEL)}")
        progress("translate", i, len(need), f"{len(batch)} strings in this batch")
        prompt = TR_PROMPT.format(lang=lang, payload=json.dumps(batch, ensure_ascii=False))
        try:
            arr = claude_json(prompt, len(batch))
        except Exception as e:  # noqa: BLE001 - translation is best effort
            log(f"translation failed, keeping originals: {e}")
            break
        new = {}
        for t, tr in zip(batch, arr):
            if isinstance(tr, str) and tr.strip():
                new[f"{lang}:{t}"] = tr
                out[t] = tr
        cache_merge(p, new)
    return out


def prepare_translations(g, mode):
    """Attach tr_title / tr_excerpt to the nodes of g. Returns number of translated strings."""
    g.translated = 0
    if mode == "none":
        return 0
    want = defaultdict(list)
    for n in g.nodes.values():
        if n.kind == "item" and needs_translation(n.title, mode):
            want[n.title].append((n, "tr_title"))
        elif n.kind == "comment":
            raw = raw_excerpt(n.body)
            if needs_translation(raw, mode):
                want[raw].append((n, "tr_excerpt"))
    if not want:
        return 0
    tr = translate_texts(list(want))
    for text, targets in want.items():
        if text in tr:
            for n, attr in targets:
                setattr(n, attr, tr[text])
    g.translated = len(tr)
    return len(tr)


class Node:
    def __init__(self, kind, nid, **kw):
        self.kind = kind          # item | comment | person
        self.id = nid
        self.repo = kw.get("repo")
        self.number = kw.get("number")
        self.is_pr = kw.get("is_pr")
        self.title = kw.get("title")
        self.state = kw.get("state")          # OPEN / CLOSED / MERGED / None
        self.draft = kw.get("draft", False)
        self.body = kw.get("body", "")
        self.created = kw.get("created")
        self.updated = kw.get("updated")
        self.url = kw.get("url")
        self.author = kw.get("author")
        self.labels = kw.get("labels", [])
        self.stub = kw.get("stub", False)     # not fetched (closed / other repo)
        self.parent = kw.get("parent")        # comment -> item id
        self.ckind = kw.get("ckind")          # comment | review | review_comment
        self.review_state = kw.get("review_state")
        self.comments_total = kw.get("comments_total", 0)
        self.inline_mentions = []             # shown as text when people=False
        self.mention_count = 0                # person nodes
        self.tr_title = None                  # translated title / excerpt (see prepare_translations)
        self.tr_body = None                   # full-text translation of the body (tui: i)
        self.tr_excerpt = None
        self.summary = None                   # one-line comment summary (see prepare_summaries)
        self.summary_pending = False          # tui: a summary for this comment is being generated

    @property
    def time(self):
        return ts(self.created)

    def state_label(self):
        if self.kind != "item":
            return ""
        if self.state == "OPEN":
            return "draft" if self.draft else "open"
        if self.state == "MERGED":
            return "merged"
        if self.state == "CLOSED":
            return "closed"
        return "?"


class Graph:
    def __init__(self, primary_repo):
        self.primary = primary_repo
        self.nodes = {}
        self.edges = set()      # (src, dst, type)
        self.adj = defaultdict(list)
        self.show_linked = True  # False when comment nodes were folded away (comments="none")
        self.fetched_at = None
        self.translated = 0
        self.summarized = 0
        self.ctx = {}            # (src node id, dst item id) -> sentence in which src references dst
        self.why = {}            # (src node id, dst item id) -> one-line reason (claude), see prepare_whys

    def item_id(self, repo, number):
        return f"{repo}#{number}"

    def ensure_item(self, repo, number, **kw):
        nid = self.item_id(repo, number)
        n = self.nodes.get(nid)
        if n is None:
            n = Node("item", nid, repo=repo, number=number, stub=True, **kw)
            self.nodes[nid] = n
        else:
            for k, v in kw.items():
                if v is not None and getattr(n, k, None) in (None, "", False):
                    setattr(n, k, v)
        return n

    def ensure_person(self, login):
        nid = f"@{login}"
        key = nid.lower()
        for k in self.nodes:
            if k.lower() == key:
                return self.nodes[k]
        n = Node("person", nid, title=login)
        self.nodes[nid] = n
        return n

    def add_edge(self, src, dst, typ):
        if src == dst:
            return
        self.edges.add((src, dst, typ))

    def finalize(self):
        # drop ref edges shadowed by closes edges
        closes = {(s, d) for s, d, t in self.edges if t == "closes"}
        self.edges = {e for e in self.edges if not (e[2] == "ref" and (e[0], e[1]) in closes)}
        self.adj = defaultdict(list)
        for s, d, t in sorted(self.edges):
            self.adj[s].append((d, t, True))
            self.adj[d].append((s, t, False))
        for n in self.nodes.values():
            if n.kind == "person":
                srcs = [self.nodes[m] for m, t, out in self.adj[n.id] if t == "mention"]
                n.mention_count = len(srcs)
                if srcs:
                    n.created = min((s.created for s in srcs if s.created), default=None)

    def neighbors(self, nid, typ=None, out=None):
        for m, t, o in self.adj.get(nid, []):
            if typ is not None and t != typ:
                continue
            if out is not None and o != out:
                continue
            yield m, t, o

    def comments_of(self, item_id):
        cs = [self.nodes[m] for m, t, o in self.adj.get(item_id, []) if t == "comment"]
        cs.sort(key=lambda c: c.time)
        return cs

    def label_num(self, n):
        if n.repo == self.primary:
            return f"#{n.number}"
        return f"{n.repo.split('/')[-1]}#{n.number}"


def build_graph(repos, state, max_age_min, refresh=False, on_batch=None):
    """Fetch (or read from the cache) every repo and assemble the graph. `on_batch(items)` is called
    from the fetch threads as each batch of items lands, so a caller can draw what is there already."""
    fetched_at = None
    all_items = []
    for repo in repos:
        items, fa = load_items(repo, state, max_age_min, refresh, on_batch=on_batch)
        fetched_at = min(fetched_at, fa) if fetched_at else fa
        all_items.extend(items)
    g = assemble_graph(repos[0], all_items, max_age_min)
    g.fetched_at = fetched_at
    return g


def assemble_graph(primary_repo, all_items, max_age_min=None, resolve=True):
    """Items -> Graph: parse references and mentions, add the timeline cross references, and (unless
    `resolve` is off — a partly fetched repo has nothing to look up yet) fill in the referenced items
    that were not fetched. Pure except for the stub lookups, so it can run on a partial item list."""
    g = Graph(primary_repo)
    for it in all_items:
        nid = g.item_id(it["repo"], it["number"])
        g.nodes[nid] = Node("item", nid, repo=it["repo"], number=it["number"], is_pr=it["is_pr"],
                            title=it["title"], state=it["state"], draft=it["draft"], body=it["body"],
                            created=it["created"], updated=it["updated"], url=it["url"],
                            author=it["author"], labels=it["labels"], stub=False,
                            comments_total=len(it["comments"]))

    parsed_pairs = set()   # (src item id, dst item id) already covered by body/comment parsing
    for it in all_items:
        iid = g.item_id(it["repo"], it["number"])
        for (repo, num), snip in parse_refs_ctx(it["body"], it["repo"]):
            t = g.ensure_item(repo, num)
            g.add_edge(iid, t.id, "ref")
            parsed_pairs.add((iid, t.id))
            g.ctx.setdefault((iid, t.id), snip)
        for login in parse_mentions(it["body"]):
            g.add_edge(iid, g.ensure_person(login).id, "mention")
        for c in it["closes"]:
            t = g.ensure_item(qualify(c["repo"], repo_host(it["repo"])), c["number"])
            g.add_edge(iid, t.id, "closes")
            parsed_pairs.add((iid, t.id))
        for c in it["comments"]:
            cid = f"{iid}/{c['id']}"
            g.nodes[cid] = Node("comment", cid, repo=it["repo"], number=it["number"], parent=iid,
                                body=c["body"], created=c["created"], url=c["url"], author=c["author"],
                                ckind=c["kind"], review_state=c["review_state"])
            g.add_edge(cid, iid, "comment")
            for (repo, num), snip in parse_refs_ctx(c["body"], it["repo"]):
                t = g.ensure_item(repo, num)
                g.add_edge(cid, t.id, "ref")
                parsed_pairs.add((iid, t.id))
                g.ctx.setdefault((cid, t.id), snip)
            for login in parse_mentions(c["body"]):
                g.add_edge(cid, g.ensure_person(login).id, "mention")
    # incoming cross references not explained by parsed bodies (closed / other-repo sources)
    for it in all_items:
        iid = g.item_id(it["repo"], it["number"])
        for x in it["crossrefs"]:
            s = g.ensure_item(qualify(x["repo"], repo_host(it["repo"])), x["number"], is_pr=x["is_pr"], title=x["title"],
                              state=x["state"], draft=x["draft"], created=x["created"], author=x["author"])
            if (s.id, iid) not in parsed_pairs:
                g.add_edge(s.id, iid, "ref")
    # resolve stubs
    by_repo = defaultdict(list)
    for n in g.nodes.values() if resolve else ():
        if n.kind == "item" and n.stub and (n.title is None or n.state is None):
            by_repo[n.repo].append(n.number)
    # every repo's referenced items in one pool (they are kept a day)
    resolved = resolve_stubs_many({r: sorted(set(nums)) for r, nums in by_repo.items()},
                                  max(max_age_min or 0, 24 * 60)).items() if by_repo else []
    for repo, info in resolved:
        for num, v in info.items():
            n = g.nodes[g.item_id(repo, num)]
            if v.get("missing"):
                continue
            n.is_pr, n.title, n.state = v["is_pr"], v["title"], v["state"]
            n.draft, n.created, n.author = v["draft"], v["created"], v["author"]
            n.body = v.get("body") or n.body
    g.finalize()
    return g


def apply_filters(g, comments="linked", people=True, closed_neighbors=True):
    """Return a new Graph restricted per display options."""
    h = Graph(g.primary)
    h.fetched_at = g.fetched_at
    h.show_linked = comments != "none"
    h.ctx, h.why = g.ctx, g.why
    keep = set(g.nodes)
    if not people:
        keep -= {i for i, n in g.nodes.items() if n.kind == "person"}
    if not closed_neighbors:
        keep -= {i for i, n in g.nodes.items() if n.kind == "item" and n.stub and n.state in ("CLOSED", "MERGED")}
    edges = {e for e in g.edges if e[0] in keep and e[1] in keep}
    # inline mentions when people are hidden
    inline = defaultdict(list)
    if not people:
        for s, d, t in g.edges:
            if t == "mention":
                inline[s].append(d)
    if comments == "none":
        for s, d, t in list(edges):
            if g.nodes[s].kind == "comment" and t != "comment":
                edges.add((g.nodes[s].parent, d, t))
        keep -= {i for i, n in g.nodes.items() if n.kind == "comment"}
        edges = {e for e in edges if e[0] in keep and e[1] in keep}
        for s in list(inline):
            if g.nodes[s].kind == "comment":
                inline[g.nodes[s].parent].extend(inline.pop(s))
    elif comments == "linked":
        linked = {s for s, d, t in edges if g.nodes[s].kind == "comment" and t != "comment"}
        keep -= {i for i, n in g.nodes.items() if n.kind == "comment" and i not in linked}
        edges = {e for e in edges if e[0] in keep and e[1] in keep}
    for i in keep:
        h.nodes[i] = g.nodes[i]
        h.nodes[i].inline_mentions = sorted(set(inline.get(i, [])))
    h.edges = edges
    h.finalize()
    return h


def components(g):
    """Connected components over non-person nodes; persons hang off as leaves."""
    seen, comps = set(), []
    for nid, n in g.nodes.items():
        if n.kind == "person" or nid in seen:
            continue
        comp, q = set(), deque([nid])
        seen.add(nid)
        while q:
            x = q.popleft()
            comp.add(x)
            for m, t, o in g.adj[x]:
                if g.nodes[m].kind == "person":
                    comp.add(m)
                    continue
                if m not in seen:
                    seen.add(m)
                    q.append(m)
        comps.append(comp)
    return comps


def focus(g, root, hops):
    dist = {root: 0}
    q = deque([root])
    while q:
        x = q.popleft()
        for m, t, o in g.adj[x]:
            cost = 0 if t == "comment" else 1
            d = dist[x] + cost
            if d > hops or (m in dist and dist[m] <= d):
                continue
            dist[m] = d
            if g.nodes[m].kind == "person" and m != root:
                continue
            (q.appendleft if cost == 0 else q.append)(m)
    return set(dist)


def subgraph(g, ids):
    h = Graph(g.primary)
    h.fetched_at, h.show_linked, h.ctx, h.why = g.fetched_at, g.show_linked, g.ctx, g.why
    h.nodes = {i: g.nodes[i] for i in ids}
    h.edges = {e for e in g.edges if e[0] in ids and e[1] in ids}
    h.finalize()
    return h


ROOT_RE = re.compile(r"^(?:@[A-Za-z0-9][A-Za-z0-9-]*|(?:(?P<repo>(?:[\w.-]+/)+[\w.-]+))?#?(?P<num>\d+))$")


def resolve_root(g, root):
    root = root.strip()
    if root.startswith("@"):
        for k in g.nodes:
            if k.lower() == root.lower():
                return k
        raise ValueError(f"person {root} is not mentioned anywhere in the loaded graph")
    m = ROOT_RE.match(root)
    if m and not m.group("num"):
        m = None
    if not m:
        raise ValueError(f"cannot parse root {root!r}: use 777, #777, owner/repo#777 or @login")
    nid = g.item_id(qualify(m.group("repo"), repo_host(g.primary)) if m.group("repo") else g.primary, int(m.group("num")))
    if nid not in g.nodes:
        raise ValueError(f"{nid} is not in the loaded graph (state filter / other repo?)")
    return nid


# --------------------------------------------------------------------------
# PR review: local worktree, diff model, findings cache
# --------------------------------------------------------------------------
# Reviewing a PR needs the code, not just the patch: the analysis has to be able to open the whole
# function a hunk sits in. So gg pulls refs/pull/N/head into a detached worktree of a checkout it
# already found (docs/PLAN-review-mode.md) and lets git produce the diff — no API truncation, and the
# context width is ours to choose.
WORKTREE_KEEP_DAYS = int(cfg("worktree_keep_days") or 7)
WORKTREE_MAX = int(cfg("worktree_max") or 5)

SEVERITIES = ["reach", "bug", "regress", "logic", "style", "design"]
SUBJECTIVE = ("style", "design")
VERDICTS = ["CONFIRMED", "PLAUSIBLE", "FALSE"]


class Hunk:
    __slots__ = ("old_start", "old_lines", "new_start", "new_lines", "heading", "lines",
                 "_old_left", "_new_left", "_old_no", "_new_no")

    def __init__(self, old_start, old_lines, new_start, new_lines, heading=""):
        self.old_start, self.old_lines = old_start, old_lines
        self.new_start, self.new_lines = new_start, new_lines
        self.heading = heading
        self.lines = []                 # [(tag, old_no, new_no, text)]  tag: " " | "+" | "-"
        self._old_left, self._new_left = old_lines, new_lines
        self._old_no, self._new_no = old_start, new_start

    @property
    def header(self):
        o = f"-{self.old_start}" + (f",{self.old_lines}" if self.old_lines != 1 else "")
        n = f"+{self.new_start}" + (f",{self.new_lines}" if self.new_lines != 1 else "")
        return f"@@ {o} {n} @@" + (f" {self.heading}" if self.heading else "")

    def done(self):
        return self._old_left <= 0 and self._new_left <= 0

    def touched(self, side):
        """Line numbers this hunk changed on one side — the only lines GitHub accepts a comment on."""
        want = "+" if side == "RIGHT" else "-"
        idx = 2 if side == "RIGHT" else 1
        return {ln[idx] for ln in self.lines if ln[0] == want and ln[idx] is not None}


class DiffFile:
    __slots__ = ("path", "old_path", "status", "additions", "deletions", "hunks", "binary", "no_newline")

    def __init__(self):
        self.path, self.old_path, self.status = None, None, "modified"
        self.additions, self.deletions = 0, 0
        self.hunks, self.binary, self.no_newline = [], False, False

    def touched(self, side):
        out = set()
        for h in self.hunks:
            out |= h.touched(side)
        return out

    def nearest(self, side, line):
        """The changed line closest to `line` on `side`, or None when the file has none."""
        cand = self.touched(side)
        return min(cand, key=lambda n: (abs(n - line), n)) if cand else None


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: ?(.*))?$")
_GIT_AB_RE = re.compile(r"^a/(.+) b/\1$")


def parse_unified_diff(text):
    """`git diff` output -> [DiffFile].

    Paths are taken from the ---/+++ (or rename) lines, never from `diff --git`, whose `a/X b/Y` split
    is ambiguous when a path contains a space. Header lines are only read outside a hunk, and a hunk
    ends when its declared line counts run out — that is what keeps a context line reading `--- foo`
    from being mistaken for a file header.
    """
    files, f, h = [], None, None

    def flush():
        nonlocal f, h
        if f is not None:
            if f.path is None:
                f.path = f.old_path
            if f.old_path is None:
                f.old_path = f.path
            files.append(f)
        f, h = None, None

    for raw in (text or "").splitlines():
        line = raw[:-1] if raw.endswith("\r") else raw
        if h is not None and not h.done():
            tag, body = (" ", "") if line == "" else (line[0], line[1:])
            if tag == "\\":                       # "\ No newline at end of file": not a content line
                f.no_newline = True
                continue
            if tag == " ":
                h.lines.append((" ", h._old_no, h._new_no, body))
                h._old_no += 1
                h._new_no += 1
                h._old_left -= 1
                h._new_left -= 1
                continue
            if tag == "-":
                h.lines.append(("-", h._old_no, None, body))
                h._old_no += 1
                h._old_left -= 1
                f.deletions += 1
                continue
            if tag == "+":
                h.lines.append(("+", None, h._new_no, body))
                h._new_no += 1
                h._new_left -= 1
                f.additions += 1
                continue
            h = None                              # malformed hunk: fall through and re-read as a header
        if line.startswith("diff --git "):
            flush()
            f = DiffFile()
            m = _GIT_AB_RE.match(line[len("diff --git "):])
            if m:
                f.path = f.old_path = m.group(1)
            continue
        if f is None:
            continue
        if line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if m:
                h = Hunk(int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1),
                         (m.group(5) or "").strip())
                f.hunks.append(h)
                if h.done():                      # an empty hunk cannot consume lines
                    h = None
            continue
        if line.startswith("new file mode "):
            f.status = "added"
        elif line.startswith("deleted file mode "):
            f.status = "deleted"
        elif line.startswith("rename from "):
            f.status, f.old_path = "renamed", line[len("rename from "):]
        elif line.startswith("rename to "):
            f.status, f.path = "renamed", line[len("rename to "):]
        elif line.startswith("copy from "):
            f.status, f.old_path = "copied", line[len("copy from "):]
        elif line.startswith("copy to "):
            f.status, f.path = "copied", line[len("copy to "):]
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            f.binary = True
        elif line.startswith("--- "):
            p = line[4:]
            if p != "/dev/null":
                f.old_path = p[2:] if p.startswith("a/") else p
            else:
                f.status = "added"
        elif line.startswith("+++ "):
            p = line[4:]
            if p != "/dev/null":
                f.path = p[2:] if p.startswith("b/") else p
            else:
                f.status = "deleted"
    flush()
    return files


class Change:
    __slots__ = ("cid", "kind", "path", "symbol", "summary")

    def __init__(self, cid="", kind="", path="", symbol="", summary=""):
        self.cid, self.kind, self.path, self.symbol, self.summary = cid, kind, path, symbol, summary

    def to_json(self):
        return {"cid": self.cid, "kind": self.kind, "path": self.path, "symbol": self.symbol,
                "summary": self.summary}

    @classmethod
    def from_json(cls, d):
        return cls(**{k: d.get(k, "") for k in ("cid", "kind", "path", "symbol", "summary")})


class Finding:
    FIELDS = ("fid", "cid", "severity", "path", "line", "end_line", "side", "title", "body",
              "evidence", "diff", "verdict", "verdict_reason", "anchor", "state", "posted_at",
              "thread_url", "digest", "error")
    __slots__ = FIELDS

    def __init__(self, **kw):
        for k in self.FIELDS:
            setattr(self, k, kw.get(k))
        self.severity = self.severity or "logic"
        self.side = self.side or "RIGHT"
        self.anchor = self.anchor or "ok"
        self.state = self.state or "new"
        self.title = self.title or ""
        self.body = self.body or ""
        if not self.digest:
            self.digest = finding_digest(self.path or "", self.title)
        if not self.fid:
            self.fid = self.digest

    @property
    def subjective(self):
        return self.severity in SUBJECTIVE

    @property
    def postable(self):
        return self.anchor in ("ok", "moved") and self.verdict != "FALSE" and self.state == "new"

    def to_json(self):
        return {k: getattr(self, k) for k in self.FIELDS if getattr(self, k) is not None}

    @classmethod
    def from_json(cls, d):
        return cls(**{k: d.get(k) for k in cls.FIELDS})


def finding_digest(path, title):
    """Same defect, same key — so a re-review after new commits does not re-offer what was already
    posted, ignored or disproved."""
    norm = re.sub(r"\s+", " ", (title or "").strip().lower())
    return hashlib.sha1(f"{path}\n{norm}".encode()).hexdigest()[:12]


class Review:
    def __init__(self, repo, number, **kw):
        self.repo, self.number = repo, number
        self.pr_id = kw.get("pr_id")
        self.title = kw.get("title", "")
        self.body = kw.get("body", "")
        self.state = kw.get("state")
        self.draft = kw.get("draft", False)
        self.author = kw.get("author")
        self.url = kw.get("url")
        self.head_ref = kw.get("head_ref")
        self.base_ref = kw.get("base_ref")
        self.head_oid = kw.get("head_oid")
        self.base_oid = kw.get("base_oid")
        self.merge_base = kw.get("merge_base")
        self.clone = kw.get("clone")
        self.worktree = kw.get("worktree")
        self.wt_size = kw.get("wt_size", 0)
        self.files = kw.get("files") or []
        self.changes = kw.get("changes") or []
        self.findings = kw.get("findings") or []
        self.threads = kw.get("threads") or []
        self.reachability = kw.get("reachability")
        self.engine = kw.get("engine")
        self.model = kw.get("model")
        self.verify = kw.get("verify", False)
        self.status = kw.get("status", "idle")
        self.error = kw.get("error")
        self.created = kw.get("created")
        self.t0 = kw.get("t0")
        self.t1 = kw.get("t1")

    @property
    def id(self):
        return f"{self.repo}#{self.number}"

    def state_label(self):
        if self.state == "OPEN":
            return "draft" if self.draft else "open"
        return (self.state or "?").lower()

    def file(self, path):
        return next((f for f in self.files if f.path == path), None)

    def by_file(self, path):
        return [f for f in self.findings if f.path == path]

    def counts(self):
        c = {"open": 0, "confirmed": 0, "plausible": 0, "posted": 0, "ignored": 0, "dropped": 0}
        for f in self.findings:
            if f.verdict == "FALSE":
                c["dropped"] += 1
            elif f.state == "posted":
                c["posted"] += 1
            elif f.state == "ignored":
                c["ignored"] += 1
            else:
                c["open"] += 1
                if f.verdict == "CONFIRMED":
                    c["confirmed"] += 1
                elif f.verdict == "PLAUSIBLE":
                    c["plausible"] += 1
        return c

    def stats(self):
        return sum(f.additions for f in self.files), sum(f.deletions for f in self.files)

    def to_json(self):
        return {"base_oid": self.base_oid, "merge_base": self.merge_base, "head_ref": self.head_ref,
                "base_ref": self.base_ref, "title": self.title, "engine": self.engine,
                "model": self.model, "verify": self.verify, "created": self.created,
                "reachability": self.reachability,
                "files": [{"path": f.path, "status": f.status, "additions": f.additions,
                           "deletions": f.deletions} for f in self.files],
                "changes": [c.to_json() for c in self.changes],
                "findings": [f.to_json() for f in self.findings]}


# ---------------------------------------------------------------- checkouts
CHECKOUTS = {}          # repo -> a local clone of it (filled by discovery; see find_checkout)
_scanned_for_checkouts = False


def find_checkout(repo):
    """A local clone of `repo`. Discovery already walks the tree, so this only rescans when a repo was
    named on the command line and never discovered."""
    global _scanned_for_checkouts
    d = CHECKOUTS.get(repo)
    if d and os.path.isdir(d):
        return d
    if not _scanned_for_checkouts:
        _scanned_for_checkouts = True
        try:
            for r, path in discover_repos(os.getcwd()):
                CHECKOUTS.setdefault(r, path)
                parent = _parent_cache.get(r)
                if parent:
                    CHECKOUTS.setdefault(parent, path)
        except OSError:
            pass
    d = CHECKOUTS.get(repo)
    return d if d and os.path.isdir(d) else None


def git_out(clone, *args, check=True, timeout=600):
    r = subprocess.run(["git", "-C", clone] + list(args), capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        err = (r.stderr or r.stdout or "").strip().splitlines()
        raise ValueError(f"git {' '.join(args[:3])}: {(err[-1] if err else 'exit ' + str(r.returncode))[:200]}")
    return r.stdout


def clone_remote_for(clone, repo):
    """The remote of `clone` that points at `repo`, else an https URL for it (git's credential helper —
    what `gh auth setup-git` installs — supplies the token). The clone is often a fork, so the remote
    we want is not necessarily origin."""
    for name, url in remote_urls(clone):
        m = _REMOTE_RE.match(url)
        if m and make_repo(resolve_ssh_alias(m.group("host").lower()), m.group("owner"),
                           m.group("name")) == repo:
            return name
    host, owner, name = split_repo(repo)
    return f"https://{host}/{owner}/{name}.git"


# ---------------------------------------------------------------- worktrees
def worktrees_dir():
    return os.path.join(CACHE_DIR, "worktrees")


def worktree_path(repo, number):
    return os.path.join(worktrees_dir(), repo.replace("/", "__"), f"pr-{number}")


def pr_ref(repo, number, base=False):
    """Where gg parks a fetched PR head. Namespaced by repo, because a fork's clone is also where the
    parent's PRs are fetched from, and both can have a #7."""
    return f"refs/gg/{repo.replace('/', '__')}/pr-{number}" + ("-base" if base else "")


def dir_size(path):
    total = 0
    for dirpath, _, names in os.walk(path):
        for n in names:
            try:
                total += os.lstat(os.path.join(dirpath, n)).st_size
            except OSError:
                pass
    return total


def drop_pr_refs(clone, repo, number):
    """Remove the refs/gg/… refs a review left in the user's clone. Kept out of drop_worktree(), which
    also runs when a head moved and the ref is about to be checked out again."""
    if not (clone and os.path.isdir(clone)):
        return
    for ref in (pr_ref(repo, number), pr_ref(repo, number, base=True)):
        subprocess.run(["git", "-C", clone, "update-ref", "-d", ref], capture_output=True, text=True)


def drop_worktree(wt, clone=None):
    """Remove a review worktree properly: `git worktree remove` first, so the clone's .git/worktrees
    metadata goes with it — an rm -rf alone leaves a stale entry behind."""
    import shutil
    if clone and os.path.isdir(clone):
        subprocess.run(["git", "-C", clone, "worktree", "remove", "--force", wt],
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", clone, "worktree", "prune"], capture_output=True, text=True)
    if os.path.isdir(wt):
        shutil.rmtree(wt, ignore_errors=True)


def review_worktree(repo, number, base_ref, head_oid=None):
    """(clone, worktree, merge_base) for PR `number` of `repo`. Fetches refs/pull/N/head and the base
    branch into refs/gg/ and checks the head out detached under the cache."""
    clone = find_checkout(repo)
    if not clone:
        home = os.path.expanduser("~")
        raise ValueError(f"no local clone of {repo} found under {os.getcwd().replace(home, '~')} "
                         f"(this directory and 2 levels below)\n"
                         f"  -> run gg from inside the checkout, or clone it there")
    remote = clone_remote_for(clone, repo)
    head_ref, base_local = pr_ref(repo, number), pr_ref(repo, number, base=True)
    progress("review", 0, None, f"fetching #{number} from {remote}")
    git_out(clone, "fetch", "--no-tags", remote,
            f"+refs/pull/{number}/head:{head_ref}", f"+refs/heads/{base_ref}:{base_local}")
    merge_base = git_out(clone, "merge-base", base_local, head_ref).strip()
    oid = git_out(clone, "rev-parse", head_ref).strip()
    wt = worktree_path(repo, number)
    have = git_out(wt, "rev-parse", "HEAD", check=False).strip() if os.path.exists(os.path.join(wt, ".git")) else ""
    if have != oid:
        drop_worktree(wt, clone)
        os.makedirs(os.path.dirname(wt), exist_ok=True)
        progress("review", 0, None, f"checking out {oid[:7]}")
        git_out(clone, "worktree", "add", "--detach", wt, head_ref)
        for d in (worktrees_dir(), os.path.dirname(wt), wt):
            try:
                os.chmod(d, 0o700)      # a checkout of a private repo, like the rest of the cache
            except OSError:
                pass
    prune_worktrees(keep=wt)
    return clone, wt, merge_base


def worktree_entries():
    """[(repo, number, path, mtime)] for every review worktree in the cache."""
    out, root = [], worktrees_dir()
    if not os.path.isdir(root):
        return out
    for slug in sorted(os.listdir(root)):
        d = os.path.join(root, slug)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if not name.startswith("pr-") or not os.path.isdir(p):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append((slug.replace("__", "/"), name[3:], p, max(st.st_mtime, st.st_atime)))
    return out


def prune_worktrees(keep=None):
    """Drop review worktrees unused for WORKTREE_KEEP_DAYS, then the oldest beyond WORKTREE_MAX.
    A kernel checkout is well over a gigabyte, so this is not a formality."""
    now, entries = time.time(), worktree_entries()
    doomed = [e for e in entries if e[2] != keep and now - e[3] > WORKTREE_KEEP_DAYS * 86400]
    rest = [e for e in entries if e not in doomed]
    rest.sort(key=lambda e: -e[3])
    doomed += [e for e in rest[WORKTREE_MAX:] if e[2] != keep]
    for repo, number, path, _ in doomed:
        # CHECKOUTS.get, not find_checkout: hygiene must not walk the filesystem, and must not run
        # `git worktree` inside a clone it merely guessed at. A leftover .git/worktrees entry is
        # harmless — the next `git worktree prune` in that clone clears it.
        drop_worktree(path, CHECKOUTS.get(repo))
        drop_pr_refs(CHECKOUTS.get(repo), repo, number)
        log(f"review: removed worktree {repo}#{number}")
    return len(doomed)


# ---------------------------------------------------------------- PR metadata
PR_META_Q = """
query($owner:String!,$name:String!,$number:Int!){
 repository(owner:$owner,name:$name){ pullRequest(number:$number){
  id number title state isDraft url body headRefName baseRefName headRefOid baseRefOid
  author{login} headRepository{ nameWithOwner }
  reviewThreads(first:100){ nodes{ id isResolved isOutdated path line diffSide
   comments(first:10){ nodes{ author{login} body createdAt url } } } } } } }
"""


def pr_meta(repo, number):
    """head/base oids and the review threads already on the PR. The diff itself comes from git."""
    host, owner, name = split_repo(repo)
    d = graphql(PR_META_Q, {"owner": owner, "name": name, "number": int(number)}, host)
    pr = ((d or {}).get("repository") or {}).get("pullRequest")
    if not pr:
        raise ValueError(f"{repo}#{number} is not a pull request (or is not visible)")
    threads = []
    for t in ((pr.get("reviewThreads") or {}).get("nodes") or []):
        cs = ((t.get("comments") or {}).get("nodes") or [])
        threads.append({"id": t.get("id"), "path": t.get("path"), "line": t.get("line"),
                        "side": t.get("diffSide"), "resolved": bool(t.get("isResolved")),
                        "outdated": bool(t.get("isOutdated")),
                        "comments": [{"author": ((c.get("author") or {}).get("login") or "?"),
                                      "body": c.get("body") or "", "created": c.get("createdAt"),
                                      "url": c.get("url")} for c in cs]})
    return {"pr_id": pr.get("id"), "title": pr.get("title") or "", "body": pr.get("body") or "",
            "state": pr.get("state"),
            "draft": bool(pr.get("isDraft")), "url": pr.get("url"),
            "author": ((pr.get("author") or {}).get("login") or "?"),
            "head_ref": pr.get("headRefName"), "base_ref": pr.get("baseRefName"),
            "head_oid": pr.get("headRefOid"), "base_oid": pr.get("baseRefOid"), "threads": threads}


DIFF_CONTEXT = 5


def review_diff(clone, merge_base, ref, paths=None):
    args = ["-c", "core.quotePath=false", "diff", "--no-color", f"-U{DIFF_CONTEXT}",
            "--find-renames", "--no-ext-diff", f"{merge_base}", ref]
    if paths:
        args += ["--"] + list(paths)
    return git_out(clone, *args)


# ---------------------------------------------------------------- findings cache
def reviews_path(repo):
    return _cache_path("reviews", repo)


def load_reviews(repo):
    d = read_json(reviews_path(repo))
    return d if isinstance(d, dict) else {}


def review_history(repo, number):
    """What is known about this PR across head SHAs: which digests were posted, ignored, disproved."""
    e = load_reviews(repo).get(str(number)) or {}
    return ({k: v for k, v in (e.get("posted") or {}).items()},
            {k: v for k, v in (e.get("ignored") or {}).items()},
            {k: v for k, v in (e.get("dropped") or {}).items()})


def save_review(rv):
    """Store this head's findings and fold the per-digest history forward, so a re-review after new
    commits neither re-posts nor rediscovers what is already settled."""
    d = load_reviews(rv.repo)
    e = d.setdefault(str(rv.number), {})
    e.setdefault("reviews", {})
    e.setdefault("posted", {})
    e.setdefault("ignored", {})
    e.setdefault("dropped", {})
    if rv.head_oid:
        e["reviews"] = {rv.head_oid: rv.to_json()}     # only the current head's detail is worth keeping
    for f in rv.findings:
        if f.state == "posted":
            e["posted"][f.digest] = {"head_oid": rv.head_oid, "at": f.posted_at or time.time(),
                                     "thread_url": f.thread_url}
        elif f.state == "ignored":
            e["ignored"][f.digest] = {"at": time.time()}
        elif f.verdict == "FALSE":
            e["dropped"][f.digest] = {"at": time.time(), "reason": f.verdict_reason or ""}
    write_json(reviews_path(rv.repo), d)
    return reviews_path(rv.repo)


def cached_review(repo, number, head_oid):
    e = (load_reviews(repo).get(str(number)) or {}).get("reviews") or {}
    return e.get(head_oid)


def apply_history(rv):
    """Carry posted / ignored / disproved verdicts of earlier head SHAs onto this run's findings."""
    posted, ignored, dropped = review_history(rv.repo, rv.number)
    for f in rv.findings:
        if f.digest in posted:
            f.state, f.posted_at = "posted", posted[f.digest].get("at")
            f.thread_url = posted[f.digest].get("thread_url")
        elif f.digest in ignored:
            f.state = "ignored"
        elif f.digest in dropped and not f.verdict:
            f.verdict, f.verdict_reason = "FALSE", dropped[f.digest].get("reason") or "disproved earlier"
    return rv


def anchor_findings(rv):
    """GitHub refuses an inline comment on a line the diff does not touch, so pin every finding to a
    real changed line before it can ever be posted."""
    for f in rv.findings:
        df = rv.file(f.path)
        if df is None:
            f.anchor = "unanchored"
            continue
        try:
            line = int(f.line)
        except (TypeError, ValueError):
            line = None
        if line is not None and line in df.touched(f.side):
            # "moved" is sticky: anchoring runs again after the verification pass, and a finding gg
            # once had to pull onto another line should not quietly read as if it never moved.
            f.anchor, f.line = ("moved" if f.anchor == "moved" else "ok"), line
        else:
            near = df.nearest(f.side, line if line is not None else 1)
            if near is None:
                f.anchor = "unanchored"
            else:
                f.line, f.anchor = near, "moved"
        if f.end_line is not None and (f.anchor != "ok" or f.line is None or f.end_line < f.line):
            f.end_line = None
    return rv


def review_load(repo, number, refresh=False, meta=None):
    """PR metadata + worktree + parsed diff. No AI: this is the half that works without an AI CLI."""
    meta = meta or pr_meta(repo, number)
    rv = Review(repo, int(number), **{k: v for k, v in meta.items() if k != "threads"})
    rv.threads = meta["threads"]
    try:
        rv.clone, rv.worktree, rv.merge_base = review_worktree(repo, number, rv.base_ref, rv.head_oid)
        rv.wt_size = dir_size(rv.worktree)  # a filesystem walk: once per load, never per redraw
        progress("review", 0, None, "diffing")
        rv.files = parse_unified_diff(review_diff(rv.clone, rv.merge_base, pr_ref(repo, number)))
        rv.status = "done"
    except (ValueError, OSError, subprocess.SubprocessError) as e:
        rv.status, rv.error = "failed", str(e)
        return rv                           # the metadata and review threads are still worth showing
    cached = None if refresh else cached_review(repo, number, rv.head_oid)
    if cached:
        rv.changes = [Change.from_json(c) for c in cached.get("changes") or []]
        rv.findings = [Finding.from_json(f) for f in cached.get("findings") or []]
        rv.reachability = cached.get("reachability")
        rv.engine, rv.model = cached.get("engine"), cached.get("model")
        rv.verify, rv.created = cached.get("verify", False), cached.get("created")
        anchor_findings(rv)
    apply_history(rv)
    return rv


def standards_files(worktree):
    """Project convention files a review should read first (sashiko's GEMINI.md step, generalised)."""
    return [n for n in ("CLAUDE.md", "AGENTS.md", "GEMINI.md", "CONTRIBUTING.md", "CODING_STYLE.md")
            if os.path.isfile(os.path.join(worktree or "", n))]



# ---------------------------------------------------------------- the review pass
# The protocol below is distilled from the kernel review prompts in ~/.claude/review-prompts (9,000
# lines of them) — not their kernel knowledge, which belongs in a `review_cmd` skill, but their
# discipline: read the whole function before judging a hunk, split the change into categories first,
# check the changed code is reachable at all, and refuse to report anything you cannot point at.
REVIEW_MODEL = cfg("review_model")
REVIEW_TIMEOUT = int(cfg("review_timeout") or 900)
REVIEW_MAX_BYTES = int(cfg("review_max_bytes") or 400000)
REVIEW_TOOLS = ("Read", "Grep", "Glob", "Bash(git *)")

REVIEW_PROMPT = """You are reviewing one pull request. You are standing in a git worktree with its head
checked out, so you can open any file in the tree — not only the lines in the diff.

{pr}

Work these steps in order. The first three exist to stop you judging a hunk before you understand it.

STEP 1 — context.
- Read the pull request description above and decide what it is trying to do.
- Read the diff line by line, all of it, before looking anything up.
- For every function the diff touches, open the whole function in the tree. Never reason about a
  fragment: a hunk that looks wrong is usually a hunk whose surroundings you have not read.
- Follow what matters: callers of a changed function, what it calls, the error and cleanup paths, and
  the project conventions in {standards}.

STEP 2 — split the change up.
Break the diff into small categories and name them CHANGE-1, CHANGE-2, … Make a separate category for
each of: one loop, one changed return value or condition, one allocation/free pair, one object
initialisation, one lock, one API or signature change, one data-format change, build files, docs.
A file is not a category; "refactoring" is not a category.

STEP 3 — reachability gate.
Can the changed code run at all for the use the description claims? Check the flags, config, callers
and protocol constraints that would keep it from executing. If it cannot run, say so — that outranks
every other finding, and its severity is "reach".

STEP 4 — look for defects, one category at a time.
Assume the author is wrong and demand proof they are right. Comments, commit text and variable names
are claims, not evidence; verify each against the code. Weigh at least:
- control flow: a path that now returns, breaks or continues where it did not, and what the caller
  does with it
- resources: an allocation with no matching free on some path, a free with a later use, an object used
  before it is fully initialised
- locking: a path that leaves a lock held or takes it twice; before reporting one, read the callers
  and say which of them already holds it
- boundaries: off-by-one, truncation, signedness, overflow, an empty or maximal input
- concurrency and ordering: a value read outside the lock that protects it, a wait with no wake
- compatibility: a changed on-disk or on-wire format, a changed API with callers you have not updated
- tests: new logic with no test, when the project's own convention is to add one

STEP 5 — evidence.
Every finding needs a concrete path stated as file:line — where the value comes from, and where it
goes wrong. If you cannot write that path down, drop the finding. A finding with no `evidence` field
is worthless and will be thrown away.

STEP 6 — remarks, at most three.
Only after the defects: things that are not wrong but read badly — a comment that restates the code,
a symbol nothing uses, duplication of an existing helper, a name that fights the file's own
convention. Judge against the neighbouring code, not an ideal. If a competent author had a plausible
reason to write it that way, stay silent. severity "style" or "design".

Do not report:
- a defensive check for input you cannot show reaching the code
- theoretical misuse of an API with no caller that does it
- anything outside the lines this pull request changed
- praise, summaries, or "consider whether…" with no concrete claim
- something already raised in the review threads listed above

Write `title` and `body` in English: they may be posted to the pull request verbatim.
"""

REVIEW_CONTRACT = """
When you are done, print nothing after these markers but the object between them. Use exactly these
fields and invent no others.

<<<GG_REVIEW
{"reachability": {"verdict": "confirmed|blocked", "reason": "one sentence"},
 "changes": [{"cid": "CHANGE-1", "kind": "control-flow|return|resource|init|locking|api|data|build|doc",
              "path": "path/from/the/diff.c", "symbol": "function_name", "summary": "one line"}],
 "findings": [{"cid": "CHANGE-1",
               "severity": "reach|bug|regress|logic|style|design",
               "path": "path/from/the/diff.c", "line": 222, "end_line": 222, "side": "RIGHT",
               "title": "one line, at most 70 characters",
               "body": "what is wrong and what to do instead, 1-4 sentences",
               "evidence": "the concrete path, as file:line -> file:line",
               "diff": "optional unified diff that applies to path"}]}
GG_REVIEW>>>

- `line` is a line number in the new file when `side` is "RIGHT", in the old file when it is "LEFT",
  and it must be a line this diff actually touches. Comments cannot be attached anywhere else.
- Never name a `path` that is not in the diff.
- `findings` may be empty. An empty list is a fine answer; a padded one is not.
"""


def _json_block(text, marker):
    """The object between <<<MARKER and MARKER>>>, else the last {...} in the reply, else None."""
    m = re.search(rf"<<<{marker}\s*(.+?)\s*{marker}>>>", text or "", re.S)
    blobs = [m.group(1)] if m else []
    if not blobs:
        starts = [i for i, ch in enumerate(text or "") if ch == "{"]
        blobs = [text[i:] for i in reversed(starts[:200])]
    for blob in blobs:
        blob = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", blob.strip())
        try:
            d = json.loads(blob)
        except json.JSONDecodeError:
            try:                                   # trailing prose after the object
                d, _ = json.JSONDecoder().raw_decode(blob)
            except ValueError:
                continue
        if isinstance(d, dict):
            return d
    return None


def parse_review_reply(text):
    """The reply of one review call -> (reachability, [Change], [Finding]). Raises when unusable."""
    d = _json_block(text, "GG_REVIEW")
    if d is None:
        raise ValueError("the review did not end with a GG_REVIEW block")
    changes = [Change.from_json(c) for c in (d.get("changes") or []) if isinstance(c, dict)]
    findings = []
    for raw in (d.get("findings") or []):
        if not isinstance(raw, dict) or not (raw.get("title") or "").strip():
            continue
        sev = raw.get("severity")
        findings.append(Finding(
            cid=raw.get("cid"), severity=sev if sev in SEVERITIES else "logic",
            path=raw.get("path"), line=raw.get("line"), end_line=raw.get("end_line"),
            side="LEFT" if str(raw.get("side", "")).upper() == "LEFT" else "RIGHT",
            title=str(raw.get("title", "")).strip(), body=str(raw.get("body", "")).strip(),
            evidence=str(raw.get("evidence") or "").strip() or None,
            diff=raw.get("diff") or None))
    reach = d.get("reachability") if isinstance(d.get("reachability"), dict) else None
    return reach, changes, findings


def diff_text(rv, paths=None):
    """The diff as the review sees it, rebuilt from the parsed files so both halves agree."""
    out = []
    for f in rv.files:
        if paths is not None and f.path not in paths:
            continue
        head = f"diff --git a/{f.old_path} b/{f.path}"
        if f.status != "modified":
            head += f"   ({f.status})"
        out.append(head)
        if f.binary:
            out.append("(binary)")
            continue
        for h in f.hunks:
            out.append(h.header)
            for tag, _, _, text in h.lines:
                out.append(tag + text)
    return "\n".join(out)


def review_chunks(rv):
    """[[path, …]] — one chunk when the diff is small, else file groups under REVIEW_MAX_BYTES."""
    sizes = [(f.path, len(diff_text(rv, {f.path}))) for f in rv.files]
    total = sum(s for _, s in sizes)
    if total <= REVIEW_MAX_BYTES or len(sizes) < 2:
        return [[f.path for f in rv.files]]
    chunks, cur, cur_size = [], [], 0
    for path, size in sizes:
        if cur and cur_size + size > REVIEW_MAX_BYTES:
            chunks.append(cur)
            cur, cur_size = [], 0
        cur.append(path)
        cur_size += size
    if cur:
        chunks.append(cur)
    return chunks


def pr_header(rv, paths, body):
    threads = "\n".join(f"  {t.get('path')}:{t.get('line')} @{(t.get('comments') or [{}])[0].get('author', '?')}: "
                        f"{trunc((t.get('comments') or [{}])[0].get('body', ''), 160)}"
                        for t in rv.threads if not t.get("resolved"))
    parts = [f"Pull request {rv.repo}#{rv.number} by @{rv.author}: {rv.title}",
             f"branch {rv.head_ref} onto {rv.base_ref}, head {(rv.head_oid or '')[:12]}"]
    if len(paths) < len(rv.files):
        parts.append(f"You are reviewing {len(paths)} of its {len(rv.files)} files; the rest is being "
                     f"reviewed separately. Report only on the files below.")
    if body:
        parts += ["", "--- description ---", trunc_lines(body, 120)]
    if threads:
        parts += ["", "--- review threads already on this pull request (do not repeat them) ---", threads]
    parts += ["", f"--- diff ({', '.join(paths)}) ---", diff_text(rv, set(paths))]
    return "\n".join(parts)


def trunc_lines(text, n):
    lines = (text or "").splitlines()
    return "\n".join(lines[:n]) + (f"\n… ({len(lines) - n} more lines)" if len(lines) > n else "")


def review_prompt(rv, paths, body=""):
    std = ", ".join(standards_files(rv.worktree)) or "the surrounding code (no conventions file found)"
    return REVIEW_PROMPT.format(pr=pr_header(rv, paths, body), standards=std) + REVIEW_CONTRACT


def run_review(rv, body="", on_step=None, verify=None):
    """Pass 1: categorise, gate on reachability, look for defects; then pass 2 checks each finding in a
    call of its own unless it is turned off. Fills rv in place."""
    if not rv.files:
        rv.status, rv.error = "failed", "nothing to review: the diff is empty"
        return rv
    if not ai_available():
        rv.status, rv.error = "failed", f"{CLAUDE_BIN} is not installed (gg ai picks another)"
        return rv
    chunks = review_chunks(rv)
    rv.status, rv.error, rv.t0 = "running", None, time.time()
    rv.engine, rv.model = f"builtin:{ai_backend()}", REVIEW_MODEL
    changes, findings, reach, errors = [], [], None, []

    def one(paths):
        return claude_call(review_prompt(rv, paths, body), REVIEW_MODEL, "review",
                           timeout=REVIEW_TIMEOUT, cwd=rv.worktree, tools=REVIEW_TOOLS)

    from concurrent.futures import ThreadPoolExecutor
    done = 0
    progress("review", 0, len(chunks), "reviewing")
    with ThreadPoolExecutor(max_workers=max(1, min(AI_PARALLEL, len(chunks)))) as pool:
        for paths, out in zip(chunks, pool.map(_safe, ((one, c) for c in chunks))):
            done += 1
            progress("review", done, len(chunks), "reviewing")
            if on_step:
                on_step(done, len(chunks))
            if isinstance(out, Exception):
                errors.append(str(out))
                continue
            try:
                r, ch, fi = parse_review_reply(out)
            except ValueError as e:
                errors.append(f"{', '.join(paths)}: {e}")
                continue
            reach = reach or r
            changes += ch
            findings += fi
    if errors and not findings:
        rv.status, rv.error = "failed", "; ".join(errors)[:500]
        return rv
    for i, c in enumerate(changes, 1):          # one numbering across chunks
        c.cid = f"CHANGE-{i}"
    rv.reachability, rv.changes = reach, changes
    rv.findings = dedupe_findings(findings)
    rv.status = "done"
    rv.error = ("; ".join(errors)[:300]) if errors else None
    anchor_findings(rv)
    apply_history(rv)
    save_review(rv)
    if REVIEW_VERIFY if verify is None else verify:
        run_verify(rv, on_step=on_step)
    else:
        cap_subjective(rv)
        save_review(rv)
    rv.t1 = time.time()
    return rv


def _safe(job):
    fn, arg = job
    try:
        return fn(arg)
    except Exception as e:  # noqa: BLE001
        return e


def dedupe_findings(findings):
    """Same digest twice (two chunks noticing one thing): keep the one with evidence."""
    best = {}
    for f in findings:
        cur = best.get(f.digest)
        if cur is None or (not cur.evidence and f.evidence):
            best[f.digest] = f
    return list(best.values())



# ---------------------------------------------------------------- the verification pass
# false-positive-guide.md tells the reviewer to check itself in the same session. Splitting that into
# a call of its own per finding is the point: a fresh context cannot be dragged along by the reasoning
# that produced the claim, and it can be told to argue the author's side first.
REVIEW_VERIFY = cfg("review_verify").lower() not in ("off", "false", "0", "no", "")
VERIFY_MODEL = cfg("review_verify_model")

VERIFY_PROMPT = """You are checking ONE claim another reviewer made about this pull request. Your job is
to disprove it. You are standing in a git worktree with the pull request's head checked out, so read
whatever you need.

{pr}

--- the claim ---
severity: {severity}
where: {path}:{line} ({side})
title: {title}
body: {body}
evidence offered: {evidence}

--- how to check it ---

STEP 1 — is it even there? Open {path} and read the code at and around line {line}. Quote it. If the
code does not say what the claim says it says, the claim is FALSE. Reviewers hallucinate line numbers,
invent function names and get arithmetic wrong; this step catches most of that.

STEP 2 — argue as the author. Build the strongest case that the code is fine as written:
- a defensive check: is there a path where untrusted or invalid data actually reaches this code? If
  the claim only says a check "would be safer", it is FALSE.
- API misuse: is there a real caller that misuses it, or only a hypothetical one?
- locking: read the callers two or three levels up and say which of them already holds the lock. A
  lock taken higher in the chain, or an RCU-style scheme, makes the claim FALSE.
- use after free: separate use-after-free (report) from use-then-free and free-after-use (fine).
  Write the alloc -> use -> free -> use sequence with locations, or say there is none.
- a leak: was ownership handed to something else, was the object put on a list or queue, is there a
  callback or deferred work that frees it? Then FALSE.
- reordering: only a race, a violated dependency, a lock-order inversion or an invalid state makes
  reordering a defect. Otherwise FALSE.
- a race: name the structure, name the lock that should protect it, and show two paths that can run
  at the same time. If you cannot, it is at best PLAUSIBLE.
- uninitialised: writing to a variable initialises it, and passing it to a function that writes before
  it reads is fine. Only reading an uninitialised value counts. A zeroing allocator initialises every
  field whose zero value is correct.
- performance or design: if the description or a comment shows the author chose this deliberately,
  it is FALSE.
- a style or design remark: is the surrounding code in this same file already like this, or is there
  a plausible engineering reason to write it this way? Then FALSE. Never argue from who or what wrote
  the code.

STEP 3 — answer as the reviewer, with code. Take each of your own arguments from STEP 2 and either
refute it by quoting code or accept it. If you dismiss the claim because a comment or a document says
so, open the implementation it describes and quote that instead: comments get copied between
implementations that do not behave alike, and a defect waved away by a stale comment is worse than a
false positive.

STEP 4 — verdict.
- CONFIRMED: you followed the path in the code and it is wrong. You can name every hop.
- PLAUSIBLE: the code could behave that way but you could not close the path — a caller you cannot
  see, a condition you cannot evaluate.
- FALSE: you refuted it with code, or STEP 1 showed the claim does not match what is there.

"Unlikely in practice" never makes a deadlock, a crash or data corruption FALSE; only "the code cannot
reach that state" does. The other way round, "this could become a problem some day" is not CONFIRMED.
"""

VERIFY_CONTRACT = """
Print nothing after these markers but the object between them. Use exactly these fields.

<<<GG_VERDICT
{"verdict": "CONFIRMED|PLAUSIBLE|FALSE",
 "reason": "one or two sentences naming the code you read",
 "line": 222}
GG_VERDICT>>>

`line` is optional. Give it only when the claim is right about the defect but wrong about where it is,
and it must still be a line this diff changed on the same side.
"""


def verify_prompt(rv, f):
    return VERIFY_PROMPT.format(
        pr=pr_header(rv, [f.path] if rv.file(f.path) else [x.path for x in rv.files], rv.body),
        severity=f.severity, path=f.path, line=f.line, side=f.side,
        title=f.title, body=f.body, evidence=f.evidence or "(none given)") + VERIFY_CONTRACT


def verify_finding(rv, f):
    """One adversarial call. Sets verdict / verdict_reason on f; leaves it PLAUSIBLE when the check
    itself fails — an unusable answer is not evidence that the finding is wrong."""
    try:
        out = claude_call(verify_prompt(rv, f), VERIFY_MODEL, "verify",
                          timeout=REVIEW_TIMEOUT, cwd=rv.worktree, tools=REVIEW_TOOLS)
    except Exception as e:  # noqa: BLE001
        f.verdict, f.verdict_reason = "PLAUSIBLE", f"could not be checked: {str(e)[:120]}"
        return f
    d = _json_block(out, "GG_VERDICT") or {}
    verdict = str(d.get("verdict", "")).upper()
    if verdict not in VERDICTS:
        f.verdict, f.verdict_reason = "PLAUSIBLE", "the check did not answer in the agreed form"
        return f
    f.verdict = verdict
    f.verdict_reason = str(d.get("reason") or "").strip()[:1000]
    if verdict != "FALSE" and isinstance(d.get("line"), int):
        f.line = d["line"]                      # re-anchored below, so a wrong correction cannot stick
    return f


def run_verify(rv, findings=None, on_step=None):
    """Pass 2 over the open findings, AI_PARALLEL at a time."""
    todo = [f for f in (rv.findings if findings is None else findings)
            if f.state == "new" and f.verdict is None]
    if not todo:
        return rv
    rv.status, rv.verify = "verifying", True
    done = 0
    progress("verify", 0, len(todo), "checking findings")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, min(AI_PARALLEL, len(todo)))) as pool:
        for _ in pool.map(lambda f: verify_finding(rv, f), todo):
            done += 1
            progress("verify", done, len(todo), "checking findings")
            if on_step:
                on_step(done, len(todo))
    anchor_findings(rv)
    cap_subjective(rv)
    rv.status = "done"
    save_review(rv)
    return rv


SUBJECTIVE_CAP = 3


def cap_subjective(rv):
    """slop-indicators.md's hard cap, in code: at most three opinions, the most concrete first.
    Volume is the problem being avoided, so the rest are dropped rather than hidden."""
    subj = [f for f in rv.findings if f.subjective and f.state == "new" and f.verdict != "FALSE"]
    if len(subj) <= SUBJECTIVE_CAP:
        return rv
    subj.sort(key=lambda f: (f.verdict != "CONFIRMED", not f.evidence, f.path or "", f.line or 0))
    for f in subj[SUBJECTIVE_CAP:]:
        f.verdict = "FALSE"
        f.verdict_reason = f"beyond the cap of {SUBJECTIVE_CAP} style remarks per pull request"
    return rv


# ---------------------------------------------------------------- CLI
PR_TARGET_RE = re.compile(r"^(?:(?P<repo>[\w.-]+/[\w.-]+(?:/[\w.-]+)?)?#|#?)(?P<num>\d+)$")


def parse_pr_target(s, repos):
    """'123' / '#123' / 'owner/name#123' / a pull URL -> (repo, number)."""
    s = (s or "").strip()
    m = URL_RE.search(s)
    if m:
        return qualify(m.group("repo"), m.group("host")), int(m.group("num"))
    m = PR_TARGET_RE.match(s)
    if not m:
        raise ValueError(f"cannot parse {s!r}: use 123, #123, owner/name#123 or a pull request URL")
    repo = m.group("repo")
    if repo:
        repo = qualify(repo, repo_host(repos[0]) if repos else DEFAULT_HOST)
    return repo or (repos[0] if repos else ""), int(m.group("num"))


def review_summary_rows(rv):
    """Rows for `gg review --print`: the PR, its files, and whatever findings are cached."""
    add, dele = rv.stats()
    rows = [Row(f"{rv.repo}#{rv.number}  {rv.title}", kind="head"),
            Row(f"{rv.state_label()}  @{rv.author}  {rv.head_oid[:7] if rv.head_oid else '?'}"
                f"  {rv.head_ref} → {rv.base_ref}", kind="head"),
            Row(f"{len(rv.files)} file{'s' if len(rv.files) != 1 else ''}  +{add} -{dele}"
                f"  worktree {fmt_bytes(rv.wt_size) if rv.worktree else '-'}"
                f"  {(rv.worktree or '').replace(os.path.expanduser('~'), '~')}", kind="head"),
            Row("")]
    for f in rv.files:
        mark = {"added": "+", "deleted": "-", "renamed": "→", "copied": "≡"}.get(f.status, " ")
        extra = "  (binary)" if f.binary else ""
        if f.status == "renamed" and f.old_path != f.path:
            extra += f"  ← {f.old_path}"
        rows.append(Row(f" {mark} {f.path}   +{f.additions} -{f.deletions}"
                        f"  {len(f.hunks)} hunk{'s' if len(f.hunks) != 1 else ''}{extra}",
                        f"file:{f.path}"))
    if rv.findings:
        rows.append(Row(""))
        rows.append(Row(f"findings ({', '.join(f'{k} {v}' for k, v in rv.counts().items() if v)})", kind="head"))
        for i, f in enumerate(rv.findings, 1):
            mark = {"CONFIRMED": "✓", "PLAUSIBLE": "?", "FALSE": "·"}.get(f.verdict, " ")
            rows.append(Row(f" {mark} #{i} [{f.severity}] {f.title}   {f.path}:{f.line}",
                            f"finding:{f.fid}"))
    elif rv.threads:
        rows.append(Row(""))
        rows.append(Row(f"{len(rv.threads)} existing review thread(s) on GitHub", kind="head"))
    return rows


def do_review(target, repos, refresh=False, as_json=False, no_ai=False, verify=True, to_tui=False,
              opts=None, color=True):
    """gg review <PR>: the TUI in review mode, or the result on stdout (--print / --json)."""
    repo, number = parse_pr_target(target, repos)
    if to_tui:
        tui({"repos": repos, "state": opts.state, "comments": opts.comments or "all",
             "people": not opts.no_people, "closed_neighbors": not opts.no_closed_neighbors,
             "max_age_min": opts.max_age, "width": opts.width, "translate": opts.translate,
             "layout": opts.layout, "hops": opts.hops, "root": None, "summary": not opts.no_summary,
             "depth": opts.depth, "days": opts.days, "start_tour": False, "tutorial": False,
             "review": (repo, number)})
        return 0
    rv = review_load(repo, number, refresh=refresh)
    if not no_ai and not rv.error and not rv.findings and not rv.engine and ai_available():
        log(f"reviewing {repo}#{number} with {CLAUDE_BIN} {REVIEW_MODEL} "
            f"({len(rv.files)} files, {len(review_chunks(rv))} call(s))…")
        run_review(rv, rv.body, verify=verify)
        if USAGE["calls"]:
            log(usage_line())          # do_review returns before main()'s own usage line
    if as_json:
        d = rv.to_json()
        d.update({"repo": rv.repo, "number": rv.number, "head_oid": rv.head_oid,
                  "worktree": rv.worktree, "url": rv.url})
        print(json.dumps(d, ensure_ascii=False, indent=1))
        return 0
    rows = review_summary_rows(rv)
    print(ansi_rows(rows, Graph(repo)) if color else "\n".join(r.text for r in rows))
    if rv.error:
        print(f"error: {rv.error}", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------
# labels
# --------------------------------------------------------------------------
def trunc(s, n):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s if dw(s) <= n else clip(s, 0, max(n - 1, 0)) + "…"


def raw_excerpt(body):
    body = FENCE_RE.sub(" ", body or "")
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith(">"):
            return trunc(line, 300)
    return ""


def excerpt(body, n):
    return trunc(raw_excerpt(body), n)


def short_date(s):
    return (s or "")[:10]


def rel_days(n, g):
    """'+Nd': days between a comment and the issue/PR it belongs to."""
    p = g.nodes.get(n.parent) if n.parent else None
    if not p or not p.created or not n.created:
        return ""
    return f"+{int(max(n.time - p.time, 0) // 86400)}d"


def kind_tag(n):
    """'[I]' / '[PR]' plus the state when it is not simply open: '[PR][merged]', '[I][closed]', '[PR][draft]'."""
    kind = "PR" if n.is_pr else ("I" if n.is_pr is False else "?")
    st = n.state_label()
    return f"[{kind}]" + (f"[{st}]" if st != "open" else "")


def item_label(g, n, width, with_meta=True):
    num = g.label_num(n)
    title = trunc(n.tr_title or n.title, width) if n.title else "(unresolved)"
    s = (f"{short_date(n.created)} " if n.created else "") + f"{num} {kind_tag(n)} {title}"
    if with_meta and n.author:
        s += f"  @{n.author}"
    if n.inline_mentions:
        s += "  mentions " + " ".join(n.inline_mentions)
    return s


def comment_label(g, n, width, show_item=True):
    tag = ""
    if n.ckind == "review":
        tag = f"[{(n.review_state or 'REVIEW').lower()}] "
    elif n.ckind == "review_comment":
        tag = "[inline] "
    pre = f"{g.label_num(g.nodes[n.parent])} " if show_item and n.parent in g.nodes else ""
    rel = rel_days(n, g)
    rel = f"{rel} {short_date(n.created)[5:]} " if rel else (f"{short_date(n.created)[5:]} " if n.created else "")
    if n.summary:
        s = f"{rel}{pre}o @{n.author} {tag}» {trunc(n.summary, width)}"
    elif n.summary_pending:
        s = f"{rel}{pre}o @{n.author} {tag}» {PENDING_TEXT}"
    else:
        ex = trunc(n.tr_excerpt, width) if n.tr_excerpt else excerpt(n.body, width)
        s = f"{rel}{pre}o @{n.author} {tag}\"{ex}\""
    if n.inline_mentions:
        s += "  mentions " + " ".join(n.inline_mentions)
    return s


def node_label(g, n, width, show_item=True):
    if n.kind == "item":
        return item_label(g, n, width)
    if n.kind == "comment":
        return comment_label(g, n, width, show_item)
    return f"{n.id}  ({n.mention_count} mentions)"


EDGE_LABEL = {
    ("closes", True): "→ closes ", ("closes", False): "← closed-by ",
    ("ref", True): "→ refs ", ("ref", False): "← cited-by ",
    ("mention", True): "→ mentions ", ("mention", False): "← mentioned-by ",
    ("comment", True): "on ", ("comment", False): "",
}
EDGE_WORDS = sorted({v.strip() for v in EDGE_LABEL.values() if v.strip()}, key=len, reverse=True)
EDGE_STYLE = {"→ closes": "closes", "← closed-by": "closedby", "→ refs": "out", "← cited-by": "in",
              "→ mentions": "out", "← mentioned-by": "in", "on": "pre"}


# --------------------------------------------------------------------------
# styling: split a row into (text, style) segments; CLI maps styles to ANSI, TUI to curses attrs
# --------------------------------------------------------------------------
# style -> (fg256, fg8, bold, dim); fg None = default colour.  people: (256-colour list, 8-colour list)
THEMES = {
    "dark": {   # dark background, 256 colours (default)
        "pre": (None, None, False, True), "link": (None, None, False, True), "meta": (None, None, False, True),
        "stub": (None, None, False, True), "pending": (None, None, False, True), "head": (None, None, True, False),
        "out": (4, 4, False, False), "in": (5, 5, False, False), "closes": (4, 4, True, False), "closedby": (5, 5, True, False),
        "issue": (2, 2, True, False), "pr": (12, 4, True, False), "draft": (12, 4, False, False),
        "merged": (5, 5, True, False), "closed": (1, 1, True, False), "comment": (6, 6, False, False),
        "url": (39, 6, True, False), "fold": (3, 3, True, False), "md_h": (214, 3, True, False), "md_code": (250, 7, False, False), "md_quote": (None, None, False, True), "md_bold": (None, None, True, False), "sum": (180, 3, False, False),
        "diff_add": (114, 2, False, False), "diff_del": (167, 1, False, False), "diff_hunk": (39, 6, True, False),
        "diff_ctx": (None, None, False, True),
        "sev_reach": (201, 5, True, False), "sev_bug": (196, 1, True, False), "sev_regress": (208, 3, True, False),
        "sev_logic": (220, 3, False, False), "sev_style": (39, 6, False, False), "sev_design": (78, 2, False, False),
        "people": ([39, 208, 42, 205, 226, 51, 141, 203, 118, 214, 81, 171, 190, 99, 209, 45], [1, 2, 3, 4, 5, 6]),
    },
    "light": {  # light background: darker tones, grey instead of dim
        "pre": (8, 8, False, False), "link": (8, 8, False, False), "meta": (8, 8, False, False),
        "stub": (8, 8, False, False), "pending": (8, 8, False, False), "head": (None, None, True, False),
        "out": (4, 4, False, False), "in": (5, 5, False, False), "closes": (4, 4, True, False), "closedby": (5, 5, True, False),
        "issue": (22, 2, True, False), "pr": (4, 4, True, False), "draft": (4, 4, False, False),
        "merged": (5, 5, True, False), "closed": (1, 1, True, False), "comment": (30, 6, False, False),
        "url": (4, 4, True, False), "fold": (130, 3, True, False), "md_h": (130, 3, True, False), "md_code": (240, 0, False, False), "md_quote": (8, 8, False, False), "md_bold": (None, None, True, False), "sum": (94, 3, False, False),
        "diff_add": (22, 2, False, False), "diff_del": (124, 1, False, False), "diff_hunk": (4, 4, True, False),
        "diff_ctx": (8, 8, False, False),
        "sev_reach": (90, 5, True, False), "sev_bug": (124, 1, True, False), "sev_regress": (130, 3, True, False),
        "sev_logic": (94, 3, False, False), "sev_style": (24, 6, False, False), "sev_design": (22, 2, False, False),
        "people": ([18, 88, 22, 90, 130, 24, 54, 94, 28, 124, 30, 91, 52, 58, 23, 89], [1, 2, 3, 4, 5, 6]),
    },
    "basic": {  # 8 colours only, no dim, no dark blue: PuTTY and other plain terminals
        "pre": (8, 8, False, False), "link": (8, 8, False, False), "meta": (8, 8, False, False),
        "stub": (8, 8, False, False), "pending": (8, 8, False, False), "head": (None, None, True, False),
        "out": (6, 6, False, False), "in": (5, 5, False, False), "closes": (6, 6, True, False), "closedby": (5, 5, True, False),
        "issue": (2, 2, True, False), "pr": (3, 3, True, False), "draft": (3, 3, False, False),
        "merged": (5, 5, True, False), "closed": (1, 1, True, False), "comment": (6, 6, False, False),
        "url": (6, 6, True, False), "fold": (7, 7, True, False), "md_h": (3, 3, True, False), "md_code": (7, 7, False, False), "md_quote": (8, 8, False, False), "md_bold": (None, None, True, False), "sum": (7, 7, False, False),
        "diff_add": (2, 2, False, False), "diff_del": (1, 1, False, False), "diff_hunk": (6, 6, True, False),
        "diff_ctx": (7, 7, False, False),
        "sev_reach": (5, 5, True, False), "sev_bug": (1, 1, True, False), "sev_regress": (3, 3, True, False),
        "sev_logic": (3, 3, False, False), "sev_style": (6, 6, False, False), "sev_design": (2, 2, False, False),
        "people": ([2, 3, 5, 6, 1, 7], [2, 3, 5, 6, 1, 7]),
    },
}
THEME = cfg("theme") if cfg("theme") in THEMES else "dark"
PERSON_COLOR = {}   # login (lower) -> palette index, assigned in order of first appearance
_LOGIN_RE = re.compile(r"@[A-Za-z0-9][A-Za-z0-9-]*")


def person_index(login):
    key = login.lower().lstrip("@")
    if key not in PERSON_COLOR:
        PERSON_COLOR[key] = len(PERSON_COLOR)
    return PERSON_COLOR[key]


def term_256():
    return THEME != "basic" and (os.environ.get("TERM", "").endswith("256color") or bool(os.environ.get("COLORTERM")))


def style_spec(st):
    """(fg, bold, dim) for a style under the current theme; fg is a colour number or None."""
    th = THEMES[THEME]
    if st.startswith("person:"):
        pal = th["people"][0 if term_256() else 1]
        return pal[person_index(st[7:]) % len(pal)], False, False
    spec = th.get(st)
    if not spec:
        return None, False, False
    fg256, fg8, bold, dim = spec
    return (fg256 if term_256() else fg8), bold, dim


_MD_INLINE = re.compile(r"(`[^`]+`)|(\*\*[^*]+\*\*)|(\[[^\]]+\]\([^)]+\))|(https?://[^\s)]+)")


def render_table(lines, max_col=48):
    """Markdown table lines -> aligned box lines: '│ a │ b │' rows and a '├───┼───┤' rule under the header."""
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in lines]
    sep = next((i for i, r in enumerate(rows) if i > 0 and all(re.fullmatch(r":?-+:?", c) for c in r if c) and any(r)), None)
    aligns = []
    if sep is not None:
        for c in rows[sep]:
            aligns.append("c" if c.startswith(":") and c.endswith(":") else "r" if c.endswith(":") else "l")
        header, body = rows[:sep], rows[sep + 1:]
    else:
        header, body = [], rows
    ncol = max(len(r) for r in rows)
    aligns += ["l"] * (ncol - len(aligns))
    widths = [min(max_col, max([dw(r[i]) for r in header + body if i < len(r)] + [1])) for i in range(ncol)]

    def fmt(r):
        cells = []
        for i in range(ncol):
            c = trunc(r[i] if i < len(r) else "", widths[i])
            padn = widths[i] - dw(c)
            if aligns[i] == "r":
                c = " " * padn + c
            elif aligns[i] == "c":
                c = " " * (padn // 2) + c + " " * (padn - padn // 2)
            else:
                c = c + " " * padn
            cells.append(c)
        return "│ " + " │ ".join(cells) + " │"

    out = [fmt(h) for h in header]
    if header:
        out.append("├─" + "─┼─".join("─" * w for w in widths) + "─┤")
    out += [fmt(b) for b in body]
    return out


def render_markdown(text, width):
    """Wrap prose to width; tables become aligned box lines (not wrapped — scroll sideways with H/L)."""
    out, block, in_code = [], [], False
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("```"):
            in_code = not in_code
        if not in_code and ln.lstrip().startswith("|") and i + 1 < len(lines) and lines[i + 1].lstrip().startswith("|"):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            out.extend(wrap("\n".join(block), width) if block else [])
            block = []
            out.extend(render_table(lines[i:j]))
            i = j
            continue
        block.append(ln)
        i += 1
    if block:
        out.extend(wrap("\n".join(block), width))
    return out


def md_segments(text, in_code=False):
    """(text, style) segments for one line of markdown: headings, code, bold, links, quotes, bullets."""
    if in_code:
        return [(text, "md_code")]
    st = text.lstrip()
    if st.startswith("```"):
        return [(text, "md_code")]
    if re.match(r"#{1,6} ", st):
        return [(text, "md_h")]
    if st.startswith(">"):
        return [(text, "md_quote")]
    m = re.match(r"^(\s*)([-*+]|\d+[.)]) (.*)$", text)
    if m and not text.startswith("    "):
        bullet = "• " if m.group(2) in "-*+" else m.group(2) + " "
        segs = [(m.group(1) + bullet, "fold")]
        text = m.group(3)
    else:
        segs = []
    pos = 0
    for mm in _MD_INLINE.finditer(text):
        if mm.start() > pos:
            segs.append((text[pos:mm.start()], ""))
        tok = mm.group(0)
        if mm.group(1):
            segs.append((tok, "md_code"))
        elif mm.group(2):
            segs.append((tok[2:-2], "md_bold"))
        elif mm.group(3):
            lm = re.match(r"\[([^\]]+)\]\(([^)]+)\)", tok)
            segs.append((lm.group(1), "url"))
            segs.append((f" ({lm.group(2)})", "meta"))
        else:
            segs.append((tok, "url"))
        pos = mm.end()
    if pos < len(text):
        segs.append((text[pos:], ""))
    return segs or [(text, "")]


def ansi_style(st):
    fg, bold, dim = style_spec(st)
    codes = (["1"] if bold else []) + (["2"] if dim else []) + (["4"] if st == "url" else [])
    if fg is not None:
        codes.append(f"3{fg}" if fg < 8 else f"9{fg - 8}" if fg < 16 else f"38;5;{fg}")
    return f"\033[{';'.join(codes)}m" if codes else ""


_PRE_TREE = re.compile(r"^[│ ]*[├└]─ ")
_PRE_LOG = re.compile(r"^[|*o@ \\/_]+  ")
_COMMENT_HEAD = re.compile(r"^(?:\S+ )?o @\S+(?: \[[^\]]*\])? ")
_ITEM_HEAD = re.compile(r"^\S+ \[[^\]]*\](?:\[[^\]]*\])? ")
_DATE_HEAD = re.compile(r"^(?:\d{4}-\d\d-\d\d|\+\d+d(?: \d\d-\d\d)?|\d\d-\d\d) ")


DIFF_KINDS = ("diff_add", "diff_del", "diff_ctx", "diff_hunk")


def segments(row, g):
    t = row.text
    if row.kind in DIFF_KINDS:
        if row.kind != "diff_hunk" and t[:1] in ("⚠", "ℹ"):
            return [(t[:2], "sev_bug" if t[0] == "⚠" else "sev_logic"), (t[2:], row.kind)]
        return [(t, row.kind)]
    if row.kind.startswith("sev_"):
        return [(t, row.kind)]
    if row.kind in ("head", "sec"):
        return [(t, "head")]
    if row.kind in ("link", "conn"):
        return [(t, "link")]
    if row.kind == "note":
        return [(t, "sum" if t.lstrip().startswith("↳ »") else "meta")]
    if row.kind == "url":
        lead = t[:len(t) - len(t.lstrip())]
        return ([(lead, "")] if lead else []) + [(t.strip(), "url")]
    if row.kind in ("md", "md_code"):
        return md_segments(t, row.kind == "md_code")
    if row.kind == "md_table":
        if t.startswith("├"):
            return [(t, "meta")]
        segs, first = [], row.jump == "head"
        for part in re.split(r"(│)", t):
            segs.append((part, "meta" if part == "│" else ("md_bold" if first else "")))
        return segs
    if row.kind == "mention":
        i = t.find("  ← ")
        head = segments(Row(t[:i] if i >= 0 else t, row.nid), g)
        return head + ([(t[i:], "in")] if i >= 0 else [])
    segs = []
    m = _PRE_TREE.match(t) or _PRE_LOG.match(t)
    if m:
        segs.append((m.group(0), "pre"))
        t = t[m.end():]
    if t.startswith("✎ "):
        segs.append(("✎ ", "fold"))
        t = t[2:]
    if t[:2] in ("▾ ", "▸ ", "· "):
        segs.append((t[:2], "fold" if t[0] == "▸" else "pre"))
        t = t[2:]
    for w in EDGE_WORDS:
        if t.startswith(w + " "):
            segs.append((w + " ", EDGE_STYLE.get(w, "out")))
            t = t[len(w) + 1:]
            break
    n = g.nodes.get(row.nid) if row.nid else None
    md = _DATE_HEAD.match(t)
    if md and n is not None and n.kind != "person":
        segs.append((md.group(0), "meta"))
        t = t[md.end():]
    if n is None:
        segs.append((t, ""))
    elif n.kind == "person":
        segs.append((t, "person"))
    elif n.kind == "comment":
        m2 = _COMMENT_HEAD.match(t)
        if m2:
            segs.append((m2.group(0), "comment"))
            t = t[m2.end():]
        if t.startswith("» "):
            body, tail = (t.split("  → ", 1) + [""])[:2]   # log rows append "  → targets"
            segs.append((body, "pending" if body == "» " + PENDING_TEXT else "sum"))
            if tail:
                segs.append(("  → " + tail, ""))
        else:
            segs.append((t, ""))
    else:
        m3 = _ITEM_HEAD.match(t)
        st = n.state_label()
        style = st if st in ("draft", "merged", "closed") else ("pr" if n.is_pr else ("issue" if n.is_pr is False else ""))
        if m3:
            segs.append((m3.group(0), style))
            t = t[m3.end():]
        if "  ⟵ " in t:
            t, why = t.split("  ⟵ ", 1)
            segs.append((t, "stub" if n.stub else ""))
            segs.append(("  ⟵ " + why, "in"))
            return [x for x in segs if x[0]]
        i = t.find("  ")
        if i >= 0:
            segs.append((t[:i], "stub" if n.stub else ""))
            segs.append((t[i:], "meta"))
        else:
            segs.append((t, "stub" if n.stub else ""))
    return [x for x in segs if x[0]]


def colorize_people(segs):
    """Split @login tokens out of segments so each person gets a colour of their own."""
    out = []
    for text, st in segs:
        if st in ("head", "fold", "issue", "pr", "draft", "merged", "closed", "closes", "closedby") or "@" not in text:
            out.append((text, st))
            continue
        pos = 0
        for m in _LOGIN_RE.finditer(text):
            if m.start() > pos:
                out.append((text[pos:m.start()], st))
            out.append((m.group(0), f"person:{m.group(0)[1:]}"))
            pos = m.end()
        if pos < len(text):
            out.append((text[pos:], st))
    return out


def ansi_rows(rows, g):
    out = []
    for r in rows:
        out.append("".join(f"{ansi_style(st)}{tx}\033[0m" if ansi_style(st) else tx
                           for tx, st in colorize_people(segments(r, g))))
    return "\n".join(out)


def item_degree(g, nid):
    items = set()
    for m, t, o in g.adj[nid]:
        mn = g.nodes[m]
        if mn.kind == "item":
            items.add(m)
        elif mn.kind == "comment" and t == "comment":
            for m2, t2, o2 in g.adj[m]:
                if g.nodes[m2].kind == "item" and m2 != nid:
                    items.add(m2)
    return len(items)


def pick_root(g, comp):
    items = [g.nodes[i] for i in comp if g.nodes[i].kind == "item"]
    fetched = [n for n in items if not n.stub] or items
    return max(fetched, key=lambda n: (item_degree(g, n.id), -n.time)).id


def comp_summary(g, comp):
    items = [g.nodes[i] for i in comp if g.nodes[i].kind == "item"]
    cs = [i for i in comp if g.nodes[i].kind == "comment"]
    ps = sorted(i for i in comp if g.nodes[i].kind == "person")
    open_ = sum(1 for n in items if n.state == "OPEN")
    s = f"{len(items)} items ({open_} open), {len(cs)} linked comments"
    if ps:
        s += ", people: " + " ".join(ps)
    return s


# --------------------------------------------------------------------------
# rows: every renderer yields Row(text, nid, jump, kind) so the TUI can put a cursor on it
# --------------------------------------------------------------------------
class Row:
    __slots__ = ("text", "nid", "jump", "kind", "mark")

    def __init__(self, text, nid=None, jump=None, kind="", mark=None):
        self.text, self.nid, self.jump, self.kind, self.mark = text, nid, jump, kind, mark
        # kind: "" node line | "head" section header | "link" cross-link line | "conn" log connector
        # mark: todo entry id, for rows in the Inbox todo section (the item may be outside this graph)


# --------------------------------------------------------------------------
# tree layout
# --------------------------------------------------------------------------
def bfs_tree(g, comp, root):
    """Shortest-path tree from root (comment<->its item costs 0, other edges 1).

    Persons are never expanded (they would tie every component together);
    they are rendered as leaf lines on the nodes that mention them.
    """
    dist, parent, pedge = {root: 0}, {root: None}, {root: None}
    dq = deque([root])
    while dq:
        x = dq.popleft()
        for m, t, o in g.adj[x]:
            if m not in comp or g.nodes[m].kind == "person":
                continue
            cost = 0 if t == "comment" else 1
            d = dist[x] + cost
            if m not in dist or d < dist[m]:
                dist[m], parent[m], pedge[m] = d, x, (t, o)
                (dq.appendleft if cost == 0 else dq.append)(m)
    children = defaultdict(list)
    for m, p in parent.items():
        if p is not None:
            children[p].append(m)
    return parent, pedge, children


def short_ref(g, n):
    if n.kind == "item":
        return g.label_num(n)
    if n.kind == "comment":
        return f"{g.label_num(g.nodes[n.parent])}:o{rel_days(n, g)}"
    return n.id


TREE_ORDER = {"closes": 0, "ref": 1, "comment": 2, "mention": 3}


def tree_rows(g, comp, root, width, collapsed=frozenset(), marks=False):
    """marks=True prefixes each node with ▾ (expanded) / ▸ (folded, with [+N]) / · (leaf) for the TUI."""
    parent, pedge, children = bfs_tree(g, comp, root)
    rows = []

    def count_desc(nid):
        return sum(1 + count_desc(c) for c in children.get(nid, []))

    def sort_key(m):
        t, o = pedge[m]
        n = g.nodes[m]
        return (TREE_ORDER[t], not o, n.kind != "item", n.time)

    def walk(nid, prefix, is_last, is_root):
        n = g.nodes[nid]
        branch = "" if is_root else ("└─ " if is_last else "├─ ")
        lab = EDGE_LABEL[pedge[nid]] if pedge[nid] else ""
        show_item = not (n.kind == "comment" and parent.get(nid) == n.parent)
        text = node_label(g, n, width, show_item)
        if n.kind == "item" and not n.stub and n.comments_total:
            text += f"  ({n.comments_total} comments"
            text += f", {len(g.comments_of(nid))} linked)" if g.show_linked else ")"
        kids = sorted(children.get(nid, []), key=sort_key)
        # edges that are not tree edges: shown compactly, the other end is drawn elsewhere
        xl, xl_first, mentions = defaultdict(list), {}, []
        for m, t, o in sorted(g.adj[nid], key=lambda e: g.nodes[e[0]].time):
            if m not in comp:
                continue
            if g.nodes[m].kind == "person":
                if o and not is_root and m != root:
                    mentions.append(m)
                continue
            if m == parent.get(nid) or parent.get(m) == nid:
                continue
            key = EDGE_LABEL[(t, o)].strip() or "comment"
            xl[key].append(short_ref(g, g.nodes[m]))
            xl_first.setdefault(key, m)
        extra = []
        if xl:
            extra.append(("⇢ " + " · ".join(f"{k} {' '.join(dict.fromkeys(v))}" for k, v in xl.items()),
                          next(iter(xl_first.values()))))
        if mentions:
            extra.append(("mentions " + " ".join(dict.fromkeys(mentions)), mentions[0]))
        if nid in collapsed and (kids or extra):
            mark = "▸ " if marks else ""
            rows.append(Row(f"{prefix}{branch}{mark}{lab}{text}  [+{count_desc(nid) + len(extra)}]", nid))
            return
        mark = ("▾ " if (kids or extra) else "· ") if marks else ""
        rows.append(Row(f"{prefix}{branch}{mark}{lab}{text}", nid))
        sub = prefix + ("" if is_root else ("   " if is_last else "│  "))
        total = len(extra) + len(kids)
        for i, (e, jump) in enumerate(extra, 1):
            rows.append(Row(sub + ("└─ " if i == total else "├─ ") + e, nid, jump, "link"))
        for i, k in enumerate(kids, len(extra) + 1):
            walk(k, sub, i == total, False)

    walk(root, "", True, True)
    rest = [i for i in comp if i not in parent and g.nodes[i].kind == "item"]
    for r in rest:  # unreachable from root (cannot happen for a component, kept as a guard)
        rows.append(Row(""))
        p2, e2, c2 = bfs_tree(g, comp, r)
        parent.update(p2), pedge.update(e2), children.update(c2)
        walk(r, "", True, True)
    return rows


def render_tree(g, comp, root, width):
    return [r.text for r in tree_rows(g, comp, root, width)]


# --------------------------------------------------------------------------
# log layout (git log --graph style lanes)
# --------------------------------------------------------------------------
GLYPH = {"item": "*", "comment": "o", "person": "@"}


def log_rows(g, comp, width):
    ids = [i for i in comp if i in g.nodes]
    order = sorted(ids, key=lambda i: (-g.nodes[i].time, i))
    pos = {n: i for i, n in enumerate(order)}
    adj = defaultdict(set)
    for s, d, t in g.edges:
        if s in pos and d in pos:
            adj[s].add(d)
            adj[d].add(s)
    lanes = []
    raw = []   # (lane string, text or None, nid)

    def alloc(m):
        for j, l in enumerate(lanes):
            if l is None:
                lanes[j] = m
                return j
        lanes.append(m)
        return len(lanes) - 1

    for n in order:
        node = g.nodes[n]
        col = lanes.index(n) if n in lanes else alloc(n)
        later = sorted((m for m in adj[n] if pos[m] > pos[n]), key=lambda m: pos[m])
        chars = ["|" if l is not None else " " for l in lanes]
        chars[col] = GLYPH[node.kind]
        text = node_label(g, node, width)
        if node.kind == "comment":
            tg = [g.label_num(g.nodes[m]) if g.nodes[m].kind == "item" else m
                  for m in sorted(adj[n], key=lambda m: pos[m]) if m != node.parent]
            if tg:
                text += "  → " + " ".join(tg)
        raw.append((" ".join(chars), text, n))
        before = list(lanes)
        lanes[col] = None
        targets = []
        for m in later:
            if m in lanes:
                k = lanes.index(m)
            elif lanes[col] is None:
                lanes[col] = m
                k = col
            else:
                k = alloc(m)
            if k != col:
                targets.append(k)
        # one connector row per target, nearest first (git style "|\" then "| \")
        width_ = max(2 * len(lanes) - 1, 1)
        base = [" "] * width_
        for j, l in enumerate(lanes):
            if l is not None and j != col and j < len(before) and before[j] is not None:
                base[2 * j] = "|"
        if lanes[col] is not None:
            base[2 * col] = "|"
        for i, k in enumerate(sorted(targets, key=lambda k: abs(k - col))):
            conn = list(base)
            if i > 0 or abs(k - col) > 1:
                conn[2 * col] = "|"  # git style "|_|/": the line leaves from the lane, not the glyph
            if k > col:
                for p in range(2 * col + 1, 2 * k - 1):
                    if conn[p] == " ":
                        conn[p] = "_"
                conn[2 * k - 1] = "\\"
            else:
                conn[2 * k + 1] = "/"
                for p in range(2 * k + 2, 2 * col):
                    if conn[p] == " ":
                        conn[p] = "_"
            raw.append(("".join(conn).rstrip(), None, None))
            base[2 * k] = "|"  # that lane is now "attached"; later rows pass straight through it
        while lanes and lanes[-1] is None:
            lanes.pop()
    w = max((len(r[0]) for r in raw), default=0)
    rows = []
    for lane, text, nid in raw:
        if text is None:
            rows.append(Row(lane.ljust(w), None, None, "conn"))
        else:
            rows.append(Row(lane.ljust(w) + "  " + text, nid))
    return rows


def render_log(g, comp, width):
    return [r.text for r in log_rows(g, comp, width)]


# --------------------------------------------------------------------------
# top-level renderers
# --------------------------------------------------------------------------
LEGEND_TREE = ("legend: date first = when the issue/PR was opened; +Nd = days after that for a comment; "
               "[I]=issue (green) [PR]=pull request (blue), [draft]/[merged]/[closed] only when not open; o=comment; @=person; "
               "→ refs = this one references that; ← cited-by = that one references this; "
               "→ closes / ← closed-by = PR fixes issue; "
               "⇢ = link to a node drawn elsewhere (#N:o+Nd = the comment on #N, N days after it opened)")
LEGEND_LOG = ("legend: newest on top; *=issue/PR o=comment @=person; lanes connect referencing nodes; "
              "'→' lists what a comment points at")


def tr_note(g):
    parts = []
    if getattr(g, "translated", 0):
        parts.append(f"{g.translated} titles/excerpts translated to {TR_LANG}")
    if getattr(g, "summarized", 0):
        parts.append(f"{g.summarized} comments summarized")
    return f"   ({', '.join(parts)} by {model_label(TR_MODEL)})" if parts else ""


def overview_rows(g, layout, width, collapsed=frozenset(), title=None, marks=False, legend=True):
    comps = components(g)
    comps.sort(key=lambda c: (-sum(1 for i in c if g.nodes[i].kind == "item"),
                              -max(g.nodes[i].time for i in c)))
    linked = [c for c in comps if sum(1 for i in c if g.nodes[i].kind == "item") > 1]
    isolated = [c for c in comps if sum(1 for i in c if g.nodes[i].kind == "item") == 1]
    head = title or f"repo: {g.primary}"
    fa = getattr(g, "fetched_at", None)
    if fa:
        head += f"   (data fetched {datetime.fromtimestamp(fa).strftime('%Y-%m-%d %H:%M')})"
    rows = [Row(head + tr_note(g), kind="head")]
    if legend:
        rows.append(Row(LEGEND_LOG if layout == "log" else LEGEND_TREE, kind="head"))
    for idx, comp in enumerate(linked, 1):
        rows.append(Row(""))
        if layout == "log":
            rows.append(Row(f"== [{idx}] {comp_summary(g, comp)} ==", kind="head"))
            rows.extend(log_rows(g, comp, width))
        else:
            root = pick_root(g, comp)
            rows.append(Row(f"== [{idx}] {comp_summary(g, comp)} — tree rooted at {g.label_num(g.nodes[root])} "
                            f"(most linked: {item_degree(g, root)} items) ==", kind="head"))
            rows.extend(tree_rows(g, comp, root, width, collapsed, marks))
    if isolated:
        rows.append(Row(""))
        rows.append(Row(f"== isolated ({len(isolated)} items with no #-links) ==", kind="head"))
        for comp in sorted(isolated, key=lambda c: -max(g.nodes[i].time for i in c)):
            it = next(g.nodes[i] for i in comp if g.nodes[i].kind == "item")
            s = item_label(g, it, width)
            ps = sorted(i for i in comp if g.nodes[i].kind == "person")
            extra = []
            if it.comments_total:
                extra.append(f"{it.comments_total} comments")
            if ps:
                extra.append("mentions " + " ".join(ps))
            if extra:
                s += "  (" + "; ".join(extra) + ")"
            rows.append(Row(s, it.id))
    return rows


def render_overview(g, layout, width, title=None):
    return "\n".join(r.text for r in overview_rows(g, layout, width, title=title))


def focus_rows(g, root, layout, width, collapsed=frozenset(), marks=False, legend=True):
    comps = [c for c in components(g) if root in c]
    comp = comps[0] if comps else set(g.nodes)  # a person root is not a component member itself
    rows = [Row(f"focus: {root}" + tr_note(g), kind="head")]
    if legend:
        rows.append(Row(LEGEND_LOG if layout == "log" else LEGEND_TREE, kind="head"))
    rows.append(Row(""))
    if layout == "log":
        rows.extend(log_rows(g, comp, width))
    else:
        rows.extend(tree_rows(g, comp, root, width, collapsed, marks))
    return rows


def render_focus(g, root, layout, width):
    return "\n".join(r.text for r in focus_rows(g, root, layout, width))


def render_show(g, nid, width=100):
    n = g.nodes[nid]
    out = []
    if n.kind == "item":
        out.append(item_label(g, n, width))
        if n.tr_title:
            out.append(f"  original title: {n.title}")
        out.append(f"  url: {n.url or '(stub - not fetched)'}")
        if n.labels:
            out.append(f"  labels: {', '.join(n.labels)}")
        if n.updated:
            out.append(f"  updated: {short_date(n.updated)}")
    else:
        out.append(node_label(g, n, width))
        if n.url:
            out.append(f"  url: {n.url}")
    out.append("")
    out.append("edges:")
    for m, t, o in sorted(g.adj[nid], key=lambda e: (e[1], not e[2], g.nodes[e[0]].time)):
        if t == "comment" and n.kind == "item":
            continue
        out.append(f"  {EDGE_LABEL[(t, o)]}{node_label(g, g.nodes[m], width)}")
        ctx = g.ctx.get((nid, m)) if o else g.ctx.get((m, nid))
        if ctx:
            out.append(f"      ↳ \"{ctx}\"")
    if n.kind == "item":
        cs = g.comments_of(nid)
        if cs or n.comments_total:
            out.append("")
            out.append(f"comments ({n.comments_total} total, {len(cs)} loaded):")
            for c in cs:
                tg = [EDGE_LABEL[(t, o)] + (g.label_num(g.nodes[m]) if g.nodes[m].kind == "item" else m)
                      for m, t, o in g.adj[c.id] if t != "comment"]
                out.append(f"  {comment_label(g, c, width, False)}" + (("  → " + ", ".join(tg)) if tg else ""))
    if n.body:
        out.append("")
        out.append("body:")
        lines = n.body.splitlines()
        out.extend("  " + l for l in lines[:40])
        if len(lines) > 40:
            out.append(f"  … ({len(lines) - 40} more lines)")
    return "\n".join(out)


# ------------------------------------------------------------------ review rows
# Files / Diff / Findings, the three panels of review mode. Every row carries the style it wants in
# its `kind` (diff_add … , sev_bug …), so segments() colours them without a node lookup: review ids
# ("file:…", "line:…", "finding:…") are not in the graph.
SEV_LABEL = {"reach": "unreachable", "bug": "bug", "regress": "regression",
             "logic": "logic", "style": "style", "design": "design"}
SEV_MARK = {"reach": "⚠", "bug": "⚠", "regress": "⚠", "logic": "ℹ", "style": "ℹ", "design": "ℹ"}
VERDICT_MARK = {"CONFIRMED": "✓", "PLAUSIBLE": "?", "FALSE": "·"}
ANCHOR_MARK = {"moved": " ⚠", "unanchored": " ⊘"}
DIFF_GUTTER = 10        # marker(2) + line number(5) + space + tag + space
TAB_WIDTH = 4


def expand_tabs(s, tw=TAB_WIDTH):
    """Tabs to the next stop, measured in display columns (a CJK character is two)."""
    if "\t" not in s:
        return s
    out, col = [], 0
    for ch in s:
        if ch == "\t":
            n = tw - (col % tw) or tw
            out.append(" " * n)
            col += n
        else:
            out.append(ch)
            col += dw(ch)
    return "".join(out)


def short_path(path, w):
    """The path if it fits, else its last components, else the truncated basename."""
    if dw(path) <= w:
        return path
    base = path.rsplit("/", 1)[-1]
    return base if dw(base) <= w else trunc(base, w)


def review_files_rows(rv, width, sel=None):
    """The PR, then one row per changed file. `sel` is the path the Diff panel is showing."""
    add, dele = rv.stats()
    head = [f"#{rv.number} {rv.title}",
            f"{rv.state_label()} · @{rv.author or '?'} · {(rv.head_oid or '')[:7]}",
            f"{len(rv.files)} file{'s' if len(rv.files) != 1 else ''}  +{add} -{dele}"]
    if rv.status in ("worktree", "running", "verifying"):
        head.append(f"⋯ {rv.status}")
    elif rv.error:
        head.append(rv.error)
    rows = [Row(trunc(t, width), kind="head") for t in head]
    rows.append(Row(""))
    worst = {}
    for f in rv.findings:
        if f.verdict != "FALSE" and f.state != "ignored":
            cur = worst.get(f.path)
            if cur is None or SEVERITIES.index(f.severity) < SEVERITIES.index(cur):
                worst[f.path] = f.severity
    for f in rv.files:
        mark = {"added": "+", "deleted": "-", "renamed": "→", "copied": "≡"}.get(f.status, " ")
        cur = "▸" if f.path == sel else " "
        sev = SEV_MARK.get(worst.get(f.path), "")
        tail = f" +{f.additions} -{f.deletions} {sev}".rstrip()
        name = short_path(f.path, max(width - dw(tail) - 3, 6))
        pad = " " * max(width - dw(tail) - dw(name) - 3, 1)
        rows.append(Row(f"{cur}{mark}{name}{pad}{tail}", f"file:{f.path}"))
    if rv.worktree:
        rows.append(Row(""))
        rows.append(Row(trunc(f"worktree {fmt_bytes(rv.wt_size)}", width), kind="head"))
    return rows


def diff_rows(rv, path, width, collapsed=frozenset()):
    """One file's diff. Rows carry `line:<path>:<side>:<n>` so a finding can jump onto its own line."""
    f = rv.file(path) if path else None
    if f is None:
        return [Row("(no file selected — ⏎ on one in Files)", kind="head")]
    if f.binary:
        return [Row(f"{f.path}: binary file, no diff", kind="head")]
    if not f.hunks:
        return [Row(f"{f.path}: {f.status}, no content change", kind="head")]
    flag = {}          # (side, line) -> marker of the worst finding sitting there
    for fi in rv.findings:
        if fi.path != path or fi.verdict == "FALSE" or fi.state == "ignored" or fi.line is None:
            continue
        key = (fi.side, fi.line)
        if key not in flag or SEVERITIES.index(fi.severity) < SEVERITIES.index(flag[key][1]):
            flag[key] = (SEV_MARK[fi.severity], fi.severity)
    body = max(width - DIFF_GUTTER, 12)
    rows = []
    for hi, h in enumerate(f.hunks):
        nid = f"hunk:{path}#{hi}"
        folded = nid in collapsed
        rows.append(Row(f"{'▸' if folded else '▾'} {clip(h.header, 0, max(width - 2, 8))}",
                        nid, None, "diff_hunk"))
        if folded:
            continue
        for tag, old_no, new_no, text in h.lines:
            side = "LEFT" if tag == "-" else "RIGHT"
            no = old_no if tag == "-" else new_no
            mark = flag.get((side, no), ("  ",))[0]
            kind = {"+": "diff_add", "-": "diff_del"}.get(tag, "diff_ctx")
            rows.append(Row(f"{mark:<2}{no if no is not None else '':>5} {tag} "
                            f"{clip(expand_tabs(text), 0, body)}",
                            f"line:{path}:{side}:{no}", None, kind))
        rows.append(Row(""))
    return rows


FIND_TABS = [("open", "open"), ("posted", "posted"), ("ignored", "ignored"),
             ("dropped", "dropped"), ("changes", "changes"), ("github", "github")]


def _find_bucket(f):
    if f.verdict == "FALSE":
        return "dropped"
    return {"posted": "posted", "ignored": "ignored"}.get(f.state, "open")


def subjective_held(rv):
    """slop-indicators.md's rule, in code: opinions stay out of the way while a real defect stands."""
    if cfg("review_subjective") != "auto":
        return False
    return any(f.verdict == "CONFIRMED" and not f.subjective and _find_bucket(f) == "open"
               for f in rv.findings)


def findings_rows(rv, tab, width, checked=frozenset()):
    if tab == "changes":
        return changes_rows(rv, width)
    if tab == "github":
        return threads_rows(rv, width)
    if rv.status in ("worktree", "running", "verifying"):
        return [Row(f"⋯ {rv.status}…", kind="head")]
    if rv.error:
        return [Row("review failed", kind="head"), Row(trunc(rv.error, width * 3), kind="note")]
    if not rv.findings and not rv.engine:
        return [Row("no review yet — R runs one" if ai_available() else
                    f"no AI CLI ({CLAUDE_BIN}) — diff only", kind="head")]
    picked = [f for f in rv.findings if _find_bucket(f) == tab]
    if not picked:
        return [Row(f"nothing {tab}", kind="head")]
    held = subjective_held(rv) if tab == "open" else False
    rows, n_held, seen = [], 0, None
    order = sorted(picked, key=lambda f: (SEVERITIES.index(f.severity), f.path or "", f.line or 0))
    for i, f in enumerate(order, 1):
        if held and f.subjective:
            n_held += 1
            continue
        if f.severity != seen:
            seen = f.severity
            rows.append(Row(f"● {SEV_LABEL[f.severity]} "
                            f"{sum(1 for x in order if x.severity == f.severity)}", kind="head"))
        sel = "•" if f.fid in checked else " "
        v = VERDICT_MARK.get(f.verdict, " ")
        rows.append(Row(f"{sel}#{i} {v} {trunc(f.title, max(width - 6, 8))}",
                        f"finding:{f.fid}", f"line:{f.path}:{f.side}:{f.line}", f"sev_{f.severity}"))
        rows.append(Row(f"   {short_path(f.path or '?', max(width - 12, 8))}:{f.line}"
                        f"{ANCHOR_MARK.get(f.anchor, '')}", f"finding:{f.fid}", None, "note"))
    if n_held:
        rows.append(Row(f"● {n_held} subjective held back (a confirmed defect stands)", kind="head"))
    return rows


def changes_rows(rv, width):
    """What the review broke the diff into before judging it (review-core.md TASK 1B)."""
    if not rv.changes:
        return [Row("no change analysis yet", kind="head")]
    rows, seen = [], None
    if rv.reachability:
        v = rv.reachability.get("verdict", "?")
        rows.append(Row(f"reachability: {v}", kind="head"))
        rows.append(Row(f"   {trunc(rv.reachability.get('reason', ''), width * 3)}", kind="note"))
    for c in rv.changes:
        if c.kind != seen:
            seen = c.kind
            rows.append(Row(f"● {c.kind}", kind="head"))
        rows.append(Row(f" {c.cid} {trunc(c.symbol or c.path or '', max(width - 12, 8))}",
                        f"change:{c.cid}", f"file:{c.path}"))
        rows.append(Row(f"   {trunc(c.summary, max(width - 4, 8))}", f"change:{c.cid}", None, "note"))
    return rows


def threads_rows(rv, width):
    """Review threads already on the PR — so gg does not repeat what a human already said."""
    if not rv.threads:
        return [Row("no review threads on this PR", kind="head")]
    rows = []
    for t in rv.threads:
        state = " (resolved)" if t.get("resolved") else (" (outdated)" if t.get("outdated") else "")
        first = (t.get("comments") or [{}])[0]
        rows.append(Row(f"@{first.get('author', '?')} "
                        f"{short_path(t.get('path') or '?', max(width - 16, 8))}:{t.get('line')}{state}",
                        f"thread:{t.get('id')}", f"line:{t.get('path')}:{t.get('side')}:{t.get('line')}"))
        rows.append(Row(f"   {trunc(first.get('body', ''), max(width - 4, 8))}",
                        f"thread:{t.get('id')}", None, "note"))
    return rows


# --------------------------------------------------------------------------
# TUI (curses): keyboard cursor over the same rows
# --------------------------------------------------------------------------
def _cw(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def dw(s):
    return sum(_cw(c) for c in s)


def char_at(s, col):
    """The character drawn at display column col of s ('' beyond the end)."""
    c = 0
    for ch in s:
        w = _cw(ch)
        if c <= col < c + w:
            return ch
        c += w
    return ""


def slice_cols(s, c0, c1):
    """Characters of s drawn between display columns c0 (inclusive) and c1 (exclusive)."""
    out, c = [], 0
    for ch in s:
        w = _cw(ch)
        if c + w > c0 and c < c1:
            out.append(ch)
        c += w
    return "".join(out)


def copy_to_clipboard(text):
    """OSC 52 (works in xterm/Windows Terminal/kitty/… and through ssh) plus a local clipboard tool when present."""
    import base64
    import shutil
    ok = []
    try:
        sys.stdout.write("\033]52;c;" + base64.b64encode(text.encode("utf-8")).decode() + "\007")
        sys.stdout.flush()
        ok.append("OSC52")
    except OSError:
        pass
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"], ["pbcopy"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode("utf-8"), timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                ok.append(cmd[0])
                break
            except (OSError, subprocess.TimeoutExpired):
                pass
    return ok


def clip(s, start, width):
    """The part of s that occupies display columns [start, start+width)."""
    out, col = [], 0
    for ch in s:
        w = _cw(ch)
        if col + w > start + width:
            break
        if col >= start:
            out.append(ch)
        elif col + w > start:
            out.append(" ")  # a wide char cut in half
        col += w
    return "".join(out)


def split_width(s, width):
    """(head, rest): head is the longest prefix of s within `width` display columns."""
    col = 0
    for i, ch in enumerate(s):
        w = _cw(ch)
        if col + w > width:
            return s[:i], s[i:]
        col += w
    return s, ""


def wrap(text, width):
    """Display-width aware word wrap (CJK and URLs included)."""
    width = max(width, 8)
    out = []
    for line in (text or "").splitlines() or [""]:
        line = line.rstrip()
        if dw(line) <= width:
            out.append(line)
            continue
        cur, curw = [], 0
        for tok in re.split(r"(\s+)", line):
            if not tok:
                continue
            w = dw(tok)
            if curw + w > width and cur:
                out.append("".join(cur).rstrip())
                cur, curw = [], 0
                if tok.isspace():
                    continue
            while dw(tok) > width:
                head, tok = split_width(tok, width)
                out.append(head)
            cur.append(tok)
            curw += dw(tok)
        if cur:
            out.append("".join(cur).rstrip())
    return out


HELP = """gg tui — lazygit style layout

  side column: 1 Repo  2 Item  3 Inbox  4 Comments  5 Links  6 People      main: 0 (content · answer)
  1-6 / 0         jump to a panel            Tab / Shift-Tab  next / previous panel
  [ / ]           previous / next tab in the focused panel (Inbox sections, main content / answer)
  + / _           screen mode normal -> half (focused side panel fills the column) -> full (only that panel)
  Up/k Down/j     move            PgUp/PgDn , .   page       g/G < >  top / bottom      H / L  scroll sideways
  K / J           scroll the main panel from anywhere
  Enter           Inbox: make it the current item (Item, Comments, Links and People follow)
                  Item / Comments: read it in main      Links: go to that item      People: view as that person
  a               ask claude about the selection (answer tab in main); every answer is saved with its question,
                  anchored to that issue/PR/comment (~/.config/gitgraph/qa.json) and shown again on its answer tab
  d               details pager     o  open in browser
  i               translate the main content in full, on demand (also the [i 번역] title button); i again shows
                  the original. Nothing is translated in the background — only what you ask for
  C               open Claude Code in a tmux pane (or full screen) that can see what gg shows: it uses the
                  `gg mcp` server (register once: claude mcp add -s user gg -- gg mcp) — tools gg_state, gg_context,
                  gg_todo, gg_show, gg_graph, gg_open, gg_mark
  m               mark the selected issue/PR or comment for my next work and write a note; marked rows show ✎,
                  (on the answer tab: saves the answer text into the mark's note)
                  Home has a "todo" section, and ~/gitgraph-todo.md (gg config todo_file) is rewritten for the
                  next session (also `gg todo`). m again on a marked row: edit the note / mark done / remove
  Del             remove the mark on the selection outright (no menu)
  Esc / b         back (previous item and perspective)     f  forward
  u               view Inbox as another person
  r / R           refresh from GitHub in the background: r = only what changed (also automatic every --max-age
                  minutes), R = everything
  F2              guided tour of the screen (also: gg tutorial)
  c t s p h       comments mode · translation · summaries · people nodes · hops (for the CLI tree / Links depth)
  / n N           search in the focused panel      T  colour theme      $  token usage      ?  this help     q  quit
  Hangul IME      shortcuts still work while the keyboard is in Hangul mode (ㅓ = j, ㅏ = k, ㅁ = a …); the Enter/Space
                  the IME needs to commit a lone consonant is ignored, so ㅁ⏎ opens the question box like a
  y               copy the URL of the selection to the clipboard
  mouse           drag in main = select text and copy it on release (OSC 52 + wl-copy/xclip/xsel/pbcopy; in tmux
                  turn on set-clipboard; Shift+drag still uses the terminal's own selection);
                  click = focus a panel (its cursor stays); a click inside the focused panel selects the row;
                  click ‹ › in the Inbox title (or a tab name in Main's title) = switch tab;
                  double-click = Enter; click on a URL's text = open it in the browser;
                  wheel = scroll that panel without moving the cursor; back/forward buttons;
                  drag the border between the side column and main to resize (gg config side_width keeps it)
  O               options menu (comments / translation / summaries / people / hops / theme / screen)
  ?               key menu for the focused panel (Enter runs the action)     F1  this text
  (AI failures)   when the AI CLI fails (not logged in, expired token, missing), a popup offers to switch to an
                  installed alternative (codex / gemini / grok), keep trying, or turn AI features off

  legend          YYYY-MM-DD = when the issue/PR was opened, +Nd = a comment N days later; [I] issue [PR] pull request,
                  [draft]/[merged]/[closed] only when not open; → refs / ← cited-by; → closes / ← closed-by;
                  ⇢ = link to a node drawn elsewhere; ▾ open ▸ folded [+N] · leaf; » one-line summary
"""

BORDERS = {"rounded": "╭╮╰╯─│", "single": "┌┐└┘─│", "double": "╔╗╚╝═║", "bold": "┏┓┗┛━┃", "hidden": "      "}

# Korean 2-set keyboard: a shortcut typed while the IME is in Hangul mode arrives as jamo or a composed syllable;
# map it back to the Latin keys that were pressed so j/k/Enter-style bindings keep working.
JAMO_KEY = {"ㅂ": "q", "ㅈ": "w", "ㄷ": "e", "ㄱ": "r", "ㅅ": "t", "ㅛ": "y", "ㅕ": "u", "ㅑ": "i", "ㅐ": "o", "ㅔ": "p",
            "ㅁ": "a", "ㄴ": "s", "ㅇ": "d", "ㄹ": "f", "ㅎ": "g", "ㅗ": "h", "ㅓ": "j", "ㅏ": "k", "ㅣ": "l",
            "ㅋ": "z", "ㅌ": "x", "ㅊ": "c", "ㅍ": "v", "ㅠ": "b", "ㅜ": "n", "ㅡ": "m",
            "ㅃ": "Q", "ㅉ": "W", "ㄸ": "E", "ㄲ": "R", "ㅆ": "T", "ㅒ": "O", "ㅖ": "P",
            "ㅘ": "hk", "ㅙ": "ho", "ㅚ": "hl", "ㅝ": "nj", "ㅞ": "np", "ㅟ": "nl", "ㅢ": "ml",
            "ㄳ": "rt", "ㄵ": "sw", "ㄶ": "sg", "ㄺ": "fr", "ㄻ": "fa", "ㄼ": "fq", "ㄽ": "ft", "ㄾ": "fx", "ㄿ": "fv",
            "ㅀ": "fg", "ㅄ": "qt"}
_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
_JONG = " ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"


def hangul_keys(ch):
    """Latin key sequence behind one Hangul character ('ㅓ' -> 'j', '자' -> 'wk'), or '' if not Hangul."""
    if ch in JAMO_KEY:
        return JAMO_KEY[ch]
    o = ord(ch)
    if 0xAC00 <= o <= 0xD7A3:
        o -= 0xAC00
        parts = [_CHO[o // 588], _JUNG[(o % 588) // 28], _JONG[o % 28].strip()]
        return "".join(JAMO_KEY.get(j, "") for j in parts if j)
    return ""
LIST_KINDS = ("", "link", "mention", "sec") + DIFF_KINDS + tuple(f"sev_{s}" for s in SEVERITIES)


class Panel:
    def __init__(self, key, title, tabs=None, scroll_only=False):
        self.key, self.title, self.tabs, self.tab = key, title, tabs or [], 0
        self.rows, self.cur, self.top, self.hs = [], 0, 0, 0
        self.rect = (0, 0, 0, 0)          # content area: y, x, h, w
        self.scroll_only = scroll_only    # main content/answer: no cursor, just scrolling
        self.free = False                 # after a wheel scroll the view may leave the cursor off screen
        self.query = ""

    def valid(self, i):
        return 0 <= i < len(self.rows) and self.rows[i].kind in LIST_KINDS and self.rows[i].text.strip() != ""

    def current(self):
        return self.rows[self.cur] if 0 <= self.cur < len(self.rows) else None

    def move(self, delta):
        self.free = False
        if self.scroll_only:
            self.top = max(0, self.top + delta)
            return
        step = 1 if delta > 0 else -1
        i, left = self.cur, abs(delta)
        while left:
            j = i + step
            while 0 <= j < len(self.rows) and not self.valid(j):
                j += step
            if not 0 <= j < len(self.rows):
                break
            i, left = j, left - 1
        self.cur = i

    def settle(self):
        """Keep cur on a valid row and the viewport sane (called after rows change and before drawing)."""
        h = max(self.rect[2], 1)
        if self.scroll_only:
            self.top = max(0, min(self.top, max(len(self.rows) - h, 0)))
            return
        self.cur = max(0, min(self.cur, len(self.rows) - 1)) if self.rows else 0
        if self.rows and not self.valid(self.cur):
            j = next((i for i in range(self.cur, len(self.rows)) if self.valid(i)), None)
            if j is None:
                j = next((i for i in range(self.cur, -1, -1) if self.valid(i)), 0)
            self.cur = j
        self.top = max(0, min(self.top, max(len(self.rows) - h, 0)))
        if not self.free:
            if self.cur < self.top:
                self.top = self.cur
            if self.cur >= self.top + h:
                self.top = self.cur - h + 1

    def find(self, nid, near=None):
        hits = [i for i, r in enumerate(self.rows)
                if r.nid == nid and (r.kind in ("", "mention") or r.kind in DIFF_KINDS
                                     or r.kind.startswith("sev_"))]
        if not hits:
            return None
        return min(hits, key=lambda i: abs(i - (near if near is not None else self.cur)))

    def goto_nid(self, nid):
        i = self.find(nid)
        if i is not None:
            self.cur, self.free = i, False
        return i is not None

    def set_rows(self, rows, keep=True):
        keep_nid = self.current().nid if (keep and self.current()) else None
        self.rows = rows
        if keep_nid:
            self.goto_nid(keep_nid)


class Tui:
    SIDE = ["repo", "item", "home", "comments", "links", "people"]
    MODES = {"browse": SIDE, "review": ["rfiles", "rdiff", "rfind"]}
    HOME_TABS = [("todo", "todo"), ("turn", "my turn"), ("mention", "mentions"), ("opened", "opened"), ("active", "active"),
                 ("waiting", "waiting"), ("mine", "mine"), ("prs", "PRs by others"), ("stale", "stale"), ("all", "all")]
    MAIN_TABS = ["content", "answer"]
    COMMENTS_CYCLE = ["linked", "all", "none"]

    def __init__(self, scr, opts):
        import curses
        self.curses = curses
        self.scr = scr
        self.o = dict(opts)
        self.o.setdefault("summary", True)
        self.me = ME or [a.lower() for a in gh_accounts()]
        self.item, self.subject, self.hist, self.fwd = None, None, [], []
        self.todo = load_todo()
        self.show_tr = False   # main content: the original until i (or the title button) asks for a translation
        self.last_side = "home"   # the side list panel that stays expanded while main is focused
        self.tr_thread, self.tr_pending = None, None
        self.collapsed = set()
        self.focus, self.screen = "home", cfg("screen_mode") if cfg("screen_mode") in ("normal", "half", "full") else "normal"
        self.mode = "browse"               # browse (the graph) | review (one PR's diff and findings)
        self.rv, self.rv_path, self._new_rv = None, None, None
        self._told_review = False
        self.rv_folded, self.rv_checked, self.rv_tr = set(), set(), {}
        self.mode_focus = {"browse": "home", "review": "rfiles"}
        self.review_files_width = float(cfg("review_files_width") or 0.22)
        self.review_findings_width = float(cfg("review_findings_width") or 0.30)
        self.side_width = float(cfg("side_width") or 0.4)
        self.expand_focused = cfg("expand_focused").lower() not in ("false", "0", "no", "")
        self.expanded_weight = float(cfg("expanded_weight") or 2)
        self.border = BORDERS.get(cfg("border"), BORDERS["rounded"])
        self.panels = {
            "repo": Panel("repo", "Repo"),
            "item": Panel("item", "Item"),
            "home": Panel("home", "Inbox", [t for _, t in self.HOME_TABS]),
            "links": Panel("links", "Links"),
            "comments": Panel("comments", "Comments"),
            "people": Panel("people", "People"),
            "main": Panel("main", "Main", self.MAIN_TABS, scroll_only=True),
            "rfiles": Panel("rfiles", "Files"),
            "rdiff": Panel("rdiff", "Diff"),
            "rfind": Panel("rfind", "Findings", [t for _, t in FIND_TABS]),
        }
        self.panels["home"].tab = 1        # todo is the first tab, but the Inbox opens on my turn
        self.visible = []                  # panel keys drawn in the current layout
        self.title_zones = {}              # panel -> [(x0, x1, action)] clickable parts of its title bar
        self.msg, self.answer, self.answer_nid = "", None, None
        self.progress, self.jobs, self.t0, self.bg_error = None, [], time.time(), None
        self.last_refresh, self._new_g = time.time(), None
        self.enriched = set()
        self.ask_thread, self.ask_state = None, None
        self.ai_prompted = 0   # how many AI failures the switch popup has already handled
        self.mouse_ev, self.last_click = None, (0.0, -1, "")
        self.tr_saved = self.o["translate"] if self.o["translate"] != "none" else "zh"
        global PROGRESS
        PROGRESS = self.on_progress
        curses.curs_set(0)
        scr.keypad(True)
        self.pairs = {}
        try:
            curses.start_color()
            curses.use_default_colors()
        except curses.error:
            pass
        self.apply_theme()
        sys.stdout.write("\033[?1000h\033[?1002h\033[?1006h")   # SGR mouse reports incl. drags, parsed in read_key()
        self.dragging = False
        self.sel = None          # main-panel drag selection: {"start": (row, col), "end": (row, col), "live": bool}
        sys.stdout.flush()
        self.load(refresh=False)

    def side_keys(self):
        return self.MODES[self.mode]

    # ------------------------------------------------------------------ background work
    def on_progress(self, phase, done, total, detail=""):
        self.progress = {"phase": phase, "done": done, "total": total, "detail": detail}

    def run_bg(self, fn, note=""):
        """Start fn in a background thread (an AI job or a fetch); finished jobs are reaped in the main loop."""
        import threading

        def body():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.bg_error = str(e)
                self.progress = {"phase": "error", "done": 0, "total": None, "detail": str(e)}

        self.t0, self.bg_error = time.time(), None
        th = threading.Thread(target=body, daemon=True)
        th.note = note
        th.start()
        self.jobs.append(th)
        return th

    def busy(self):
        return any(t.is_alive() for t in self.jobs) or \
               (self.ask_thread is not None and self.ask_thread.is_alive()) or \
               (self.tr_thread is not None and self.tr_thread.is_alive())

    def ai_running(self):
        return sum(1 for t in self.jobs if t.is_alive() and getattr(t, "note", "") != "fetch")

    def progress_text(self):
        sp = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time() * 8) % 10]
        if self.ask_thread is not None and self.ask_thread.is_alive():
            el = int(time.time() - self.ask_state["t0"])
            return f"{sp} asking {model_label(ASK_MODEL)} about {self.ask_state['label']}  {el}s"
        if self.tr_thread is not None and self.tr_thread.is_alive():
            el = int(time.time() - self.tr_pending[1])
            return f"{sp} translating {self.tr_pending[0]} in full ({model_label(TR_MODEL)})  {el}s"
        p = self.progress or {}
        alive = [getattr(t, "note", "") for t in self.jobs if t.is_alive() and getattr(t, "note", "")]
        if len(alive) > 1 and (p.get("phase") in ("translate", "summarize")):
            el = int(time.time() - self.t0)
            return f"{sp} {len(alive)} AI jobs ({model_label(TR_MODEL)}): " + ", ".join(alive[:4]) + f"  {el}s"
        el = int(time.time() - self.t0)
        els = f"{el // 60}m{el % 60:02d}s" if el >= 60 else f"{el}s"
        names = {"fetch": "fetching issues/PRs", "stubs": "resolving referenced items",
                 "translate": f"translating ({model_label(TR_MODEL)})", "summarize": f"summarizing ({model_label(TR_MODEL)})",
                 "error": "error"}
        phase, done, total = p.get("phase", "starting"), p.get("done", 0), p.get("total")
        if total:
            f = int(12 * min(done, total) / total)
            bar = f" [{'#' * f}{'.' * (12 - f)}] {done}/{total}"
        elif phase == "fetch":
            bar = f" {done} items"
        else:
            bar = ""
        return f"{sp} {names.get(phase, phase)}{bar} {p.get('detail', '')} {els}"

    def draw_loading(self, what):
        scr = self.scr
        scr.erase()
        self._size = None
        h, w = scr.getmaxyx()
        lines = [f"gg — {what}", "", self.progress_text(), "", "q = quit"]
        for i, t in enumerate(lines):
            self.put(max(h // 2 - 2 + i, 0), max((w - dw(t)) // 2, 0), t, self.curses.A_BOLD if i == 0 else 0)
        scr.refresh()

    def load(self, refresh):
        """Fill the screen as soon as there is something to show: on a cold cache the items arrive in
        batches, so the first batch is drawn as a graph of its own (references to what has not arrived
        yet stay unresolved) and the complete graph is swapped in by the main loop when the fetch ends.
        A warm cache skips all of that — build_graph returns without a single batch."""
        import threading
        self.g, self.enriched, self.partial = None, set(), False
        self._full_g, self._partial_items = None, None
        lock, collected = threading.Lock(), []

        def on_batch(items):
            with lock:
                collected.extend(items)
                self._partial_items = list(collected)

        def work():
            g = build_graph(self.o["repos"], self.o["state"], self.o["max_age_min"], refresh,
                            on_batch=on_batch)
            self._full_g = g
            if self.partial:                   # a skeleton is on screen: the main loop swaps this in
                self._refresh_note = "loaded"
                self._new_g = g

        th = self.run_bg(work, "fetch")
        self.scr.timeout(100)
        while th.is_alive() and self._full_g is None:
            if self._partial_items:
                self.g = assemble_graph(self.o["repos"][0], self._partial_items, resolve=False)
                self.partial = True
                break
            self.draw_loading("fetching from GitHub" if refresh else "loading")
            if self.scr.getch() == ord("q"):
                raise SystemExit
        self.scr.timeout(-1)
        self.scr.clear()
        if self._full_g is not None:           # it finished while we were about to draw a skeleton
            self.g, self.partial, self._new_g = self._full_g, False, None
        if self.g is None:
            raise SystemExit(f"gg: cannot load the graph: {self.bg_error}")
        self.rebuild_graph()
        if self.o.get("review"):
            repo, number = self.o["review"]
            self.mode, self.focus = "review", "rfiles"
            self.mode_focus["browse"] = "home"
            self.load_review(repo, number)
        if self.o.get("root") and self.item is None:
            try:
                self.item = resolve_root(self.g, self.o["root"])
                self.subject = self.item
                self.focus = "main"
                self.panels["main"].tab = 0
            except ValueError as e:
                self.msg = str(e)
        self.refresh_all()
        if self.item is None and self.mode == "browse":
            self.focus = "home"
        if not CONFIG.get("tutorial_done") and self.o.get("tutorial", True):
            if self.popup_menu("first run — take a 2-minute tour of the screen? (F2 later)", [("yes", True), ("no", False)]) is True:
                self.tutorial()
            else:
                CONFIG["tutorial_done"] = True
                save_config()
        if self.o.get("start_tour"):
            self.tutorial()
        if not refresh and self._partial_items is None:
            self.refresh_bg()      # the cache answered: still ask GitHub what changed (only that is fetched)

    def refresh_bg(self, full=False):
        """r: fetch what changed on GitHub in the background and swap the graph in when done (R: everything)."""
        if any(t.is_alive() and getattr(t, "note", "") == "fetch" for t in self.jobs):
            self.msg = "a refresh is already running"
            return
        repos, state, max_age = self.o["repos"], self.o["state"], self.o["max_age_min"]

        def work():
            changed = 0
            for repo in repos:
                if full or state != "open":
                    load_items(repo, state, 0, refresh=True)
                else:
                    p = _cache_path("items", repo, state)
                    d = read_json(p)
                    if d is None:
                        load_items(repo, state, 0, refresh=True)
                        continue
                    items, n_changed, n_dropped = refresh_items(repo, d["items"])
                    write_json(p, {"fetched_at": time.time(), "repo": repo, "state": state, "items": items})
                    changed += n_changed + n_dropped
            self._new_g = build_graph(repos, state, max_age)
            self._refresh_note = "full refetch done" if full else f"refreshed: {changed} changed"

        self._new_g = None
        self.progress = {"phase": "fetch", "done": 0, "total": None, "detail": "checking GitHub for changes"}
        self.run_bg(work, "fetch")
        self.last_refresh = time.time()

    def rebuild_graph(self):
        self.cg = apply_filters(self.g, self.o["comments"], self.o["people"], self.o["closed_neighbors"])

    # ------------------------------------------------------------------ rows for each panel
    def label_w(self):
        """Title/excerpt width for side-panel rows: what fits after date, number, tags and author."""
        w = self.panels["home"].rect[3] or int(self.scr.getmaxyx()[1] * self.side_width) - 2
        return max(24, w - 24)

    def refresh_all(self, keep=True):
        self.o["width"] = self.label_w()
        self.panels["repo"].rows = self.repo_rows()
        self.panels["home"].set_rows(self.home_rows(), keep)
        self.panels["links"].set_rows(self.links_rows(), keep)
        self.panels["item"].rows = self.item_rows()
        self.panels["comments"].set_rows(self.comments_rows(), keep)
        self.panels["people"].set_rows(self.people_rows(), keep)
        self.refresh_main(keep)
        if self.rv is not None:
            self.refresh_review(keep)
        if self.subject is None or self.subject not in self.g.nodes:
            self.subject = self.item
        self.enrich()

    # ------------------------------------------------------------------ review mode
    def refresh_review(self, keep=True):
        rv = self.rv
        if rv is None:
            return
        if self.rv_path is None or rv.file(self.rv_path) is None:
            self.rv_path = rv.files[0].path if rv.files else None
        fw = max(self.panels["rfiles"].rect[3], 20)
        dwid = max(self.panels["rdiff"].rect[3], 40)
        vw = max(self.panels["rfind"].rect[3], 24)
        self.panels["rfiles"].set_rows(review_files_rows(rv, fw, self.rv_path), keep)
        self.panels["rdiff"].set_rows(diff_rows(rv, self.rv_path, dwid, self.rv_folded), keep)
        self.panels["rfind"].set_rows(
            findings_rows(rv, FIND_TABS[self.panels["rfind"].tab][0], vw, self.rv_checked), keep)
        self.panels["rdiff"].title = "Diff  " + short_path(self.rv_path or "-", max(dwid - 12, 8))

    def review_subject(self):
        """The PR the cursor is on, following a comment up to its item."""
        nid = self.subject or self.item
        n = self.g.nodes.get(nid) if nid else None
        if n is not None and n.kind == "comment":
            n = self.g.nodes.get(n.parent)
        return n if (n is not None and n.kind == "item" and n.is_pr) else None

    def toggle_review(self):
        """v: into review mode on the current PR, or back out of it."""
        if self.mode == "review":
            self.mode_focus["review"] = self.focus
            self.mode, self.focus = "browse", self.mode_focus.get("browse", "home")
            return
        n = self.review_subject()
        if n is None:
            self.msg = "v opens the review of a pull request — put the cursor on one first"
            return
        if n.stub:
            self.msg = f"{self.g.label_num(n)} was only referenced here, not fetched — open it first"
            return
        self.mode_focus["browse"] = self.focus
        self.mode, self.focus = "review", self.mode_focus.get("review", "rfiles")
        if self.rv is not None and (self.rv.repo, self.rv.number) == (n.repo, n.number):
            self.refresh_review()
        else:
            self.load_review(n.repo, n.number)

    def load_review(self, repo, number, refresh=False):
        """The worktree, the diff and whatever findings are cached, in the background."""
        node = self.g.nodes.get(f"{repo}#{number}")
        self.rv = Review(repo, number, status="worktree", title=(node.title if node else ""),
                         state=(node.state if node else None), author=(node.author if node else None))
        self.rv_path, self.rv_folded, self._told_review = None, set(), True
        self.refresh_review(keep=False)

        def job():
            try:
                self._new_rv = review_load(repo, int(number), refresh=refresh)
            except (GhError, ValueError, OSError, subprocess.SubprocessError) as e:
                failed = self.rv                        # no metadata: keep the placeholder, add the reason
                failed.status, failed.error = "failed", str(e)
                self._new_rv = failed

        self.run_bg(job, "review")

    def review_enter(self, r):
        if self.focus == "rfiles" and (r.nid or "").startswith("file:"):
            self.rv_path = r.nid[5:]
            self.panels["rdiff"].cur = self.panels["rdiff"].top = 0
            self.refresh_review(keep=False)
            self.focus = "rdiff"
        elif self.focus == "rfind" and r.jump and r.jump.startswith("line:"):
            path, _, _ = r.jump[5:].rsplit(":", 2)
            if path != self.rv_path:
                self.rv_path = path
                self.refresh_review(keep=False)
            if not self.panels["rdiff"].goto_nid(r.jump):
                self.msg = "that line is not in this diff any more"
            self.focus = "rdiff"
        elif self.focus == "rfind" and r.jump and r.jump.startswith("file:"):
            self.rv_path = r.jump[5:]
            self.refresh_review(keep=False)
            self.focus = "rdiff"
        elif self.focus == "rdiff" and (r.nid or "").startswith("hunk:"):
            self.rv_folded ^= {r.nid}
            self.refresh_review()

    def run_review_bg(self):
        """R: the AI review of this PR, in the background. It costs real money and minutes, so it is
        never started on its own — the cache is used until you ask for a new one."""
        rv = self.rv
        if rv is None or rv.status in ("running", "verifying", "worktree"):
            return
        if not rv.files:
            self.msg = rv.error or "nothing to review yet"
            return
        if not ai_available():
            self.msg = f"{CLAUDE_BIN} is not installed — gg ai picks another AI CLI"
            return
        n = len(review_chunks(rv))
        if not self.confirm(f"Review {rv.repo}#{rv.number} with {CLAUDE_BIN} {REVIEW_MODEL}? "
                            f"({len(rv.files)} files, {n} call{'s' if n != 1 else ''})"):
            return
        rv.status, rv.error, self._told_review = "running", None, False
        self.refresh_review(keep=False)
        self.run_bg(lambda: run_review(rv, rv.body), "review")

    def review_finding(self):
        r = self.panels["rfind"].current()
        fid = r.nid[8:] if r and (r.nid or "").startswith("finding:") else None
        return next((f for f in (self.rv.findings if self.rv else []) if f.fid == fid), None)

    def review_key(self, k):
        """Review-mode keys. None means 'not mine' — the shared keys below still run."""
        if k in (ord("v"), 27):
            self.toggle_review()
            return True
        if self.rv is None:
            return None
        if k == ord("r"):
            self.load_review(self.rv.repo, self.rv.number)
            return True
        if k == ord("R"):
            self.run_review_bg()
            return True
        if k in (ord("d"), ord("i")) and self.focus == "rfind":
            self.review_details(translate=k == ord("i"))
            return True
        if k == ord("V") and self.focus == "rfind":
            f = self.review_finding()
            if f is None:
                self.msg = "no finding under the cursor"
                return True
            f.verdict, f.verdict_reason = None, None
            self.msg = f"checking again: {trunc(f.title, 50)}"
            self.refresh_review(keep=False)
            self.run_bg(lambda: run_verify(self.rv, [f]), "review")
            return True
        if k == ord("x") and self.focus == "rfind":
            f = self.review_finding()
            if f is None:
                self.msg = "no finding under the cursor"
                return True
            f.state = "new" if f.state == "ignored" else "ignored"
            save_review(self.rv)
            self.msg = f"{'ignored' if f.state == 'ignored' else 'restored'}: {trunc(f.title, 50)}"
            self.refresh_review(keep=False)
            return True
        if k == ord("o"):
            url = self.review_url()
            if url:
                self.open_url(url)
            return True
        if k == ord("y"):
            url = self.review_url()
            if url:
                self.msg = f"copied {url} ({', '.join(copy_to_clipboard(url)) or 'no clipboard tool'})"
            return True
        return None

    def review_details(self, translate=False):
        """d / i on a finding: the whole thing — the panel only has room for its title.
        i shows it in `lang`; what would be posted is always the original (see P)."""
        f = self.review_finding()
        if f is None:
            self.msg = "no finding under the cursor"
            return
        title, body = f.title, f.body
        if translate:
            if not ai_available():
                self.msg = f"{CLAUDE_BIN} is not installed — cannot translate"
                return
            key = (f.fid, "tr")
            if key not in self.rv_tr:
                self.msg = f"translating to {TR_LANG}…"
                self.draw()
                try:
                    self.rv_tr[key] = translate_text(f"{f.title}\n\n{f.body}", "pull request")
                except Exception as e:  # noqa: BLE001
                    self.msg = f"translation failed: {str(e)[:120]}"
                    return
            title, _, body = self.rv_tr[key].partition("\n\n")
        w = max(self.panels["rfind"].rect[3] * 2, 60)
        lines = [f"[{f.severity}] {title}", ""]
        lines += wrap(body, w) or [""]
        if f.verdict:
            lines += ["", f"verdict: {f.verdict}"] + wrap(f.verdict_reason or "", w)
        lines += ["", f"{f.path}:{f.line} ({f.side})"
                      + {"moved": "  ⚠ moved onto the nearest changed line",
                         "unanchored": "  ⊘ not on a line of this diff — cannot be posted"}.get(f.anchor, "")]
        if f.evidence:
            lines += ["", "evidence:"] + wrap(f.evidence, w)
        if f.diff:
            lines += ["", "suggested fix:"] + f.diff.splitlines()
        if translate:
            lines += ["", f"(translated; d shows the original — what P would post)"]
        self.popup_text(f"finding {f.fid}" + (f" — {TR_LANG}" if translate else ""), lines)

    def review_url(self):
        """The PR, or the file and line the cursor is on inside it."""
        rv = self.rv
        if rv is None or not rv.url:
            return None
        r = self.panels[self.focus].current()
        nid = (r.nid or "") if r else ""
        if self.focus == "rdiff" and nid.startswith("line:"):
            path, side, no = nid[5:].rsplit(":", 2)
            frag = f"R{no}" if side == "RIGHT" else f"L{no}"
            return f"{rv.url}/files#diff-{hashlib.sha256(path.encode()).hexdigest()}{frag}"
        return rv.url

    def repo_rows(self):
        g = self.g
        n_items = sum(1 for n in g.nodes.values() if n.kind == "item" and not n.stub)
        fa = datetime.fromtimestamp(g.fetched_at).strftime("%H:%M") if g.fetched_at else "?"
        me = ",".join("@" + m for m in self.me) or "-"
        loading = "  ⋯ still loading" if getattr(self, "partial", False) else ""
        return [Row(f"{g.primary}  {n_items} open{loading}  {fa}  me={me}", kind="head"),
                Row(f"{THEME} c={self.o['comments']} t={self.o['translate']} s={'on' if self.o['summary'] else 'off'} "
                    f"h={self.o['hops']} | {usage_line().replace('tokens ', '')}", kind="head")]

    def item_rows(self):
        """The current item: label, metadata, one-line summary, url — Enter shows it in main."""
        g, w = self.g, self.o["width"]
        n = g.nodes.get(self.item)
        if not n:
            return [Row("(no current item — Enter on a row in Inbox)", kind="head")]
        n_links = sum(1 for r in self.panels["links"].rows if r.kind == "")
        n_qa = len(load_qa().get(n.id, []))
        meta = [f"updated {short_date(n.updated)}" if n.updated else "", ", ".join(n.labels),
                f"{n.comments_total} comments" if n.comments_total else "", f"{n_links} links" if n_links else "",
                f"{n_qa} Q&A" if n_qa else ""]
        summ = ("» " + n.summary) if n.summary else ("» " + PENDING_TEXT if n.summary_pending else
                                                     (excerpt(n.body, 200) if (n.body or "").strip() else "(no body)"))
        return [Row(self.mark_prefix(n.id) + item_label(g, n, w), n.id),
                Row("  " + " · ".join(x for x in meta if x), n.id, kind="note"),
                Row("  " + summ, n.id, kind="note"),
                Row("  " + (n.url or ""), n.id, kind="url")]

    def home_sections(self):
        """{key: [Row]} for every home section (same rules as the old single-screen home)."""
        g, cg, w = self.g, self.cg, self.o["width"]
        days = self.o.get("days", 7)
        now = time.time()
        me = set(self.me)
        items = [n for n in g.nodes.values() if n.kind == "item" and not n.stub]

        def last_comment(n):
            cs = g.comments_of(n.id)
            return cs[-1] if cs else None

        def mentions_me(n):
            for src in [n] + g.comments_of(n.id):
                for m, t, o in g.adj[src.id]:
                    if t == "mention" and o and m[1:].lower() in me:
                        return src
            return None

        def involved(n):
            return (n.author or "").lower() in me or any((c.author or "").lower() in me for c in g.comments_of(n.id)) \
                or mentions_me(n) is not None

        def my_turn_reason(n, lc):
            """Why this item is on me, compactly: what the last comment did relative to me."""
            who, when = f"@{lc.author}", rel_days(lc, g)
            if any(t == "mention" and o and m[1:].lower() in me for m, t, o in g.adj[lc.id]):
                return f"{who} mentioned {when}"
            if (n.author or "").lower() in me:
                return f"{who} on my {'PR' if n.is_pr else 'issue'} {when}"
            if any((c.author or "").lower() in me for c in g.comments_of(n.id)):
                return f"{who} replied {when}"
            return f"{who} commented {when}"

        def item_row(n, src=None, reason=None):
            deg = item_degree(cg, n.id) if n.id in cg.nodes else 0
            if reason:
                tw = max(12, w - dw(reason) - 4)      # the title keeps what the reason leaves
                label = item_label(g, n, tw, with_meta=False) + f"  ⟵ {reason}"
            else:
                label = item_label(g, n, w)
            r = Row(self.mark_prefix(n.id) + label + (f"  ⇢ {deg}" if deg else ""), n.id)
            if src is not None and src.kind == "comment":
                what = (("» " + trunc(src.summary, w)) if src.summary else
                        ("\"" + (trunc(src.tr_excerpt, w) if src.tr_excerpt else excerpt(src.body, w)) + "\""))
                r.text += f"  ← @{src.author} {rel_days(src, g)} {short_date(src.created)[5:]} {what}"
                r.kind, r.jump = "mention", src.id
            return r

        newest = lambda ns: sorted(ns, key=lambda n: -n.time)  # noqa: E731
        by_update = lambda ns: sorted(ns, key=lambda n: -ts(n.updated))  # noqa: E731
        opened = newest(n for n in items if n.time >= now - days * 86400)
        opened_ids = {n.id for n in opened}
        active = by_update(n for n in items if ts(n.updated) >= now - days * 86400 and n.id not in opened_ids)
        my_turn, waiting = [], []
        for n in items:
            if not involved(n):
                continue
            lc = last_comment(n)
            if lc is not None and (lc.author or "").lower() not in me:
                my_turn.append((n, lc))
            elif lc is not None or (n.author or "").lower() in me:
                waiting.append((n, lc))
        my_turn.sort(key=lambda x: -x[1].time)
        waiting.sort(key=lambda x: -(x[1].time if x[1] else x[0].time))
        mentioned = sorted(((n, mentions_me(n)) for n in items if mentions_me(n)), key=lambda x: -x[1].time)
        return {
            "turn": [item_row(n, lc, my_turn_reason(n, lc)) for n, lc in my_turn],
            "mention": [item_row(n, src) for n, src in mentioned],
            "opened": [item_row(n) for n in opened],
            "active": [item_row(n, last_comment(n)) for n in active],
            "waiting": [item_row(n, lc) for n, lc in waiting],
            "mine": [item_row(n) for n in newest(n for n in items if (n.author or "").lower() in me)],
            "prs": [item_row(n, last_comment(n)) for n in newest(n for n in items if n.is_pr and (n.author or "").lower() not in me)],
            "stale": [item_row(n) for n in by_update(n for n in items if ts(n.updated) < now - 30 * 86400)],
            "all": [item_row(n) for n in newest(items)],
        }

    def marked(self, nid):
        return next((e for e in self.todo if not e.get("done") and (e.get("comment") == nid or (not e.get("comment") and e["item"] == nid))), None)

    def mark_prefix(self, nid):
        return "✎ " if self.marked(nid) else ""

    def todo_rows(self):
        g, w = self.g, self.o["width"]
        rows = []
        for e in sorted(self.todo, key=lambda e: e["created"], reverse=True):
            if e.get("done"):
                continue
            n = g.nodes.get(e["item"])
            head = item_label(g, n, w, with_meta=False) if n else f"{e['item_num']} {trunc(e['title'], w)}"
            text = f"✎ {head}"
            if e.get("comment"):
                text += f"  ← @{e['comment_author']} {e['comment_when']}"
            if e.get("note"):
                text += f"  · {trunc(e['note'].splitlines()[0], w)}"
            r = Row(text, e["item"] if n else None, e.get("comment") if e.get("comment") in g.nodes else None,
                    "mention" if e.get("comment") in g.nodes else "", mark=e["id"])
            rows.append(r)
        return rows or [Row("(nothing marked — press m on an item or a comment)", kind="head")]

    def home_rows(self):
        if self.HOME_TABS[self.panels["home"].tab][0] == "todo":
            self.home_counts = getattr(self, "home_counts", {})
            self.home_counts["todo"] = sum(1 for e in self.todo if not e.get("done"))
            return self.todo_rows()
        secs = self.home_sections()
        self.home_counts = {k: len(v) for k, v in secs.items()}
        self.home_counts["todo"] = sum(1 for e in self.todo if not e.get("done"))
        key = self.HOME_TABS[self.panels["home"].tab][0]
        return secs[key] or [Row("(nothing here)", kind="head")]

    def links_rows(self):
        if not self.item:
            return [Row("(no current item)", kind="head")]
        g, w = self.g, self.o["width"]
        rows, seen = [], set()
        order = {"closes": 0, "ref": 1, "mention": 3, "comment": 4}
        srcs = [g.nodes[self.item]] + g.comments_of(self.item)
        edges = []
        for src in srcs:
            for m, t, o in g.adj[src.id]:
                if t == "comment" or g.nodes[m].kind == "person" or m == self.item:
                    continue
                if (m, t, o) in seen:
                    continue
                seen.add((m, t, o))
                edges.append((order[t], not o, -g.nodes[m].time, src, m, t, o))   # newest first within a type
        self.link_pairs = []
        for _, _, _, src, m, t, o in sorted(edges):
            n = g.nodes[m]
            via = "" if src.kind == "item" else f" (via {rel_days(src, g)} comment)"
            rows.append(Row(EDGE_LABEL[(t, o)] + node_label(g, n, w) + via, m))
            note_w = max(20, (self.panels["links"].rect[3] or 40) - 5)
            for i, line in enumerate(wrap(self.link_note(src, n, t, o), note_w)[:2]):
                rows.append(Row(("   ↳ " if i == 0 else "     ") + line, m, kind="note"))
            if t != "closes":
                self.link_pairs.append(self.link_pair(src, n, o))
        return rows or [Row("(no links)", kind="head")]

    def link_pair(self, src, n, o):
        """(src, dst) key of the sentence behind a link row: outgoing = src references n, incoming = n references us."""
        return (src.id, n.id) if o else (n.id, self.item)

    def link_note(self, src, n, t, o):
        """Why this link exists, briefly: claude's one-line reason, else a short quote around the reference,
        else a one-line summary of the other item."""
        g = self.g
        if t == "closes":
            return "PR closes the issue" if o else "closed by this PR"
        pair = self.link_pair(src, n, o)
        ctx = g.ctx.get(pair)
        if ctx:
            why = g.why.get(pair)
            if why and not weak_why(why):
                return why
            ref = g.label_num(g.nodes[pair[1]]) if pair[1] in g.nodes else ""
            return f"\"{around(ctx, ref)}\"" + ("  (» …)" if self.o["summary"] and pair in getattr(self, "why_pending", set()) else "")
        if n.kind == "comment":
            return "» " + n.summary if n.summary else ("» " + PENDING_TEXT if n.summary_pending else excerpt(n.body, 120))
        if n.summary:
            return "» " + n.summary
        if n.summary_pending:
            return "» " + PENDING_TEXT
        return "(referenced via GitHub's timeline; no text available)" if not (n.body or "").strip() else excerpt(n.body, 120)

    def comments_rows(self):
        if not self.item:
            return [Row("(no current item)", kind="head")]
        g, w = self.g, self.o["width"]
        cs = list(reversed(g.comments_of(self.item)))     # newest on top
        rows = [Row(self.mark_prefix(c.id) + comment_label(g, c, w, show_item=False), c.id) for c in cs]
        return rows or [Row("(no comments)", kind="head")]

    def people_rows(self):
        if not self.item:
            return [Row("(no current item)", kind="head")]
        g = self.g
        n = g.nodes[self.item]
        roles = {}

        def add(login, role):
            if not login:
                return
            roles.setdefault(login, [])
            if role not in roles[login]:
                roles[login].append(role)

        add(n.author, "author")
        for c in g.comments_of(self.item):
            add(c.author, "commented")
        for src in [n] + g.comments_of(self.item):
            for m, t, o in g.adj[src.id]:
                if t == "mention" and o:
                    add(m[1:], "mentioned")
        last = {}
        for c in g.comments_of(self.item):
            last[c.author] = max(last.get(c.author, 0), c.time)
        rows = []
        for login, rs in sorted(roles.items(), key=lambda kv: -last.get(kv[0], n.time if kv[0] == n.author else 0)):
            pid = next((k for k in g.nodes if k.lower() == "@" + login.lower()), None)
            rows.append(Row(f"@{login}  {', '.join(rs)}", pid or f"@{login}"))
        return rows

    def refresh_main(self, keep=True):
        p = self.panels["main"]
        tab = self.MAIN_TABS[p.tab]
        if tab == "content":
            lines = self.content_lines(self.subject, max(p.rect[3], 40))
        else:
            lines = render_markdown(self.answer_text(), max(p.rect[3], 40))
        p.scroll_only = True
        rows, in_code, prev_table = [], False, False
        for t in lines:
            if re.match(r"https?://\S+$", t):
                rows.append(Row(t, kind="url"))
                prev_table = False
                continue
            if t.startswith("⟳ "):
                rows.append(Row(t, kind="head"))
                prev_table = False
                continue
            if not in_code and t.startswith(("│", "├")):
                r = Row(t, kind="md_table")
                if not prev_table:
                    r.jump = "head"          # first row of a table: the header
                rows.append(r)
                prev_table = True
                continue
            prev_table = False
            fence = t.lstrip().startswith("```")
            rows.append(Row(t, kind="md_code" if (in_code or fence) else "md"))
            if fence:
                in_code = not in_code
        p.rows = rows

    @staticmethod
    def reflow(body):
        """Join hard-wrapped prose lines into paragraphs; code fences, lists, tables, quotes, headings stay as they are."""
        out, para, in_code = [], [], False

        def flush():
            if para:
                out.append(" ".join(x.strip() for x in para))
                para.clear()

        for ln in body.splitlines():
            st = ln.strip()
            if st.startswith("```"):
                flush(); out.append(ln); in_code = not in_code; continue
            special = in_code or not st or st.startswith(("#", "|", ">", "- ", "* ", "+ ", "```")) or \
                re.match(r"^\d+[.)] ", st) or ln.startswith(("    ", "\t"))
            if special:
                flush(); out.append(ln)
            else:
                para.append(ln)
        flush()
        return "\n".join(out)

    def answer_text(self):
        """The answer tab: the running/last answer if it is about the current subject, then earlier Q&A on it."""
        nid = self.subject or self.item
        parts = []
        if self.answer and self.answer_nid == nid:
            parts.append(self.answer)
        hist = load_qa().get(nid, []) if nid else []
        cur_q = self.ask_state["q"] if self.ask_state and self.answer_nid == nid else None
        earlier = [e for e in hist if not (cur_q and e["q"] == cur_q and parts)]
        if earlier:
            n = self.g.nodes.get(nid)
            label = (self.g.label_num(n) if n and n.kind == "item" else
                     f"comment on {self.g.label_num(self.g.nodes[n.parent])}" if n and n.kind == "comment" else nid)
            parts.append(f"## earlier questions on {label}")
            for e in reversed(earlier):
                parts.append(f"**{e['when']}  Q:** {e['q']}\n\n{e['a']}")
        if not parts:
            return "(no answer yet — press a to ask about the selection; answers stay anchored to it across sessions)"
        return "\n\n---\n\n".join(parts)

    def content_lines(self, nid, width):
        n = self.g.nodes.get(nid) if nid else None
        if not n:
            return wrap(nid.lstrip("@") if nid else "(nothing selected)", width)
        g = self.g
        out, body = [], ""
        if n.kind == "item":
            if n.url:
                out.append(n.url)
            out.append(f"{g.label_num(n)} {kind_tag(n)} {n.tr_title or n.title or '(unresolved)'}")
            if n.tr_title:
                out.append(f"original: {n.title}")
            meta = [f"@{n.author}" if n.author else "", short_date(n.created),
                    f"updated {short_date(n.updated)}" if n.updated else "", ", ".join(n.labels)]
            out.append("  ".join(x for x in meta if x))
            if n.summary:
                out.append(f"» {n.summary}")
            if n.stub:
                out.append("(not fetched: closed item or other repo — press o to open it)")
            body = n.body
        elif n.kind == "comment":
            head = f"{g.label_num(g.nodes[n.parent])} comment  @{n.author}  {short_date(n.created)} ({rel_days(n, g)})"
            if n.ckind == "review":
                head += f"  [{(n.review_state or 'review').lower()}]"
            elif n.ckind == "review_comment":
                head += "  [inline review comment]"
            if n.url:
                out.append(n.url)
            out.append(head)
            if n.summary:
                out.append(f"» {n.summary}")
            body = n.body
        else:
            out.append(f"{n.id}  mentioned {n.mention_count} times:")
            for m, t, o in sorted(g.adj[nid], key=lambda e: -g.nodes[e[0]].time):
                if t == "mention":
                    out.append("  " + node_label(g, g.nodes[m], 120))
        if self.show_tr and n.kind in ("item", "comment"):
            if n.tr_body:
                out.append(f"(translated to {TR_LANG} — i shows the original)")
                body = n.tr_body
            elif self.tr_thread is not None and self.tr_thread.is_alive() and self.tr_pending and self.tr_pending[2] == nid:
                el = int(time.time() - self.tr_pending[1])
                ko = TR_LANG.lower().startswith("korean")
                out.append((f"⟳ 번역 중… {el}s ({model_label(TR_MODEL)}) — 아래는 원문, 끝나면 바뀝니다" if ko else
                            f"⟳ translating… {el}s ({model_label(TR_MODEL)}) — original below until it is done"))
        if body.strip():
            out.append("")
            out.extend(render_markdown(self.reflow(body), width))
        return out

    def start_translation(self, n, nid):
        import threading
        label = self.g.label_num(n) if n.kind == "item" else f"comment on {self.g.label_num(self.g.nodes[n.parent])}"
        self.tr_pending = (label, time.time(), nid)
        self.tr_failed = getattr(self, "tr_failed", set())

        def work():
            try:
                n.tr_body = translate_body(n, self.g) or None
                if not n.tr_body:
                    self.tr_failed.add(nid)
            except Exception as e:  # noqa: BLE001
                self.tr_failed.add(nid)              # no automatic retry; i tries again by hand
                self.msg = f"translation failed: {e}"

        self.tr_thread = threading.Thread(target=work, daemon=True)
        self.tr_thread.start()
        self.refresh_main()

    def translate_content(self):
        """i: toggle between the original and a full translation of the main content (translated on demand)."""
        nid = self.subject or self.item
        n = self.g.nodes.get(nid) if nid else None
        if not n or n.kind not in ("item", "comment") or not (n.body or "").strip():
            self.msg = "nothing to translate here"
            return
        if self.show_tr:
            self.show_tr = False
            self.msg = "showing the original (i = translation again)"
            self.refresh_main()
            return
        self.show_tr = True
        self.panels["main"].tab = 0
        if n.tr_body:
            self.refresh_main()
            return
        if self.tr_thread is not None and self.tr_thread.is_alive():
            self.msg = "a translation is still running"
            return
        getattr(self, "tr_failed", set()).discard(nid)
        self.start_translation(n, nid)

    # ------------------------------------------------------------------ selection / history
    def snapshot(self):
        return (self.item, self.subject, list(self.me), self.panels["main"].tab, self.panels["home"].tab, set(self.collapsed))

    def restore(self, st):
        self.item, self.subject, self.me, self.panels["main"].tab, self.panels["home"].tab, self.collapsed = st
        self.refresh_all()
        self.sync_cursors()

    def sync_cursors(self):
        """Put every side list's cursor on the current item, so the highlight matches what is on screen.
        refresh_all() keeps each panel's own cursor, which is right while you browse but wrong after
        b / f or a jump: the row you came from would stay highlighted. The Comments panel follows the
        selected comment instead, when the selection is one of this item's comments (the snapshot's
        subject can be another item entirely — that is what main was previewing, not what is current)."""
        if not self.item:
            return
        n = self.g.nodes.get(self.subject) if self.subject else None
        comment = self.subject if n is not None and n.kind == "comment" and n.parent == self.item else None
        for key in ("home", "links", "item", "comments"):
            p = self.panels.get(key)
            if not p or not p.rows:
                continue
            if key == "comments" and comment and p.goto_nid(comment):
                continue
            p.goto_nid(self.item)

    def set_item(self, nid, push=True):
        if not nid or nid not in self.g.nodes or self.g.nodes[nid].kind == "person":
            return
        if push:
            self.hist.append(self.snapshot())
            self.fwd = []
        self.item, self.subject, self.collapsed = nid, nid, set()
        self.panels["main"].top = 0
        self.refresh_all()
        self.sync_cursors()

    def back(self):
        if not self.hist:
            self.msg = "nothing to go back to"
            return
        self.fwd.append(self.snapshot())
        self.restore(self.hist.pop())

    def forward(self):
        if not self.fwd:
            self.msg = "nothing to go forward to"
            return
        self.hist.append(self.snapshot())
        self.restore(self.fwd.pop())

    def view_as(self, me):
        self.hist.append(self.snapshot())
        self.fwd = []
        self.me = [m.lower() for m in me]
        self.msg = "viewing as " + (", ".join("@" + m for m in self.me) or "(nobody)")
        self.refresh_all(keep=False)
        self.focus = "home"

    def update_subject(self):
        """The main content follows the row under the cursor of the focused side panel; it stays when main is focused."""
        if self.focus in ("item", "home", "links", "comments", "people"):
            r = self.panels[self.focus].current()
            nid = (r.jump if r and r.kind == "mention" and r.jump else (r.nid if r else None))
            if nid and nid != self.subject:
                self.subject = nid
                self.panels["main"].top = 0
                self.refresh_main()

    # ------------------------------------------------------------------ tree folding (main tree tab)
    def fold_below(self, depth):
        p = self.panels["main"]
        if self.MAIN_TABS[p.tab] != "tree" or p.scroll_only:
            return
        keep = p.current().nid if p.current() else None
        self.collapsed.clear()
        self.refresh_main(keep=False)
        self.collapsed = {r.nid for i, r in enumerate(p.rows) if r.kind == "" and r.nid
                          and self.row_depth(p, i) >= depth and self.has_kids(p, i)}
        self.refresh_main(keep=False)
        if keep:
            p.goto_nid(keep)

    @staticmethod
    def row_indent(p, i):
        m = re.search(r"[├└]─ ", p.rows[i].text)
        return m.start() if m else 0

    def row_depth(self, p, i):
        return self.row_indent(p, i) // 3 + 1 if re.search(r"[├└]─ ", p.rows[i].text) else 0

    def has_kids(self, p, i):
        return i + 1 < len(p.rows) and p.rows[i + 1].kind != "head" and self.row_indent(p, i + 1) > self.row_indent(p, i)

    def toggle_fold(self, want=None):
        p = self.panels["main"]
        r = p.current()
        if self.MAIN_TABS[p.tab] != "tree" or not r or r.kind != "" or not r.nid:
            return
        folded = r.nid in self.collapsed
        if want is None:
            want = not folded
        if want and not folded and self.has_kids(p, p.cur):
            self.collapsed.add(r.nid)
        elif not want and folded:
            self.collapsed.discard(r.nid)
        else:
            return
        self.refresh_main()

    # ------------------------------------------------------------------ enrichment (visible rows first)
    def enrich(self):
        """On demand, in parallel: translate / summarize the nodes in the current rows (on-screen first), a few
        small AI jobs at a time (AI_PARALLEL)."""
        want_tr, want_sum = self.o["translate"] != "none", self.o["summary"]
        if not (want_tr or want_sum):
            return
        free = AI_PARALLEL - self.ai_running()
        if free <= 0:
            return
        ids = []

        def add(nid):
            if nid and nid in self.g.nodes and nid not in self.enriched and nid not in ids:
                ids.append(nid)

        add(self.subject)
        for key in [self.focus] + [k for k in self.visible if k != self.focus]:
            p = self.panels.get(key)
            if not p:
                continue
            h = max(p.rect[2], 1)
            for r in p.rows[p.top:p.top + h]:
                add(r.nid)
                if r.kind == "mention":
                    add(r.jump)
            for r in p.rows:
                add(r.nid)
        whys = [pr for pr in getattr(self, "link_pairs", []) if pr in self.g.ctx and pr not in self.g.why
                and pr not in self.enriched] if want_sum else []
        chunk = max(1, ENRICH_BATCH // 2)
        started = False
        for _ in range(free):
            batch = ids[:chunk]
            ids = ids[chunk:]
            wb = whys[:chunk] if not started else []
            whys = whys[chunk:] if not started else whys
            if not batch and not wb:
                break
            self.start_enrich_job(set(batch), wb, want_sum)
            started = True
        if started:
            self.refresh_all_rows()

    def start_enrich_job(self, ids, whys, want_sum):
        parents = {self.g.nodes[i].parent for i in ids if self.g.nodes[i].kind == "comment"} - {None}
        sub = subgraph(self.g, ids | parents)
        mode = self.o["translate"]
        pending = [self.g.nodes[i] for i in ids if self.g.nodes[i].kind == "comment" and want_sum
                   and not self.g.nodes[i].summary and self.g.nodes[i].body.strip()]
        for n in pending:
            n.summary_pending = True
        self.why_pending = getattr(self, "why_pending", set()) | set(whys)

        def work():
            try:
                prepare_translations(sub, mode)
                if want_sum:
                    prepare_summaries(sub)
                if whys:
                    prepare_whys(self.g, whys)
            finally:
                for n in pending:
                    n.summary_pending = False
                self.why_pending -= set(whys)

        self.enriched |= ids | set(whys)
        note = f"{len(pending)} summaries" if pending else (f"{len(whys)} link reasons" if whys else f"{len(ids)} titles")
        self.run_bg(work, note)

    def refresh_all_rows(self):
        """Rebuild the rows without starting new jobs (pending markers, finished results)."""
        self.panels["repo"].rows = self.repo_rows()
        self.panels["home"].set_rows(self.home_rows())
        self.panels["links"].set_rows(self.links_rows())
        self.panels["item"].rows = self.item_rows()
        self.panels["comments"].set_rows(self.comments_rows())
        self.panels["people"].set_rows(self.people_rows())
        self.refresh_main()

    # ------------------------------------------------------------------ actions
    def enter(self):
        p = self.panels[self.focus]
        r = p.current()
        if not r:
            return
        if self.mode == "review":
            return self.review_enter(r)
        if self.focus == "home":
            self.set_item(r.nid)
            if r.kind == "mention" and r.jump in self.g.nodes:      # the comment that put it here / was marked
                self.subject = r.jump
                self.panels["comments"].goto_nid(r.jump)
                self.refresh_main()
        elif self.focus == "item":
            self.subject = self.item
            self.panels["main"].tab = 0
            self.panels["main"].top = 0
            self.refresh_main()
            self.focus = "main"
        elif self.focus == "links":
            n = self.g.nodes.get(r.nid)
            if n and n.kind == "comment":
                self.set_item(n.parent)
                self.panels["comments"].goto_nid(n.id)
                self.subject = n.id
                self.refresh_main()
                self.focus = "comments"
            else:
                self.set_item(r.nid)
                self.focus = "item"
        elif self.focus == "comments":
            self.subject = r.nid
            self.panels["main"].tab = 0
            self.panels["main"].top = 0
            self.refresh_main()
            self.focus = "main"
        elif self.focus == "people":
            self.view_as([r.nid.lstrip("@")])
        elif self.focus == "main":
            return

    def node_url(self, nid):
        n = self.g.nodes.get(nid)
        if not n:
            return f"https://{repo_host(self.g.primary)}/{nid.lstrip('@')}" if nid else None
        if n.kind == "person":
            return f"https://{repo_host(self.g.primary)}/{n.title}"
        if n.url:
            return n.url
        host, owner, name = split_repo(n.repo)
        return f"https://{host}/{owner}/{name}/issues/{n.number}"

    def open_url(self, url):
        try:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.msg = f"opened {url}"
        except OSError as e:
            self.msg = f"cannot open browser: {e}"

    def open_browser(self):
        url = self.node_url(self.subject or self.item)
        if url:
            self.open_url(url)

    def details(self):
        nid = self.subject or self.item
        if nid and nid in self.g.nodes:
            self.pager(render_show(self.g, nid, width=200).splitlines(), f"details: {nid}")

    # ------------------------------------------------------------------ popups (centred boxes over the screen)
    def popup_rect(self, want_h, want_w):
        h, w = self.scr.getmaxyx()
        bh, bw = min(want_h, h - 2), min(want_w, w - 2)
        return (h - bh) // 2, (w - bw) // 2, bh, bw

    def popup_frame(self, title, want_h, want_w, background=True):
        """A centred box over the screen (the screen itself is redrawn only when background=True);
        returns the content rect (y, x, h, w)."""
        if background:
            self.draw()
        c = self.curses
        by, bx, bh, bw = self.popup_rect(want_h, want_w)
        tl, tr, bl, br, hz, vt = BORDERS["rounded"] if self.border == BORDERS["hidden"] else self.border
        attr = self.style_attr("fold") | c.A_BOLD
        top = f"{tl}{hz}{clip(title, 0, bw - 4)}"
        self.put(by, bx, top + hz * max(0, bw - 1 - dw(top)) + tr, attr)
        for i in range(1, bh - 1):
            self.put(by + i, bx, vt + " " * (bw - 2) + vt, attr)
        self.put(by + bh - 1, bx, bl + hz * (bw - 2) + br, attr)
        return by + 1, bx + 1, bh - 2, bw - 2

    def popup_text(self, title, lines, hint="j/k PgUp/PgDn scroll  H L sideways  Esc/q/⏎ close"):
        c = self.curses
        top, hs, first = 0, 0, True
        while True:
            y, x, hh, ww = self.popup_frame(title, len(lines) + 3, max(dw(l) for l in lines + [hint]) + 2 if lines else 40, first)
            first = False
            body = hh - 1
            for i in range(body):
                if top + i >= len(lines):
                    break
                self.put(y + i, x, clip(lines[top + i], hs, ww))
            self.put(y + hh - 1, x, clip(f"{top + 1}-{min(top + body, len(lines))}/{len(lines)}  {hint}", 0, ww), self.dim())
            self.scr.refresh()
            self.scr.timeout(-1)
            k = self.read_key()
            if k in (ord("q"), 27, 10, 13, c.KEY_ENTER, ord("?"), ord("d"), ord("$")):
                return
            if k == c.KEY_MOUSE and self.mouse_ev:
                b = self.mouse_ev[0] & ~28
                if b == 64:
                    top = max(top - 3, 0)
                elif b == 65:
                    top = min(top + 3, max(len(lines) - body, 0))
                elif self.mouse_ev[3] and b == 0:
                    return
            elif k in (c.KEY_DOWN, ord("j")):
                top = min(top + 1, max(len(lines) - body, 0))
            elif k in (c.KEY_UP, ord("k")):
                top = max(top - 1, 0)
            elif k in (c.KEY_NPAGE, ord(" "), ord(".")):
                top = min(top + body, max(len(lines) - body, 0))
            elif k in (c.KEY_PPAGE, ord(",")):
                top = max(top - body, 0)
            elif k in (ord("g"), ord("<")):
                top = 0
            elif k in (ord("G"), ord(">")):
                top = max(len(lines) - body, 0)
            elif k == ord("L"):
                hs += 20
            elif k == ord("H"):
                hs = max(hs - 20, 0)

    def pager(self, lines, title):
        self.popup_text(title, lines)

    def popup_menu(self, title, items, cur=0):
        """items: [(label, value)]; returns the chosen value or None. j/k/digits/mouse, Enter picks, Esc cancels."""
        c = self.curses
        top, first = 0, True
        while True:
            width = max([dw(l) for l, _ in items] + [dw(title) + 4]) + 6
            y, x, hh, ww = self.popup_frame(title, len(items) + 2, width, first)
            first = False
            if cur < top:
                top = cur
            if cur >= top + hh:
                top = cur - hh + 1
            for i in range(hh):
                j = top + i
                if j >= len(items):
                    break
                label = f"{j + 1 if j < 9 else ' '} {items[j][0]}"
                self.put(y + i, x, clip(label, 0, ww), c.A_REVERSE if j == cur else 0, fill=ww)
            self.scr.refresh()
            self.scr.timeout(-1)
            k = self.read_key()
            if k in (27, ord("q")):
                return None
            if k in (10, 13, c.KEY_ENTER):
                return items[cur][1]
            if k in (c.KEY_DOWN, ord("j")):
                cur = min(cur + 1, len(items) - 1)
            elif k in (c.KEY_UP, ord("k")):
                cur = max(cur - 1, 0)
            elif k in (c.KEY_NPAGE, ord(".")):
                cur = min(cur + hh, len(items) - 1)
            elif k in (c.KEY_PPAGE, ord(",")):
                cur = max(cur - hh, 0)
            elif ord("1") <= k <= ord("9") and k - ord("1") < len(items):
                return items[k - ord("1")][1]
            elif k == c.KEY_MOUSE and self.mouse_ev:
                b, mx, my, press = self.mouse_ev
                b &= ~28
                if b == 64:
                    cur = max(cur - 1, 0)
                elif b == 65:
                    cur = min(cur + 1, len(items) - 1)
                elif press and b == 0:
                    if y <= my < y + hh and x <= mx < x + ww and top + (my - y) < len(items):
                        if top + (my - y) == cur:
                            return items[cur][1]
                        cur = top + (my - y)
                    else:
                        return None

    def popup_prompt(self, label, initial=""):
        """Single-line input box. Returns the text, or "" when cancelled with Esc."""
        c = self.curses
        buf = list(initial)
        c.curs_set(1)
        first = True
        try:
            while True:
                y, x, hh, ww = self.popup_frame(label, 4, max(60, dw(label) + 4), first)
                first = False
                text = "".join(buf)
                shown = text if dw(text) < ww - 1 else text[-(ww - 2):]
                self.put(y, x, shown, fill=ww)
                self.put(y + 1, x, "⏎ ok   Esc cancel", self.dim())
                try:
                    self.scr.move(y, x + min(dw(shown), ww - 1))
                except c.error:
                    pass
                self.scr.refresh()
                self.scr.timeout(-1)
                try:
                    ch = self.scr.get_wch()
                except c.error:
                    continue
                if isinstance(ch, str):
                    if ch in ("\n", "\r", " ") and not buf and time.time() < getattr(self, "ime_until", 0):
                        self.ime_until = 0                 # Enter/Space that only committed the Hangul shortcut
                        continue
                    if ch in ("\n", "\r"):
                        return text.strip()
                    if ch == "\x1b":
                        return None
                    if ch in ("\x7f", "\b"):
                        if buf:
                            buf.pop()
                    elif ch == "\x15":      # ctrl-u
                        buf = []
                    elif ch >= " ":
                        buf.append(ch)
                elif ch in (c.KEY_BACKSPACE, c.KEY_DC):
                    if buf:
                        buf.pop()
                elif ch == c.KEY_ENTER:
                    return text.strip()
        finally:
            c.curs_set(0)

    def prompt_line(self, label, maxlen=400):
        return self.popup_prompt(label) or ""

    def mark(self):
        """m: mark the selection for my next work, with a note; on a marked row: edit / done / remove.
        On the answer tab: save the answer text itself into the mark's note."""
        nid = self.subject or self.item
        n = self.g.nodes.get(nid) if nid else None
        e = self.selected_mark()
        if e is not None and (self.marked(nid) if nid else None) is not e:
            n, nid = None, None      # the cursor is on a mark whose item this graph does not hold
        if e is None and (not n or n.kind not in ("item", "comment")):
            self.msg = "select an issue, PR or comment first"
            return
        label = (self.g.label_num(n) if n.kind == "item"
                 else f"comment by @{n.author} on {self.g.label_num(self.g.nodes[n.parent])}") if n \
            else (e.get("item_num") or e["item"])
        if n is not None and self.focus == "main" and self.MAIN_TABS[self.panels["main"].tab] == "answer" and self.answer \
                and "(waiting for the answer" not in self.answer:
            if e is None:
                e = todo_entry(self.g, nid, "")
                self.todo.append(e)
            e["note"] = (e.get("note", "") + "\n\n" if e.get("note") else "") + self.answer.strip()
            path = save_todo(self.todo)
            self.msg = f"answer saved into the mark for {label} → {path.replace(os.path.expanduser('~'), '~')}"
            self.refresh_all()
            return
        if e is None:
            note = self.popup_prompt(f"mark {label} — my note (Enter = none, Esc = cancel): ")
            if note is None:
                return
            self.todo.append(todo_entry(self.g, nid, note))
            path = save_todo(self.todo)
            self.msg = f"marked {label} → {path.replace(os.path.expanduser('~'), '~')}"
        else:
            choice = self.popup_menu(f"{label} is marked: {trunc((e.get('note') or '(no note)').splitlines()[0], 60)}",
                                     [("edit the note", "edit"), ("mark done", "done"), ("remove the mark", "remove"), ("cancel", None)])
            if choice == "edit":
                e["note"] = self.popup_prompt("note: ", (e.get("note") or "").splitlines()[0] if e.get("note") else "")
            elif choice == "done":
                e["done"] = True
            elif choice == "remove":
                self.todo.remove(e)
            else:
                return
            self.msg = f"todo updated → {save_todo(self.todo).replace(os.path.expanduser('~'), '~')}"
        self.refresh_all()

    def selected_mark(self):
        """The mark the cursor is on, if any: in the Inbox todo section a row carries its entry id, so
        marks whose item is not in this graph (another repo, a closed item) still work — such a row has
        no node id, and m / Del used to act on whatever was selected before it. Otherwise: the mark on
        the current selection."""
        p = self.panels.get(self.focus)
        r = p.current() if p else None
        if r is not None and r.mark:
            return next((e for e in self.todo if e.get("id") == r.mark and not e.get("done")), None)
        nid = self.subject or self.item
        return self.marked(nid) if nid else None

    def unmark(self):
        """Delete: drop the mark under the cursor outright (m offers edit / done / remove instead)."""
        e = self.selected_mark()
        if e is None:
            self.msg = "nothing marked here (m marks it)"
            return
        self.todo.remove(e)
        self.msg = (f"mark removed: {e.get('item_num') or e['item']} → "
                    f"{save_todo(self.todo).replace(os.path.expanduser('~'), '~')}")
        self.refresh_all()

    def confirm(self, question):
        return self.popup_menu(question, [("yes", True), ("no", False)], cur=1) is True

    def search(self):
        p = self.panels[self.focus]
        q = self.prompt_line("/")
        if q:
            p.query = q
            self.search_next(1)

    def search_next(self, direction):
        p = self.panels[self.focus]
        if not p.query:
            self.msg = "no search query (press /)"
            return
        q = p.query.lower()
        n = len(p.rows)
        for k in range(1, n + 1):
            i = (p.cur + direction * k) % n
            if (p.scroll_only or p.valid(i)) and q in p.rows[i].text.lower():
                if p.scroll_only:
                    p.top = i
                else:
                    p.cur, p.free = i, False
                    self.update_subject()
                return
        self.msg = f"not found: {p.query}"

    def ask(self):
        if self.ask_thread is not None and self.ask_thread.is_alive():
            self.msg = "a question is still running"
            return
        nid = self.subject or self.item
        if not nid or nid not in self.g.nodes:
            self.msg = "select an issue, PR, comment or person first"
            return
        n = self.g.nodes[nid]
        label = self.g.label_num(n) if n.kind == "item" else (f"comment on {self.g.label_num(self.g.nodes[n.parent])}"
                                                              if n.kind == "comment" else n.id)
        q = self.prompt_line(f"ask about {label}: ")
        if not q:
            return
        import threading
        st = {"nid": nid, "label": label, "q": q, "t0": time.time()}
        self.ask_state = st
        self.answer = f"Q ({label}): {q}\n\n(waiting for the answer…)"
        self.answer_nid = nid
        self.panels["main"].tab = self.MAIN_TABS.index("answer")
        self.refresh_main()

        def work():
            try:
                st["answer"] = ask_claude(self.g, nid, q)
                save_qa(nid, q, st["answer"])          # anchored to the item/comment, survives restarts
            except Exception as e:  # noqa: BLE001
                st["answer"] = f"error: {e}"
            self.answer = f"Q ({label}): {q}\n\n{st['answer']}"

        self.ask_thread = threading.Thread(target=work, daemon=True)
        self.ask_thread.start()

    # ------------------------------------------------------------------ layout
    def layout(self):
        """Assign content rects to the visible panels for the current screen mode / terminal size."""
        if self.focus in ("home", "links", "comments", "people"):
            self.last_side = self.focus
        h, w = self.scr.getmaxyx()
        H = h - 1                                   # bottom line
        portrait = w <= 84
        side_w = w if portrait else max(26, min(int(w * self.side_width), w - 30))
        for p in self.panels.values():
            p.rect = (0, 0, 0, 0)
        vis = []

        def place(key, y, x, hh, ww):
            if hh >= 3 and ww >= 4:
                self.panels[key].rect = (y + 1, x + 1, hh - 2, ww - 2)
                vis.append(key)

        if self.mode == "review":
            self.layout_review(place, H, w, portrait)
            self.visible = vis
            return
        if self.screen == "full":
            place(self.focus, 0, 0, H, w)
        elif self.screen == "half" or portrait:
            side_key = self.focus if self.focus in self.SIDE else "home"
            if portrait:
                top_h = max(6, H * 2 // 5)
                place(side_key, 0, 0, top_h, w)
                place("main", top_h, 0, H - top_h, w)
            else:
                if self.focus == "main":
                    place("main", 0, 0, H, w)
                else:
                    place(side_key, 0, 0, H, side_w)
                    place("main", 0, side_w, H, w - side_w)
        else:
            repo_h, item_h = 4, 6
            rest = H - repo_h - item_h
            lists = ["home", "comments", "links", "people"]
            if rest < 4 * 3:
                # too small: only the focused list panel gets space, the others collapse to their title bar
                y = 0
                place("repo", y, 0, repo_h, side_w)
                y += repo_h
                place("item", y, 0, item_h, side_w)
                y += item_h
                for k in lists:
                    hh = rest - 2 * 3 if k == (self.focus if self.focus in lists else self.last_side) else 2
                    if hh >= 3:
                        place(k, y, 0, hh, side_w)
                        y += hh
                    else:
                        self.panels[k].rect = (y, 0, 0, side_w)   # title bar only
                        y += 2
            else:
                grow = self.focus if self.focus in lists else self.last_side
                weights = [self.expanded_weight if (self.expand_focused and k == grow) else 1.0 for k in lists]
                total = sum(weights)
                y = 0
                place("repo", y, 0, repo_h, side_w)
                y += repo_h
                place("item", y, 0, item_h, side_w)
                y += item_h
                heights = [max(3, int(rest * wt / total)) for wt in weights]
                heights[-1] = max(3, rest - sum(heights[:-1]))
                for k, hh in zip(lists, heights):
                    place(k, y, 0, hh, side_w)
                    y += hh
            place("main", 0, side_w, H, w - side_w)
        self.visible = vis

    def layout_review(self, place, H, w, portrait):
        """Files | Diff | Findings. Three columns eat width fast, so the fallbacks are pinned numbers
        (docs/PLAN-review-mode.md §1) rather than the browse layout's ratios."""
        f = self.focus if self.focus in self.MODES["review"] else "rfiles"
        if self.screen == "full":
            place(f, 0, 0, H, w)
            return
        files_w = max(22, min(int(w * self.review_files_width), 40))
        find_w = max(26, min(int(w * self.review_findings_width), 52))
        if self.screen == "half":
            if f == "rdiff":
                place("rdiff", 0, 0, H, w)
            else:
                side = max(24, min(files_w if f == "rfiles" else find_w, w - 40))
                place(f, 0, 0, H, side)
                place("rdiff", 0, side, H, w - side)
            return
        if portrait:
            top, bot = max(5, H // 5), max(6, H // 4)
            if self.expand_focused:
                if f == "rfiles":
                    top += H // 6
                elif f == "rfind":
                    bot += H // 6
            mid = H - top - bot
            if mid < 4:                      # no room for three: the focused panel keeps the screen
                place(f, 0, 0, H, w)
                return
            place("rfiles", 0, 0, top, w)
            place("rdiff", top, 0, mid, w)
            place("rfind", top + mid, 0, bot, w)
            return
        diff_w = w - files_w - find_w
        if diff_w < 56 and w >= 100:
            find_w = 26
            diff_w = w - files_w - find_w
        if diff_w >= 56:
            place("rfiles", 0, 0, H, files_w)
            place("rdiff", 0, files_w, H, diff_w)
            place("rfind", 0, files_w + diff_w, H, find_w)
            return
        strip = 0 if H < 16 else max(6, H // 4)     # too narrow for three columns: Findings goes under
        place("rfiles", 0, 0, H, files_w)
        place("rdiff", 0, files_w, H - strip, w - files_w)
        if strip:
            place("rfind", H - strip, files_w, strip, w - files_w)
        else:
            self.panels["rfind"].rect = (H - 1, files_w, 0, w - files_w)   # title bar only

    # ------------------------------------------------------------------ drawing
    def apply_theme(self):
        c = self.curses
        self.pairs = {}
        if not c.has_colors():
            return
        colors, n = c.COLORS, 1
        for st in list(THEMES[THEME]) + ["people"]:
            if st == "people":
                pal = THEMES[THEME]["people"][0 if (colors >= 256 and THEME != "basic") else 1]
                for i, fg in enumerate(pal):
                    if fg < colors:
                        try:
                            c.init_pair(n, fg, -1)
                            self.pairs[f"person#{i}"] = n
                            n += 1
                        except c.error:
                            pass
                continue
            fg256, fg8, _, _ = THEMES[THEME][st]
            fg = fg256 if (colors >= 256 and THEME != "basic") else fg8
            if fg is None or fg >= colors:
                continue
            try:
                c.init_pair(n, fg, -1)
                self.pairs[st] = n
                n += 1
            except c.error:
                pass

    def style_attr(self, st):
        c = self.curses
        if st.startswith("person:"):
            pal = THEMES[THEME]["people"][0 if (c.COLORS >= 256 and THEME != "basic") else 1]
            return c.color_pair(self.pairs.get(f"person#{person_index(st[7:]) % len(pal)}", 0))
        spec = THEMES[THEME].get(st)
        if not spec:
            return 0
        _, _, bold, dim = spec
        a = c.color_pair(self.pairs.get(st, 0))
        if bold:
            a |= c.A_BOLD
        if dim:
            a |= c.A_DIM
        if st == "pending":
            a |= getattr(c, "A_ITALIC", 0)
        if st == "url":
            a |= c.A_UNDERLINE
        return a

    def dim(self):
        return 0 if THEME == "basic" else self.curses.A_DIM

    def put(self, y, x, text, attr=0, fill=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        s = clip(text, 0, (fill or (w - x)) - 0)
        if fill:
            s += " " * max(0, fill - dw(s))
        try:
            self.scr.addstr(y, x, clip(s, 0, w - x - (1 if y == h - 1 else 0)), attr)
        except self.curses.error:
            pass

    def put_row(self, y, x, row, hs, width, extra=0, pad=False, hl=None):
        """hl = (c0, c1): display columns (before hs) drawn reversed, for the drag selection."""
        col, cx = 0, x
        for text, st in colorize_people(segments(row, self.g)):
            attr = self.style_attr(st) | extra
            buf = []
            for ch in text:
                cw = _cw(ch)
                if col + cw > hs + width:
                    break
                if col >= hs:
                    buf.append((ch, hl is not None and hl[0] <= col < hl[1]))
                elif col + cw > hs:
                    buf.append((" ", False))
                col += cw
            i = 0
            while i < len(buf):                       # runs of same highlight state
                j = i
                while j < len(buf) and buf[j][1] == buf[i][1]:
                    j += 1
                sub = "".join(ch for ch, _ in buf[i:j])
                try:
                    self.scr.addstr(y, cx, sub, attr | (self.curses.A_REVERSE if buf[i][1] else 0))
                except self.curses.error:
                    pass
                cx += dw(sub)
                i = j
        if (extra or pad) and cx < x + width:
            try:
                self.scr.addstr(y, cx, " " * (x + width - cx), extra)
            except self.curses.error:
                pass

    def sel_range(self, key, idx):
        """Highlighted display-column range of row idx in panel key, or None."""
        if key != "main" or not self.sel:
            return None
        (r0, c0), (r1, c1) = sorted([self.sel["start"], self.sel["end"]])
        if idx < r0 or idx > r1:
            return None
        if r0 == r1:
            return (min(c0, c1), max(c0, c1) + 1)
        if idx == r0:
            return (c0, 10 ** 6)
        if idx == r1:
            return (0, c1 + 1)
        return (0, 10 ** 6)

    def selection_text(self):
        p = self.panels["main"]
        (r0, c0), (r1, c1) = sorted([self.sel["start"], self.sel["end"]])
        if r0 == r1:
            c0, c1 = min(c0, c1), max(c0, c1)
        parts = []
        for idx in range(r0, min(r1, len(p.rows) - 1) + 1):
            t = p.rows[idx].text
            if r0 == r1:
                parts.append(slice_cols(t, c0, c1 + 1))
            elif idx == r0:
                parts.append(slice_cols(t, c0, 10 ** 6))
            elif idx == r1:
                parts.append(slice_cols(t, 0, c1 + 1))
            else:
                parts.append(t)
        return "\n".join(x.rstrip() for x in parts)

    def draw_box(self, key):
        p = self.panels[key]
        y, x, hh, ww = p.rect
        if ww == 0:
            return
        c = self.curses
        focused = key == self.focus
        tl, tr, bl, br, hz, vt = self.border
        attr = (self.style_attr("fold") | c.A_BOLD) if focused else self.dim()
        keys = self.side_keys()
        num = keys.index(key) + 1 if key in keys else 0
        title = f"{num} {p.title}"
        zones = []                                   # (start col, end col, action) within the title, for mouse clicks
        if p.tabs:
            if key == "home":
                cnt = getattr(self, "home_counts", {})
                k_, t_ = self.HOME_TABS[p.tab]
                title += " "
                zones.append((dw(title), dw(title) + 1, ("tab", -1)))
                title += f"‹ {t_} {cnt.get(k_, 0)} "
                zones.append((dw(title), dw(title) + 1, ("tab", +1)))
                title += f"› {p.tab + 1}/{len(p.tabs)}"
            else:
                title += " "
                n_qa = len(load_qa().get(self.subject or self.item or "", [])) if key == "main" else 0
                for i, t in enumerate(p.tabs):
                    if t == "answer" and n_qa:
                        t = f"answer({n_qa})"
                    lab = f"[{t}]" if i == p.tab else t
                    zones.append((dw(title), dw(title) + dw(lab), ("tab", i)))
                    title += lab + " "
                title = title.rstrip()
            if key == "main":
                sub = self.g.nodes.get(self.subject) if self.subject else None
                what = (self.g.label_num(sub) if sub and sub.kind == "item" else
                        (f"comment on {self.g.label_num(self.g.nodes[sub.parent])}" if sub and sub.kind == "comment" else
                         (self.subject or "")))
                title += f"  · {what}  "
                busy_tr = self.tr_thread is not None and self.tr_thread.is_alive()
                ko = TR_LANG.lower().startswith("korean")
                btn = ("[번역 중…]" if ko else "[translating…]") if busy_tr else \
                      (("[i 원문]" if self.show_tr else "[i 번역]") if ko else ("[i original]" if self.show_tr else "[i translate]"))
                zones.append((dw(title), dw(title) + dw(btn), ("translate", 0)))
                title += btn
        self.title_zones[key] = [(x - 1 + 2 + a, x - 1 + 2 + b, act) for a, b, act in zones]   # screen columns
        if hh == 0:                                  # collapsed to a title bar
            self.put(y, x, clip(f"{tl}{hz}{title}{hz}", 0, ww) + hz * max(0, ww - dw(title) - 3) + tr, attr)
            return
        by, bx, bh, bw = y - 1, x - 1, hh + 2, ww + 2
        top = f"{tl}{hz}{clip(title, 0, bw - 4)}"
        self.put(by, bx, top + hz * max(0, bw - 1 - dw(top)) + tr, attr)
        for i in range(1, bh - 1):
            self.put(by + i, bx, vt, attr)
            self.put(by + i, bx + bw - 1, vt, attr)
        self.put(by + bh - 1, bx, bl + hz * (bw - 2) + br, attr)
        # rows
        p.settle()
        for i in range(hh):
            idx = p.top + i
            if idx >= len(p.rows):
                self.put(y + i, x, "", 0, fill=ww)          # blank the rest of the panel (no erase() per frame)
                continue
            r = p.rows[idx]
            extra = 0
            if not p.scroll_only and idx == p.cur:
                extra = c.A_REVERSE if focused else c.A_UNDERLINE
            self.put_row(y + i, x, r, p.hs, ww, extra, pad=True, hl=self.sel_range(key, idx))
        if len(p.rows) > hh:                          # scroll indicator on the right border
            pos = int((bh - 3) * min(p.top, max(len(p.rows) - hh, 1)) / max(len(p.rows) - hh, 1))
            self.put(by + 1 + pos, bx + bw - 1, "┃" if self.border != BORDERS["hidden"] else "|", attr | c.A_BOLD)

    TOUR = [
        ("home", "Inbox", "What needs you. Tabs ([ ]): my turn = someone else spoke last on something you are in, "
                          "todo = what you marked with m, mentions, opened / active in the last days, waiting on others, "
                          "mine, PRs by others, stale, all. Move with j/k, Enter makes a row the current item."),
        ("item", "Item", "The current item: title, updated date, labels, comment and link counts, a one-line summary "
                         "and its URL (click it to open the browser). Enter shows it in main."),
        ("comments", "Comments", "The item's comments, newest on top: +Nd = days after the item opened, » = one-line "
                                 "summary (made in the background). Enter reads one in main; m marks it for later."),
        ("links", "Links", "Everything this item references or is referenced by: → refs, ← cited-by, → closes, "
                           "← closed-by. The ↳ line says why: the sentence that made the reference, summarised. "
                           "Enter jumps to that item; b comes back, f goes forward."),
        ("people", "People", "Who is in the thread: author, commenters, mentioned people. Enter views the Inbox as that "
                             "person (their turn, their mentions); u does the same by name."),
        ("main", "Main", "Full text of whatever the side cursor is on (content tab), rendered as markdown. "
                         "i translates it, a asks claude a question about it (answer tab), K/J scroll it from anywhere, "
                         "[ ] switch tabs."),
        (None, "Keys", "1-6 and 0 jump to panels, Tab cycles. + and _ change the screen mode (normal / half / full). "
                       "/ searches the focused panel. ? opens the key menu for the panel, O the options menu. "
                       "Shortcuts also work while your keyboard is in Hangul mode. q quits."),
        (None, "Mouse and marks", "Click focuses and selects, double-click = Enter, wheel scrolls without moving the "
                                  "cursor, drag the border between the side column and main to resize. m marks an item or "
                                  "comment with a note; ~/gitgraph-todo.md is rewritten so the next session (or Claude) "
                                  "can pick the work up; gg todo prints it."),
        (None, "Claude", "C opens Claude Code next to gg (a tmux pane, or full screen). Through the gg MCP server it "
                         "sees what you are looking at (gg_state, gg_context), your marks (gg_todo), and can drive gg "
                         "(gg_open, gg_mark). Register once: claude mcp add -s user gg -- gg mcp."),
    ]

    def popup_step(self, title, lines, first=False, last=False):
        """One tutorial step. Returns "next", "prev" or "stop"."""
        c = self.curses
        hint = ("  ".join(x for x in ["⏎/→ next" if not last else "⏎ finish", "←/p prev" if not first else "", "Esc stop"] if x))
        fresh = True
        while True:
            y, x, hh, ww = self.popup_frame(title, len(lines) + 3, max(dw(l) for l in lines + [hint]) + 2, fresh)
            fresh = False
            for i, l in enumerate(lines[:hh - 1]):
                self.put(y + i, x, l)
            self.put(y + hh - 1, x, hint, self.dim())
            self.scr.refresh()
            self.scr.timeout(-1)
            k = self.read_key()
            if k in (10, 13, c.KEY_ENTER, ord(" "), c.KEY_RIGHT, ord("l"), ord("n"), ord("j")):
                return "next"
            if k in (c.KEY_LEFT, ord("h"), ord("p"), ord("k"), c.KEY_BACKSPACE, 127) and not first:
                return "prev"
            if k in (27, ord("q")):
                return "stop"
            if k == c.KEY_MOUSE and self.mouse_ev and self.mouse_ev[3]:
                b = self.mouse_ev[0] & ~28
                if b == 0:
                    return "next"
                if b in (2, 128) and not first:
                    return "prev"

    def tutorial(self):
        if self.item is None:
            r = next((r for r in self.panels["home"].rows if r.nid), None)
            if r:
                self.set_item(r.nid)
        i = 0
        while 0 <= i < len(self.TOUR):
            panel, title, text = self.TOUR[i]
            if panel:
                self.focus = panel
                self.update_subject()
            r = self.popup_step(f"tour {i + 1}/{len(self.TOUR)} — {title}", wrap(text, 72), first=i == 0, last=i == len(self.TOUR) - 1)
            if r == "stop":
                break
            i += 1 if r == "next" else -1
        CONFIG["tutorial_done"] = True
        save_config()
        self.msg = "tour finished — F2 shows it again, ? lists the keys"

    HINTS = {
        "home": "⏎ open item  m mark  [ ] section  / search  a ask  o browser  u view-as",
        "links": "⏎ go to item  / search  a ask  o browser",
        "comments": "⏎ read in main  m mark  / search  a ask  o browser",
        "people": "⏎ view as this person  a ask  o browser",
        "main": "[ ] content / answer  i translate  a ask  K J scroll  o browser",
        "repo": "r refresh (changed only)  R refetch all  c t s p h toggles  T theme  $ tokens",
        "item": "⏎ read in main  m mark  i translate  a ask  o browser  d details",
        "rfiles": "⏎ show this file's diff  r reload  R refetch  o browser  v back to the graph",
        "rdiff": "⏎ fold/unfold a hunk  J K scroll  o open this line on GitHub  v back to the graph",
        "rfind": "⏎ jump to the line  d read it  i in " + TR_LANG + "  V check again  x ignore  [ ] tab  v back",
    }

    def state_snapshot(self):
        g = self.g
        def node_info(nid):
            n = g.nodes.get(nid) if nid else None
            if not n:
                return None
            if n.kind == "item":
                return {"id": nid, "kind": "pr" if n.is_pr else "issue", "label": g.label_num(n), "title": n.title or "",
                        "url": n.url or "", "text": (n.summary or excerpt(n.body, 200))}
            if n.kind == "comment":
                p = g.nodes.get(n.parent)
                return {"id": nid, "kind": "comment", "label": f"comment by @{n.author} {rel_days(n, g)} on {g.label_num(p) if p else '?'}",
                        "title": p.title if p else "", "url": n.url or "", "text": (n.summary or excerpt(n.body, 300))}
            return {"id": nid, "kind": "person", "label": nid, "title": "", "url": "", "text": ""}
        r = self.panels[self.focus].current()
        return {"pid": os.getpid(), "updated": datetime.now().isoformat(timespec="seconds"), "updated_ts": time.time(),
                "repos": self.o["repos"], "me": self.me, "focus": self.focus, "mode": self.mode,
                "inbox_tab": self.HOME_TABS[self.panels["home"].tab][1],
                "item": node_info(self.item), "subject": node_info(self.subject),
                "review": self.review_info(),
                "cursor_row": r.text.strip() if r else "", "answer": (self.answer or "")[:4000],
                "todo_open": sum(1 for e in self.todo if not e.get("done"))}

    def review_info(self):
        """The review half of the snapshot: null in browse mode, so old readers see no change."""
        rv = self.rv
        if self.mode != "review" or rv is None:
            return None
        f = self.review_finding()
        return {"repo": rv.repo, "number": rv.number, "url": rv.url or "", "head_oid": rv.head_oid or "",
                "status": rv.status, "error": rv.error or "", "file": self.rv_path or "",
                "files": [x.path for x in rv.files], "counts": rv.counts(),
                "finding": None if f is None else
                {"fid": f.fid, "severity": f.severity, "verdict": f.verdict, "state": f.state,
                 "anchor": f.anchor, "path": f.path, "line": f.line, "side": f.side,
                 "title": f.title, "body": (f.body or "")[:2000]}}

    def write_state(self):
        st = self.state_snapshot()
        sig = (st["item"], st["subject"], st["focus"], st["mode"], json.dumps(st["review"], sort_keys=True),
               st["inbox_tab"], st["cursor_row"], st["me"], st["todo_open"], st["answer"])
        if sig != getattr(self, "_state_sig", None) or time.time() - getattr(self, "_state_t", 0) > 20:
            self._state_sig, self._state_t = sig, time.time()
            try:
                write_json(STATE_PATH, st)
            except OSError:
                pass

    def poll_cmd(self):
        """Commands from `gg mcp` (Claude in another window): open an item, mark it."""
        cmd = read_json(CMD_PATH)
        if not cmd:
            return
        try:
            os.remove(CMD_PATH)
        except OSError:
            pass
        msg = "unknown command"
        try:
            if cmd.get("op") == "open" and str(cmd.get("id", "")).startswith("finding:"):
                if self.mode != "review" or not self.panels["rfind"].goto_nid(cmd["id"]):
                    msg = "gg is not showing that finding"
                else:
                    self.focus = "rfind"
                    msg = f"gg now shows {cmd['id']}"
            elif cmd.get("op") == "open" and cmd.get("id") in self.g.nodes:
                self.set_item(cmd["id"])
                self.focus = "item"
                msg = f"gg now shows {self.g.label_num(self.g.nodes[cmd['id']])}"
            elif cmd.get("op") == "done":
                msg = todo_finish(cmd.get("id", ""), bool(cmd.get("remove")))
                self.todo = load_todo()
                self.refresh_all()
            elif cmd.get("op") == "mark" and cmd.get("id") in self.g.nodes:
                if not self.marked(cmd["id"]):
                    self.todo.append(todo_entry(self.g, cmd["id"], cmd.get("note", "")))
                    save_todo(self.todo)
                    self.refresh_all()
                msg = f"marked {self.g.label_num(self.g.nodes[cmd['id']]) if self.g.nodes[cmd['id']].kind == 'item' else cmd['id']}"
            self.msg = f"claude: {msg}"
        except Exception as e:  # noqa: BLE001
            msg = f"failed: {e}"
        write_json(CMD_RESULT_PATH, {"req": cmd.get("req"), "msg": msg})

    def launch_claude(self):
        """C: Claude Code in another pane (tmux) or full screen, connected through `gg mcp`."""
        import shlex
        cmd = f"claude {shlex.quote(CLAUDE_PROMPT)}"
        if os.environ.get("TMUX"):
            r = subprocess.run(["tmux", "split-window", "-h", "-c", os.getcwd(), cmd], capture_output=True, text=True)
            self.msg = "claude opened in a tmux pane (it reads gg through the gg MCP server)" if r.returncode == 0 else f"tmux failed: {r.stderr.strip()}"
            return
        self.curses.endwin()
        try:
            subprocess.call(["claude", CLAUDE_PROMPT])
        finally:
            self.scr.clear()
            self.scr.refresh()
            sys.stdout.write("\033[?1000h\033[?1002h\033[?1006h")
            sys.stdout.flush()
            self.msg = "back from claude"

    def draw(self):
        c = self.curses
        scr = self.scr
        self.write_state()
        h, w = scr.getmaxyx()
        if (h, w) != getattr(self, "_size", None):     # only a resize clears the screen; frames overwrite every cell
            self._size = (h, w)
            scr.erase()
        self.layout()
        widths = tuple(p.rect[3] for p in self.panels.values())
        if widths != getattr(self, "_widths", None):
            self._widths = widths
            self.refresh_all()          # "…" truncation follows the new panel widths
            self.layout()
        for key in self.visible:
            self.draw_box(key)
        for key in self.side_keys():                  # collapsed title bars in the tiny layout
            if key not in self.visible and self.panels[key].rect[3] and self.screen == "normal":
                self.draw_box(key)
        if self.busy():
            bottom = self.progress_text()
            attr = c.A_BOLD
        elif self.bg_error:
            bottom, attr = f"background work failed: {self.bg_error}", self.style_attr("closed")
        elif self.msg:
            bottom, attr = self.msg, c.A_BOLD
        else:
            nav = ("1-3 Tab panels  + _ screen  ? keys  q quit" if self.mode == "review" else
                   "1-6 0 Tab panels  + _ screen  b f back/fwd  ? keys  q quit")
            short = "1-3 Tab  + _  ? q" if self.mode == "review" else "1-6 0 Tab  + _  ? q"
            bottom = (f"{self.HINTS.get(self.focus, '')}   {nav}"
                      if w >= 110 else f"{self.HINTS.get(self.focus, '')[:max(0, w - 30)]}  {short}")
            attr = self.dim()
        self.put(h - 1, 0, bottom, attr, fill=w - 1)
        scr.refresh()

    # ------------------------------------------------------------------ input
    ESC_SEQ = {"A": "KEY_UP", "B": "KEY_DOWN", "C": "KEY_RIGHT", "D": "KEY_LEFT", "H": "KEY_HOME", "F": "KEY_END",
               "5~": "KEY_PPAGE", "6~": "KEY_NPAGE", "1~": "KEY_HOME", "4~": "KEY_END", "3~": "KEY_DC", "Z": "KEY_BTAB"}

    def read_key(self):
        c = self.curses
        k = self.scr.getch()
        if 0xC2 <= k <= 0xF4:                      # a UTF-8 character: Hangul typed in IME mode?
            n = 1 if k < 0xE0 else 2 if k < 0xF0 else 3
            bs = bytes([k])
            self.scr.timeout(60)      # the rest of the character may land in a later read (IME, ssh, tmux)
            try:
                for _ in range(n):
                    b = self.scr.getch()
                    if b == -1:
                        break
                    bs += bytes([b])
            finally:
                self.scr.nodelay(False)   # the main loop sets its own timeout before the next getch
            ch = bs.decode("utf-8", "replace")
            keys = hangul_keys(ch)
            if keys:
                for extra in reversed(keys[1:]):
                    c.ungetch(ord(extra))
                self.ime_until = time.time() + 0.4      # the IME commit key (Enter/Space) that follows is not a command
                return ord(keys[0])
            return ord(ch) if len(ch) == 1 else k
        if k in (10, 13, 32) and time.time() < getattr(self, "ime_until", 0):
            self.ime_until = 0
            return -1
        if k == c.KEY_MOUSE:
            self.mouse_ev = None
            self.scr.nodelay(True)
            try:
                seq = ""
                for _ in range(24):
                    k2 = self.scr.getch()
                    if k2 == -1:
                        break
                    seq += chr(k2)
                    if seq[-1] in "Mm":
                        break
            finally:
                self.scr.nodelay(False)
            m = re.match(r"<?(\d+);(\d+);(\d+)([Mm])", seq)
            if m:
                self.mouse_ev = (int(m.group(1)), int(m.group(2)) - 1, int(m.group(3)) - 1, m.group(4) == "M")
            return k
        if k != 27:
            return k
        self.scr.nodelay(True)
        try:
            k2 = self.scr.getch()
            if k2 not in (ord("["), ord("O")):
                if k2 != -1:
                    c.ungetch(k2)
                return 27
            seq = ""
            for _ in range(8):
                k3 = self.scr.getch()
                if k3 == -1:
                    break
                seq += chr(k3)
                if seq[-1].isalpha() or seq[-1] == "~":
                    break
            name = self.ESC_SEQ.get(seq)
            return getattr(c, name) if name else 27
        finally:
            self.scr.nodelay(False)

    def panel_at(self, x, y):
        for key in self.visible:
            py, px, ph, pw = self.panels[key].rect
            if py - 1 <= y <= py + ph and px - 1 <= x <= px + pw:
                return key
        return None

    def border_x(self):
        m = self.panels["main"].rect
        return m[1] - 1 if m[3] and m[1] > 1 else None

    def on_mouse(self):
        ev = self.mouse_ev
        if not ev:
            return
        b, x, y, press = ev
        h, w = self.scr.getmaxyx()
        mp = self.panels["main"]
        if os.environ.get("GG_DEBUG"):
            log(f"mouse b={b} x={x} y={y} press={press} main.rect={mp.rect} sel={self.sel} dragging={self.dragging}")
        if b & 32:                                   # motion with a button held
            if self.dragging and w > 40:
                self.side_width = min(0.8, max(0.15, (x + 1) / w))
            elif self.sel and self.sel.get("live"):
                self.sel["end"] = (mp.top + max(0, min(y - mp.rect[0], mp.rect[2] - 1)), max(0, x - mp.rect[1]) + mp.hs)
            return
        if not press:
            if self.dragging:
                self.dragging = False
                self.msg = f"side width {self.side_width:.2f}   (keep it: gg config side_width {self.side_width:.2f})"
            elif self.sel and self.sel.get("live"):
                self.sel["live"] = False
                if self.sel["start"] == self.sel["end"]:      # no movement: a plain click on main
                    self.sel = None
                    self.focus = "main"
                    self.mouse_ev = (0, x, y, True)
                    self.click_main(x, y)
                else:
                    text = self.selection_text()
                    how = copy_to_clipboard(text)
                    self.msg = f"copied {len(text)} chars ({', '.join(how) or 'no clipboard tool'})"
            return
        base = b & ~28
        bx = self.border_x()
        if base == 0 and bx is not None and bx - 1 <= x <= bx and self.screen == "normal" and w > 84:   # the two border columns
            self.dragging = True
            return
        key = self.panel_at(x, y)
        if base == 0 and key == "main" and mp.rect[0] <= y < mp.rect[0] + mp.rect[2] and mp.rect[3]:
            self.sel = {"start": (mp.top + (y - mp.rect[0]), max(0, x - mp.rect[1]) + mp.hs), "live": True}
            self.sel["end"] = self.sel["start"]
            return                                   # decided on release: click or copy
        if base in (64, 65):
            if key:
                p = self.panels[key]
                d = -3 if base == 64 else 3
                if p.scroll_only:
                    p.top = max(0, p.top + d)
                else:
                    p.top = max(0, p.top + d)
                    p.free = True
            return
        if base == 128:
            self.back()
            return
        if base == 129:
            self.forward()
            return
        if base != 0 or not key:
            return
        p = self.panels[key]
        if y == p.rect[0] - 1:                   # the title bar: ‹ › or a tab name switches tabs
            for x0, x1, act in self.title_zones.get(key, []):
                if x0 <= x < x1:
                    self.focus = key
                    if act[0] == "translate":
                        self.translate_content()
                    else:
                        self.switch_tab(key, act, relative=(key == "home"))
                    return
            self.focus = key
            return
        if key != self.focus:                    # first click on another panel: focus it, keep its cursor
            self.focus = key
            self.update_subject()
            self.last_click = (0.0, -1, "")
            return
        self.click_main(x, y, key)

    def click_main(self, x, y, key="main"):
        idx = self.click_row(key, y)
        self.update_subject()
        p = self.panels[key]
        ridx = p.top + (y - p.rect[0])
        if 0 <= y - p.rect[0] < p.rect[2] and ridx < len(p.rows) and p.rows[ridx].kind == "url":
            text = p.rows[ridx].text
            url = text.strip()
            xr = x - p.rect[1]                                        # column inside the panel
            c0 = dw(text[:len(text) - len(text.lstrip())]) - p.hs      # column where the URL starts
            visible_end = min(c0 + dw(url), p.rect[3])                # the URL may be clipped by the panel edge
            ch = char_at(text, xr + p.hs)                             # what is actually drawn under the pointer
            on_url = c0 <= xr < visible_end and ch not in ("", " ")
            if url.startswith("http") and on_url:
                try:
                    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self.msg = f"opened {url}"
                except OSError as e:
                    self.msg = f"cannot open browser: {e}"
                return
        now = time.time()
        if now - self.last_click[0] < 0.4 and self.last_click[1] == idx and self.last_click[2] == key:
            self.last_click = (0.0, -1, "")
            p = self.panels[key]
            r = p.current()
            if r and key == "main" and not p.scroll_only:
                m = re.search(r"[▾▸·] ", r.text)
                if m and (dw(r.text[:m.start()]) - p.hs) <= x - p.rect[1] <= (dw(r.text[:m.start()]) - p.hs) + 1:
                    self.toggle_fold()
                    return
            self.enter()
        else:
            self.last_click = (now, idx, key)

    def click_row(self, key, y):
        p = self.panels[key]
        idx = p.top + (y - p.rect[0])
        if not p.scroll_only and 0 <= y - p.rect[0] < p.rect[2] and idx < len(p.rows) and p.valid(idx):
            p.cur, p.free = idx, False
            return idx
        return -1

    # ------------------------------------------------------------------ main loop
    def switch_tab(self, key, action, relative=False):
        p = self.panels[key]
        if not p.tabs:
            return
        p.tab = (p.tab + action[1]) % len(p.tabs) if relative else action[1] % len(p.tabs)
        p.top = 0
        if key == "home":
            p.set_rows(self.home_rows(), keep=False)
            p.cur = 0
            if self.focus == "home":
                self.update_subject()
        elif key == "main":
            self.refresh_main()
        elif key == "rfind":
            p.cur = 0
            self.refresh_review(keep=False)
        self.enrich()

    def cycle_focus(self, d):
        order = self.side_keys() + (["main"] if self.mode == "browse" else [])
        if self.focus not in order:
            self.focus = order[0]
            return
        self.focus = order[(order.index(self.focus) + d) % len(order)]
        if self.focus == "repo":
            self.focus = order[(order.index("repo") + d) % len(order)]

    def run(self):
        c = self.curses
        try:
            self._run()
        finally:
            sys.stdout.write("\033[?1006l\033[?1002l\033[?1000l")
            sys.stdout.flush()

    KEYMENU = [   # (context, keys, description, key code fed to handle_key)
        ("*", "⏎", "open / go to / re-root (see the panel hint)", 10),
        ("*", "Tab", "next panel", 9), ("*", "[ ]", "previous / next tab", ord("]")),
        ("*", "+ _", "screen mode normal / half / full", ord("+")),
        ("*", "a", "ask claude about the selection", ord("a")),
        ("*", "i", "translate the main content in full (toggle original / translation)", ord("i")),
        ("*", "y", "copy the URL of the selection", ord("y")),
        ("browse", "m", "mark for my next work (with a note) / edit, done, remove", ord("m")),
        ("browse", "Del", "remove the mark on the selection", 0),
        ("browse", "C", "open Claude Code next to gg (tmux pane / full screen), connected via gg mcp", ord("C")),
        ("browse", "d", "details", ord("d")), ("*", "o", "open in the browser", ord("o")),
        ("browse", "b f", "back / forward", ord("b")), ("browse", "u", "view Inbox as another person", ord("u")),
        ("browse", "F2", "guided tour of the screen", 0),
        ("browse", "v", "review this pull request (Files / Diff / Findings)", ord("v")),
        ("*", "/", "search in this panel", ord("/")), ("browse", "O", "options menu (toggles)", ord("O")),
        ("browse", "r", "refresh what changed on GitHub (background)", ord("r")),
        ("browse", "R", "refetch everything", ord("R")), ("*", "T", "colour theme", ord("T")),
        ("*", "$", "token usage", ord("$")), ("*", "q", "quit", ord("q")),
        ("main", "K J", "scroll", ord("J")),
        ("review", "v Esc", "back to the graph", ord("v")),
        ("review", "r", "reload the PR and its diff", ord("r")),
        ("review", "R", "refetch it, ignoring the cached findings", ord("R")),
        ("rfind", "d", "read the whole finding (body, evidence, suggested fix)", ord("d")),
        ("rfind", "i", f"the same in {TR_LANG} — what P posts is always the original", ord("i")),
        ("rfind", "V", "check this finding again (a fresh call that tries to disprove it)", ord("V")),
        ("rfind", "x", "ignore this finding / take it back", ord("x")),
    ]

    def key_menu(self):
        items = [(f"{keys:6} {desc}", code) for ctx, keys, desc, code in self.KEYMENU
                 if ctx in ("*", self.focus, self.mode)]
        code = self.popup_menu(f"keys — {self.panels[self.focus].title} panel", items)
        if code is not None and code != ord("q"):
            self.handle_key(code)

    def offer_ai_switch(self, fail):
        """After an AI CLI failure: switch to an installed alternative, keep trying, or turn AI features off."""
        cur = os.path.basename(fail["bin"])
        alts = installed_ais(exclude=cur)
        items = [(f"switch to {a}   ({AI_BACKENDS[a][0].split(':')[0]})", ("switch", a)) for a in alts]
        items += [(f"keep {cur} and try again later" + (f"   (login: {AI_BACKENDS[cur][1]})" if cur in AI_BACKENDS and AI_BACKENDS[cur][1] else ""), ("keep", None)),
                  ("turn AI features off for this session (no translation / summaries / questions)", ("off", None))]
        choice = self.popup_menu(f"{cur} failed: {trunc(fail['msg'], 70)}", items)
        if not choice or choice[0] == "keep":
            self.msg = f"{cur} kept — gg ai switches the AI CLI"
            return
        if choice[0] == "off":
            self.o["translate"], self.o["summary"], self.show_tr = "none", False, False
            self.enriched = set()
            self.msg = "AI features off for this session (gg ai / O to change)"
            self.refresh_all()
            return
        switch_ai(choice[1])
        self.enriched = set()
        self.ai_prompted = len(AI_FAILURES)
        self.msg = f"AI CLI = {choice[1]} (saved with gg config claude_bin)"
        self.refresh_all()

    def options_menu(self):
        o = self.o
        items = [(f"comments: {o['comments']}  (cycle)", ord("c")),
                 (f"translation: {o['translate']}  (toggle)", ord("t")),
                 (f"summaries: {'on' if o['summary'] else 'off'}  (toggle)", ord("s")),
                 (f"people nodes: {'on' if o['people'] else 'off'}  (toggle)", ord("p")),
                 (f"hops: {o['hops']}  (cycle 1/2/3)", ord("h")),
                 (f"AI CLI: {os.path.basename(CLAUDE_BIN)}  (gg ai to change)", None),
                 (f"theme: {THEME}  (cycle)", ord("T")),
                 (f"screen mode: {self.screen}  (cycle)", ord("+")),
                 (f"side width: {self.side_width:.2f}  (drag the border with the mouse; gg config side_width)", None),
                 ("refresh what changed on GitHub", ord("r")), ("refetch everything", ord("R"))]
        code = self.popup_menu("options", items)
        if code is not None:
            self.handle_key(code)

    def _run(self):
        c = self.curses
        while True:
            finished = [t for t in self.jobs if not t.is_alive()]
            if finished:
                self.jobs = [t for t in self.jobs if t.is_alive()]
                if not self.jobs:
                    self.progress = None
                if getattr(self, "_new_g", None) is not None:      # a background refresh finished: swap the graph in
                    self.g, self._new_g, self.partial = self._new_g, None, False
                    self.enriched = set()
                    self.rebuild_graph()
                    self.msg = getattr(self, "_refresh_note", "refreshed")
                if self._new_rv is not None:                        # the worktree and diff are ready
                    self.rv, self._new_rv = self._new_rv, None
                    self.rv_path = None
                if self.rv is not None and self.rv.status == "done" and self.rv.t1 and not self._told_review:
                    self._told_review = True
                    cnt = self.rv.counts()          # not `c`: that is the curses module in this loop
                    self.msg = (f"review done in {int(self.rv.t1 - (self.rv.t0 or self.rv.t1))}s: "
                                f"{cnt['open']} open, {cnt['dropped']} dropped"
                                + (f"  ({self.rv.error})" if self.rv.error else ""))
                elif self.rv is not None and self.rv.error and self.rv.status == "failed":
                    self.msg = f"review failed: {self.rv.error.splitlines()[0]}"
                self.refresh_all()                                  # rows + next enrich jobs
            if not self.busy() and time.time() - getattr(self, "last_refresh", self.t0) > self.o["max_age_min"] * 60:
                self.refresh_bg()                                   # auto: every max-age minutes, incrementally
            if self.ask_thread is not None and not self.ask_thread.is_alive():
                self.ask_thread = None
                self.refresh_main()
            if self.tr_thread is not None and not self.tr_thread.is_alive():
                self.tr_thread = None
                self.refresh_main()          # shows the translation, and starts the next one if the subject moved on
            elif self.tr_thread is not None and self.MAIN_TABS[self.panels["main"].tab] == "content":
                top = self.panels["main"].top
                self.refresh_main()
                self.panels["main"].top = top
            if len(AI_FAILURES) > self.ai_prompted and not self.busy():
                self.ai_prompted = len(AI_FAILURES)
                self.offer_ai_switch(AI_FAILURES[-1])
            self.scr.timeout(400 if self.busy() else 500)
            self.draw()
            k = self.read_key()
            if k == -1:
                self.poll_cmd()
                continue
            if not (k == c.KEY_MOUSE and self.mouse_ev and not self.mouse_ev[3]):   # a button release keeps the message
                self.msg = ""
            if not self.handle_key(k):
                return

    def handle_key(self, k):
        """Returns False to quit."""
        c = self.curses
        if k != c.KEY_MOUSE and self.sel and not self.sel.get("live"):
            self.sel = None
        h, w = self.scr.getmaxyx()
        p = self.panels[self.focus]
        page = max(p.rect[2] - 1, 1)
        if os.environ.get("GG_DEBUG"):
            log(f"key={k!r} focus={self.focus} cur={p.cur} rows={len(p.rows)} item={self.item} subject={self.subject} me={self.me}")
        if k == ord("q"):
            return False
        if k == c.KEY_MOUSE:
            self.on_mouse()
            return True
        if k == c.KEY_RESIZE:
            return True
        if self.mode == "review":
            handled = self.review_key(k)
            if handled is not None:
                return handled
        elif k == ord("v"):
            self.toggle_review()
            return True
        # ---- panels ----
        keys = self.side_keys()
        if ord("1") <= k <= ord("9") and k - ord("1") < len(keys):
            self.focus = keys[k - ord("1")]
            self.update_subject()
            return True
        if k == ord("0") and self.mode == "browse":
            self.focus = "main"
            return True
        if k == 9:
            self.cycle_focus(1)
            self.update_subject()
            return True
        if k == c.KEY_BTAB:
            self.cycle_focus(-1)
            self.update_subject()
            return True
        if k in (ord("["), ord("]")) and p.tabs:
            self.switch_tab(self.focus, ("tab", 1 if k == ord("]") else -1), relative=True)
            return True
        if k == ord("+"):
            self.screen = {"normal": "half", "half": "full", "full": "normal"}[self.screen]
            return True
        if k == ord("_"):
            self.screen = {"normal": "full", "half": "normal", "full": "half"}[self.screen]
            return True
        # ---- navigation in the focused panel ----
        if k in (c.KEY_DOWN, ord("j")):
            p.move(1)
            self.update_subject()
        elif k in (c.KEY_UP, ord("k")):
            p.move(-1)
            self.update_subject()
        elif k in (c.KEY_NPAGE, ord(".")):
            p.move(page)
            self.update_subject()
        elif k in (c.KEY_PPAGE, ord(",")):
            p.move(-page)
            self.update_subject()
        elif k in (ord("g"), ord("<"), c.KEY_HOME):
            p.free = False
            p.cur, p.top = 0, 0
            p.settle()
            self.update_subject()
        elif k in (ord("G"), ord(">"), c.KEY_END):
            p.free = False
            p.cur = len(p.rows) - 1
            p.top = max(len(p.rows) - max(p.rect[2], 1), 0)
            p.settle()
            self.update_subject()
        elif k == ord("L"):
            p.hs += 20
        elif k == ord("H"):
            p.hs = max(p.hs - 20, 0)
        elif k == ord("J"):
            self.panels["main"].move(3) if self.panels["main"].scroll_only else self.panels["main"].move(1)
        elif k == ord("K"):
            self.panels["main"].move(-3) if self.panels["main"].scroll_only else self.panels["main"].move(-1)
        elif k in (10, 13, c.KEY_ENTER):
            self.enter()
        elif k in (27, c.KEY_BACKSPACE, 127, 8, ord("b")):
            self.back()
        elif k == ord("f"):
            self.forward()
        elif k == ord("a"):
            self.ask()
        elif k == ord("m"):
            self.mark()
        elif k == c.KEY_DC:
            self.unmark()
        elif k == ord("C"):
            self.launch_claude()
        elif k == ord("i"):
            self.translate_content()
        elif k == ord("y"):
            url = self.node_url(self.subject or self.item)
            if url:
                self.msg = f"copied {url} ({', '.join(copy_to_clipboard(url)) or 'no clipboard tool'})"
        elif k == ord("d"):
            self.details()
        elif k == ord("o"):
            self.open_browser()
        elif k == ord("u"):
            who = self.prompt_line("view as @login (empty = my gh accounts): ").lstrip("@")
            self.view_as([who] if who else (ME or [a.lower() for a in gh_accounts()]))
        elif k == ord("r"):
            self.refresh_bg()
        elif k == ord("R"):
            if self.confirm("Refetch everything from GitHub (full, slow)?"):
                self.load(refresh=True)
        elif k == ord("O"):
            self.options_menu()
        elif k == ord("c"):
            i = self.COMMENTS_CYCLE.index(self.o["comments"])
            self.o["comments"] = self.COMMENTS_CYCLE[(i + 1) % 3]
            self.rebuild_graph()
            self.refresh_all()
        elif k == ord("p"):
            self.o["people"] = not self.o["people"]
            self.rebuild_graph()
            self.refresh_all()
        elif k == ord("h"):
            self.o["hops"] = self.o["hops"] % 3 + 1
            self.refresh_main()
        elif k == ord("t"):
            if self.o["translate"] == "none":
                self.o["translate"] = self.tr_saved
            else:
                self.tr_saved, self.o["translate"] = self.o["translate"], "none"
                for n in self.g.nodes.values():
                    n.tr_title = n.tr_excerpt = None
            self.enriched.clear()
            self.refresh_all()
        elif k == ord("s"):
            self.o["summary"] = not self.o["summary"]
            if not self.o["summary"]:
                for n in self.g.nodes.values():
                    n.summary = None
            self.enriched.clear()
            self.refresh_all()
        elif k == ord("/"):
            self.search()
        elif k == ord("n"):
            self.search_next(1)
        elif k == ord("N"):
            self.search_next(-1)
        elif k == ord("T"):
            global THEME
            names = list(THEMES)
            THEME = names[(names.index(THEME) + 1) % len(names)]
            self.apply_theme()
            self.panels["repo"].rows = self.repo_rows()
            self.msg = f"theme: {THEME}   (keep it: gg config theme {THEME})"
        elif k == ord("$"):
            self.pager(usage_report(), "claude token usage")
        elif k == ord("?"):
            self.key_menu()
        elif k == c.KEY_F1:
            self.popup_text("help", HELP.splitlines())
        elif k == c.KEY_F2 or k == 0:
            self.tutorial()
        return True


def tui(opts):
    import curses
    import locale
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    os.makedirs(CACHE_DIR, exist_ok=True)
    errf = open(os.path.join(CACHE_DIR, "tui.log"), "a")
    os.dup2(errf.fileno(), 2)
    try:
        curses.wrapper(lambda scr: Tui(scr, opts).run())
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code)


# --------------------------------------------------------------------------
# request handling (shared by CLI and MCP)
# --------------------------------------------------------------------------
def graph_rows(repos=None, layout="tree", state="open", comments="linked", people=True,
               closed_neighbors=True, root=None, hops=2, max_age_min=15, refresh=False, width=60,
               translate=None, summary=False):
    """(rows, rendered graph) for the overview or a focus view."""
    repos = resolve_repos(repos)
    translate = translate or DEFAULT_TRANSLATE
    g = build_graph(repos, state, max_age_min, refresh)
    g2 = apply_filters(g, comments, people, closed_neighbors)
    if root:
        rid = resolve_root(g, root)
        if rid not in g2.nodes:
            raise ValueError(f"{rid} was filtered out (comments={comments}, people={people})")
        g2 = subgraph(g2, focus(g2, rid, hops))
    prepare_translations(g2, translate)
    if summary:
        prepare_summaries(g2)
    if root:
        return focus_rows(g2, rid, layout, width), g2
    return overview_rows(g2, layout, width), g2


def do_show(id_, repos=None, state="open", max_age_min=15, refresh=False, translate=None):
    repos = resolve_repos(repos)
    g = build_graph(repos, state, max_age_min, refresh)
    nid = resolve_root(g, id_)
    prepare_translations(subgraph(g, {nid} | {m for m, t, o in g.adj[nid]}), translate or DEFAULT_TRANSLATE)
    return render_show(g, nid)


# --------------------------------------------------------------------------
# gg check: why can't I see the issues/PRs of this repo?
# --------------------------------------------------------------------------
def check_cmd(repos):
    ok = True
    for repo in repos:
        host, owner, name = split_repo(repo)
        print(f"== {repo}  (host {host})")
        accts = gh_accounts(host)
        print(f"   gh accounts for {host}: {', '.join(accts) or 'NONE — run: gh auth login -h ' + host}")
        if not accts:
            ok = False
            continue
        good = 0
        for acct in accts:
            env = dict(os.environ)
            tok = gh_token(acct, host)
            if not tok:
                print(f"   @{acct}: no token (gh auth refresh -h {host} -u {acct}?)")
                continue
            env["GH_TOKEN"] = env["GH_ENTERPRISE_TOKEN"] = tok
            q = ('query($o:String!,$n:String!){ repository(owner:$o,name:$n){ nameWithOwner isPrivate hasIssuesEnabled '
                 'isFork parent{ nameWithOwner } '
                 'issues(states:[OPEN]){totalCount} pullRequests(states:[OPEN]){totalCount} '
                 'allIssues: issues{totalCount} allPRs: pullRequests{totalCount} } viewer{login} }')
            body = json.dumps({"query": q, "variables": {"o": owner, "n": name}})
            r = gh_api(["graphql", "--hostname", host, "--input", "-"], body, env)
            try:
                d = json.loads(r.stdout)
            except json.JSONDecodeError:
                d = {}
            errs = d.get("errors") or []
            rep_ = (d.get("data") or {}).get("repository")
            who = ((d.get("data") or {}).get("viewer") or {}).get("login", "?")
            if rep_:
                print(f"   @{acct} (token user {who}): OK — {'private' if rep_['isPrivate'] else 'public'}, "
                      f"open issues {rep_['issues']['totalCount']} / open PRs {rep_['pullRequests']['totalCount']} "
                      f"(all-time {rep_['allIssues']['totalCount']} / {rep_['allPRs']['totalCount']}), "
                      f"issues {'enabled' if rep_['hasIssuesEnabled'] else 'DISABLED'}")
                good += 1
                if rep_.get("isFork") and rep_.get("parent"):
                    print(f"      -> this is a FORK of {rep_['parent']['nameWithOwner']}: the issues/PRs live there; "
                          f"gg now switches to the parent automatically (or: gg -r {rep_['parent']['nameWithOwner']})")
                elif rep_["issues"]["totalCount"] + rep_["pullRequests"]["totalCount"] == 0:
                    print("      -> nothing is open; gg shows open items only (try --state all)")
            else:
                msg = "; ".join(e.get("message", "") for e in errs) or (r.stderr or "").strip()[:200] or "no data"
                print(f"   @{acct} (token user {who}): no access — {msg}")
        if not good:
            print(f"   -> no account on {host} can read {owner}/{name}: log in with one that can (gh auth login -h {host})")
            ok = False
        # the fields gg's real query needs (older GitHub Enterprise may lack some)
        env = dict(os.environ)
        tok = gh_token(accts[0], host)
        env["GH_TOKEN"] = env["GH_ENTERPRISE_TOKEN"] = tok or ""
        q = ('query($o:String!,$n:String!){ repository(owner:$o,name:$n){ pullRequests(first:1){ nodes{ '
             'closingIssuesReferences(first:1){totalCount} reviews(first:1){totalCount} '
             'timelineItems(first:1, itemTypes:[CROSS_REFERENCED_EVENT]){totalCount} } } } }')
        r = gh_api(["graphql", "--hostname", host, "--input", "-"], json.dumps({"query": q, "variables": {"o": owner, "n": name}}), env)
        try:
            errs = (json.loads(r.stdout) or {}).get("errors") or []
        except json.JSONDecodeError:
            errs = [{"message": (r.stderr or r.stdout or "").strip()[:200]}]
        bad = [e.get("message", "") for e in errs if "NOT_FOUND" not in (e.get("type") or "")]
        print("   GraphQL fields gg uses (closingIssuesReferences, reviews, timelineItems): "
              + ("OK" if not bad else "PROBLEM — " + "; ".join(bad)[:300]))
        if bad:
            ok = False
    print("\nresult:", "everything looks fine — if the tui still shows nothing, run with --refresh (cache) and check ~/.cache/gitgraph/tui.log"
          if ok else "see the lines marked 'no access' / PROBLEM above")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# todo: marks made in the tui (m) -> todo.json (source) + a markdown file for the next session
# --------------------------------------------------------------------------
TODO_JSON = os.path.expanduser("~/.config/gitgraph/todo.json")


def load_todo():
    try:
        with open(TODO_JSON) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def todo_md_path():
    return os.path.expanduser(cfg("todo_file"))


def render_todo_md(entries):
    lines = ["# gg todo", "",
             "Marks made in `gg tui` with `m` (source: ~/.config/gitgraph/todo.json; `gg todo` prints this file).",
             "Each entry: the issue/PR, the comment it was marked on (if any) and my note. `[x]` = done.", ""]
    by_repo = {}
    for e in entries:
        by_repo.setdefault(e["repo"], []).append(e)
    for repo, es in by_repo.items():
        lines += [f"## {repo}", ""]
        for e in sorted(es, key=lambda e: (e.get("done", False), e["created"]), reverse=False):
            box = "[x]" if e.get("done") else "[ ]"
            lines.append(f"- {box} {e['created'][:10]} {e['item_num']} {e['title']} — {e['url']}")
            if e.get("comment_url"):
                lines.append(f"  - comment by @{e['comment_author']} {e['comment_when']}: {e['comment_text']} — {e['comment_url']}")
            if e.get("note"):
                note_lines = e["note"].splitlines()
                lines.append(f"  - note: {note_lines[0]}")
                lines.extend("    " + x for x in note_lines[1:])
        lines.append("")
    return "\n".join(lines)


def save_todo(entries):
    os.makedirs(os.path.dirname(TODO_JSON), exist_ok=True)
    with open(TODO_JSON, "w") as f:
        json.dump(entries, f, ensure_ascii=False, indent=1)
    p = todo_md_path()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w") as f:
        f.write(render_todo_md(entries))
    return p


QA_JSON = os.path.expanduser("~/.config/gitgraph/qa.json")


def load_qa():
    return read_json(QA_JSON) or {}


def save_qa(nid, question, answer):
    """Append a question/answer pair anchored to a node (issue, PR or comment); kept across sessions."""
    qa = load_qa()
    qa.setdefault(nid, []).append({"when": datetime.now().isoformat(timespec="minutes"), "q": question, "a": answer})
    write_json(QA_JSON, qa)


def todo_find(entries, ref):
    """Entries matching ref: an item id/number ('750', '#750', 'owner/name#750', node id), a comment node id or url."""
    ref = (ref or "").strip()
    num = re.sub(r"^.*#", "", ref) if "#" in ref or ref.isdigit() else None
    hits = []
    for e in entries:
        if e.get("done"):
            continue
        if ref in (e.get("item"), e.get("comment"), e.get("url"), e.get("comment_url"), e.get("id")):
            hits.append(e)
        elif num and (e.get("item_num") == f"#{num}" or e.get("item", "").endswith(f"#{num}")):
            hits.append(e)
    return hits


def todo_finish(ref, remove=False):
    """Mark every open entry matching ref done (or remove it). Returns a message."""
    entries = load_todo()
    hits = todo_find(entries, ref)
    if not hits:
        return f"nothing marked matches {ref!r}"
    for e in hits:
        if remove:
            entries.remove(e)
        else:
            e["done"] = True
            e["done_at"] = datetime.now().isoformat(timespec="minutes")
    path = save_todo(entries)
    what = "removed" if remove else "marked done"
    return f"{what} {len(hits)} entr{'y' if len(hits) == 1 else 'ies'} for {hits[0]['item_num']} -> {path}"


def todo_entry(g, nid, note):
    n = g.nodes[nid]
    item = g.nodes[n.parent] if n.kind == "comment" else n
    e = {"id": f"{int(time.time() * 1000)}", "created": datetime.now().isoformat(timespec="minutes"),
         "repo": item.repo, "item": item.id, "item_num": g.label_num(item), "title": item.title or "",
         "url": item.url or "", "note": note, "done": False}
    if n.kind == "comment":
        e.update({"comment": n.id, "comment_url": n.url or "", "comment_author": n.author, "comment_when": rel_days(n, g),
                  "comment_text": n.summary or excerpt(n.body, 160)})
    return e


# --------------------------------------------------------------------------
# local data: what is stored, listing, clearing, automatic hygiene
# --------------------------------------------------------------------------
CACHE_KINDS = {   # filename prefix -> (group, purpose)
    "items__": ("items", "issues/PRs with bodies and comments of one repo (fetched from GitHub; refreshed after --max-age)"),
    "stubs__": ("items", "titles/bodies of referenced items of one repo (closed ones, other repos)"),
    "translations.json": ("ai", "title/excerpt translations"),
    "translations_full.json": ("ai", "full-text translations (i)"),
    "summaries.json": ("ai", "one-line summaries of comments and items"),
    "whys.json": ("ai", "link reasons"),
    "tui.log": ("logs", "tui stderr / progress log"),
    "reviews__": ("review", "AI review findings of one repo's PRs, with what was posted / ignored / disproved"),
    "accounts.json": ("state", "which gh account can see which repo (saves a round trip on start-up)"),
    "state.json": ("state", "what the tui shows now (read by gg mcp)"),
    "cmd.json": ("state", "command from gg mcp to the tui"),
    "cmd_result.json": ("state", "its result"),
}
ITEMS_KEEP_DAYS = 30        # repo data not touched for this long is deleted at start-up
LOG_MAX = 1_000_000         # tui.log is cut back to its last half beyond this
AI_MAX_ENTRIES = 20000      # per AI cache file; oldest entries are dropped beyond this


def cache_kind(name):
    for prefix, kd in CACHE_KINDS.items():
        if name.startswith(prefix):
            return kd
    return ("other", "")


def cache_files():
    out = []
    if not os.path.isdir(CACHE_DIR):
        return out
    for name in sorted(os.listdir(CACHE_DIR)):
        path = os.path.join(CACHE_DIR, name)
        if os.path.isfile(path):
            st = os.stat(path)
            out.append((name, path, st.st_size, st.st_mtime, st.st_atime) + cache_kind(name))
    for repo, number, path, mtime in worktree_entries():          # directories, and by far the biggest
        out.append((f"worktrees/{repo}#{number}", path, dir_size(path), mtime, mtime,
                    "review", "git worktree of a PR head, checked out for reviewing it"))
    return out


def secure(path):
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def cache_hygiene():
    """Called at start-up: private permissions, drop repo data unused for ITEMS_KEEP_DAYS, cap logs and AI caches."""
    try:
        os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
        os.chmod(CACHE_DIR, 0o700)
    except OSError:
        return
    now = time.time()
    prune_worktrees()
    for name, path, size, mtime, atime, group, _ in cache_files():
        if os.path.isdir(path):
            continue
        secure(path)
        if group == "items" and now - max(mtime, atime) > ITEMS_KEEP_DAYS * 86400:
            try:
                os.remove(path)
                log(f"cache: removed {name} (unused for {ITEMS_KEEP_DAYS} days)")
            except OSError:
                pass
        elif name == "tui.log" and size > LOG_MAX:
            try:
                with open(path, "rb") as f:
                    f.seek(size - LOG_MAX // 2)
                    tail = f.read()
                with open(path, "wb") as f:
                    f.write(tail)
            except OSError:
                pass
        elif group == "ai" and size > 2_000_000:
            d = read_json(path)
            if isinstance(d, dict) and len(d) > AI_MAX_ENTRIES:
                keep = list(d.items())[len(d) - AI_MAX_ENTRIES:]      # dicts keep insertion order: oldest first
                write_json(path, dict(keep))
                secure(path)
    cfg_dir = os.path.dirname(CONFIG_PATH)
    if os.path.isdir(cfg_dir):
        try:
            os.chmod(cfg_dir, 0o700)
            for n in os.listdir(cfg_dir):
                secure(os.path.join(cfg_dir, n))
        except OSError:
            pass


def cache_cmd(args):
    """gg cache                 what is stored where, sizes, ages
       gg cache clear all|items|ai|logs|<owner/name>"""
    home = os.path.expanduser("~")
    files = cache_files()
    if args and args[0] == "clear":
        what = args[1] if len(args) > 1 else "all"
        n = 0
        for name, path, size, mtime, atime, group, _ in files:
            hit = (what == "all" or what == group or
                   (what not in ("all", "items", "ai", "logs", "state", "review") and
                    group in ("items", "review") and what.replace("/", "__") in name.replace("#", "__")))
            if not hit:
                continue
            if os.path.isdir(path):
                repo_, _, num_ = name.split("/", 1)[1].partition("#")
                clone_ = find_checkout(repo_)
                drop_worktree(path, clone_)
                drop_pr_refs(clone_, repo_, num_)
            else:
                os.remove(path)
            n += 1
        print(f"removed {n} item(s) from {CACHE_DIR.replace(home, '~')}")
        return 0
    if not files:
        print(f"nothing cached in {CACHE_DIR.replace(home, '~')}")
        return 0
    now, total = time.time(), 0
    print(f"{CACHE_DIR.replace(home, '~')}  (files are 0600, dir 0700; repo data unused for {ITEMS_KEEP_DAYS} days is removed at start-up)\n")
    print(f"{'group':6} {'size':>8} {'age':>6}  file — purpose")
    for name, path, size, mtime, atime, group, purpose in files:
        total += size
        age = now - mtime
        ages = f"{int(age // 86400)}d" if age >= 86400 else f"{int(age // 3600)}h" if age >= 3600 else f"{int(age // 60)}m"
        print(f"{group:6} {fmt_bytes(size):>8} {ages:>6}  {name} — {purpose}")
    print(f"\ntotal {fmt_bytes(total)}.  Also: {CONFIG_PATH.replace(home, '~')} (settings), "
          f"{TODO_JSON.replace(home, '~')} + {todo_md_path().replace(home, '~')} (marks).")
    print("clear: gg cache clear all | items | ai | logs | review | owner/name")
    return 0


def fmt_bytes(n):
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


# --------------------------------------------------------------------------
# live state for other tools (Claude in another window) + commands back into the tui
# --------------------------------------------------------------------------
STATE_PATH = os.path.join(CACHE_DIR, "state.json")
CMD_PATH = os.path.join(CACHE_DIR, "cmd.json")
CMD_RESULT_PATH = os.path.join(CACHE_DIR, "cmd_result.json")


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    secure(path)


def send_cmd(cmd, wait=3.0):
    """Ask a running gg tui to do something (open / mark); returns its result or None when no tui answered."""
    st = read_json(STATE_PATH) or {}
    if time.time() - st.get("updated_ts", 0) > 30:
        return None                                  # no tui has written state recently
    try:
        os.remove(CMD_RESULT_PATH)
    except OSError:
        pass
    cmd["req"] = f"{time.time():.3f}"
    write_json(CMD_PATH, cmd)
    end = time.time() + wait
    while time.time() < end:
        res = read_json(CMD_RESULT_PATH)
        if res and res.get("req") == cmd["req"]:
            return res
        time.sleep(0.1)
    return None


def state_text():
    st = read_json(STATE_PATH)
    if not st:
        return "gg tui is not running (no state file)."
    age = int(time.time() - st.get("updated_ts", 0))
    lines = [f"gg tui state ({age}s ago{'; the tui may have exited' if age > 60 else ''}):",
             f"repos: {', '.join(st.get('repos', []))}   viewing as: {', '.join('@' + m for m in st.get('me', []))}",
             f"focused panel: {st.get('focus')}   Inbox tab: {st.get('inbox_tab')}"]
    rv = st.get("review")
    if rv:
        c = rv.get("counts") or {}
        lines.append(f"REVIEW MODE on {rv['repo']}#{rv['number']} ({rv.get('status')}), head {rv.get('head_oid', '')[:7]}"
                     + (f" — {rv['error']}" if rv.get("error") else ""))
        lines.append(f"  files: {', '.join(rv.get('files') or []) or '-'}   showing: {rv.get('file') or '-'}")
        lines.append("  findings: " + (", ".join(f"{k} {v}" for k, v in c.items() if v) or "none"))
        f = rv.get("finding")
        if f:
            lines.append(f"  cursor finding: [{f['severity']}/{f.get('verdict') or 'unverified'}] {f['title']}"
                         f"  {f['path']}:{f['line']}\n  {f.get('body', '')[:600]}")
    it = st.get("item")
    if it:
        lines.append(f"current item: {it['label']} — {it['title']}  ({it['url']})")
    sub = st.get("subject")
    if sub and sub.get("id") != (it or {}).get("id"):
        lines.append(f"on screen (main): {sub['label']}: {sub.get('text', '')[:200]}  ({sub.get('url', '')})")
    row = st.get("cursor_row")
    if row:
        lines.append(f"cursor row: {row}")
    if st.get("answer"):
        lines.append("last answer in gg:\n" + st["answer"][:1500])
    lines.append(f"marks (todo): {st.get('todo_open', 0)} open — gg_todo for the list")
    return "\n".join(lines)


MCP_TOOLS = [
    {"name": "gg_state", "description": "What the user is looking at right now in gg tui: repo, current issue/PR, the "
                                         "comment or item shown in the main panel, focused panel, Inbox tab, last answer. In review mode it "
                                         "reports the PR under review, its files, finding counts and the finding at the cursor.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "gg_context", "description": "Full material for what is on screen in gg (or a given id): body, metadata, "
                                           "the whole comment thread in order, linked issues/PRs with the sentence that made "
                                           "each link. Use before answering questions about it. While gg is in review mode, "
                                           "finding:<fid> gives one finding in full and file:<path> the reviewed file from the PR worktree.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string", "description": "777 / owner/repo#777 / @login / finding:<fid> / file:<path>; default: what gg shows"}}}},
    {"name": "gg_todo", "description": "The user's marks made with m in gg tui (their next-work list with notes), as markdown.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "gg_show", "description": "Details of one issue/PR: every edge with the referencing sentence, comments, body.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}},
    {"name": "gg_graph", "description": "ASCII graph of how open issues/PRs/comments link together (tree), optionally around one item.",
     "inputSchema": {"type": "object", "properties": {"root": {"type": "string"}, "hops": {"type": "integer"}}}},
    {"name": "gg_open", "description": "Make gg tui show this issue/PR (its Item/Comments/Links panels follow).",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}},
    {"name": "gg_mark", "description": "Mark an issue/PR (or a comment url) in the user's gg todo list with a note.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}, "note": {"type": "string"}}}},
    {"name": "gg_todo_done", "description": "Tick off (or remove) a mark in the user's gg todo list once you have handled it. "
                                             "id: the item number/id or comment url as shown by gg_todo.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"},
                                                                          "remove": {"type": "boolean", "description": "delete instead of ticking off (default false)"}}}},
]


def review_context(st, want):
    """gg_context material for `finding:<fid>` / `file:<path>` of the review gg has open, else None."""
    rv = st.get("review")
    if not rv:
        return None
    if not want:
        want = "finding:" + (rv.get("finding") or {}).get("fid", "") if rv.get("finding") else ""
    if want.startswith("finding:"):
        f = rv.get("finding") or {}
        if f.get("fid") != want[8:]:
            return (f"gg is reviewing {rv['repo']}#{rv['number']} but the cursor is not on {want} — "
                    "ask the user to select it, or pass file:<path>")
        review = load_reviews(rv["repo"]).get(str(rv["number"])) or {}
        cached = (review.get("reviews") or {}).get(rv.get("head_oid")) or {}
        full = next((x for x in cached.get("findings") or [] if x.get("fid") == f["fid"]), f)
        out = [f"[finding {full.get('severity')}/{full.get('verdict') or 'unverified'}] {full.get('title')}",
               f"{rv['repo']}#{rv['number']}  {full.get('path')}:{full.get('line')} ({full.get('side')})",
               "", full.get("body") or ""]
        if full.get("evidence"):
            out += ["", "evidence: " + full["evidence"]]
        if full.get("diff"):
            out += ["", "suggested fix:", full["diff"]]
        return "\n".join(out)
    if want.startswith("file:"):
        path = want[5:]
        wt = worktree_path(rv["repo"], rv["number"])
        full = os.path.join(wt, path)
        head = f"[review file] {rv['repo']}#{rv['number']}  {path}\nworktree: {wt}"
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                return head + "\n\n" + fh.read()[:200000]
        except OSError as e:
            return f"{head}\n\ncannot read it: {e}"
    return None


def mcp_call(name, a):
    a = a or {}
    st = read_json(STATE_PATH) or {}
    repos = st.get("repos") or resolve_repos(None)
    if name == "gg_state":
        return state_text()
    if name == "gg_todo":
        entries = load_todo()
        return render_todo_md(entries) if entries else "nothing marked yet"
    if name == "gg_graph":
        rows, _ = graph_rows(repos=repos, root=a.get("root"), hops=int(a.get("hops", 2)), translate="none")
        return "\n".join(r.text for r in rows)
    g = build_graph(repos, "open", 15)
    if name == "gg_context":
        ctx = review_context(st, a.get("id"))
        if ctx:
            return ctx
        nid = resolve_root(g, a["id"]) if a.get("id") else ((st.get("subject") or st.get("item") or {}).get("id"))
        if not nid or nid not in g.nodes:
            return "nothing on screen; pass an id"
        kind, label, text = ask_context(g, nid)
        return f"[{kind}] {label}\n\n{text}"
    if name == "gg_show":
        return render_show(g, resolve_root(g, a["id"]))
    if name == "gg_open":
        nid = resolve_root(g, a["id"])
        res = send_cmd({"op": "open", "id": nid})
        return res.get("msg", "done") if res else f"gg tui is not running; {nid} exists (gg {a['id']} opens it)"
    if name == "gg_todo_done":
        res = send_cmd({"op": "done", "id": a["id"], "remove": bool(a.get("remove"))})
        return res.get("msg", "done") if res else todo_finish(a["id"], bool(a.get("remove")))
    if name == "gg_mark":
        nid = resolve_root(g, a["id"])
        res = send_cmd({"op": "mark", "id": nid, "note": a.get("note", "")})
        if res:
            return res.get("msg", "marked")
        entries = load_todo()
        entries.append(todo_entry(g, nid, a.get("note", "")))
        return f"marked {nid} -> {save_todo(entries)}"
    raise ValueError(f"unknown tool {name}")


def mcp_serve():
    """Minimal MCP stdio server (JSON-RPC over newline-delimited stdin/stdout); no dependencies."""
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                                                          "capabilities": {"tools": {}},
                                                          "serverInfo": {"name": "gg", "version": VERSION},
                                                          "instructions": (
                                                              "gg is the user's GitHub issue/PR browser (tui). When the user refers to "
                                                              "'what I am looking at', 'this issue/PR/comment', 'in gg', or asks what to work "
                                                              "on next: call gg_state first, then gg_context for the full thread before "
                                                              "answering. gg_todo lists their marks (next work). gg_open changes what gg "
                                                              "shows; gg_mark adds a mark with a note — do these only when asked. "
                                                              "When you handle something from gg_todo (answer it, fix it, write the "
                                                              "reply), call gg_todo_done for that entry so the user's list stays clean; "
                                                              "use remove=true if they asked to delete handled marks.")}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": MCP_TOOLS}})
        elif method == "tools/call":
            try:
                text = mcp_call(params.get("name"), params.get("arguments"))
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}})
            except Exception as e:  # noqa: BLE001
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}})


CLAUDE_PROMPT = ("You are paired with the terminal tool gg (GitHub issue/PR graph) that I am using in another window. "
                 "Call the MCP tool gg_state to see what I am looking at, gg_context for its full thread and links, "
                 "gg_todo for my marks; gg_open / gg_mark let you drive gg. Start by telling me in one paragraph what "
                 "I am looking at and what needs doing.")


# --------------------------------------------------------------------------
# self-update
# --------------------------------------------------------------------------
def _run(cmd, **kw):
    log("$ " + " ".join(cmd))
    return subprocess.run(cmd, **kw)


def update():
    """Refresh this installation from GitHub, whichever way it was installed."""
    here = os.path.realpath(__file__)
    d = os.path.dirname(here)
    old = VERSION
    if os.path.isdir(os.path.join(d, ".git")):
        log(f"git checkout at {d}")
        r = _run(["git", "-C", d, "pull", "--ff-only"])
        if r.returncode:
            return 1
    elif "/pipx/venvs/" in here or "/pipx/" in here:
        r = _run(["pipx", "upgrade", "gg-gitgraph"])
        if r.returncode:
            r = _run(["pipx", "install", "--force", f"git+{REPO_URL}"])
            if r.returncode:
                return 1
    elif "site-packages" in here:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", "--no-deps", f"git+{REPO_URL}"]
        if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
            cmd.insert(5, "--user")
        r = _run(cmd)
        if r.returncode:
            return 1
    else:
        import urllib.request
        log(f"single file at {here}: downloading {RAW_URL}")
        try:
            with urllib.request.urlopen(RAW_URL, timeout=60) as resp:
                data = resp.read()
        except Exception as e:  # noqa: BLE001
            log(f"download failed: {e}")
            return 1
        tmp = here + ".new"
        with open(tmp, "wb") as f:
            f.write(data)
        chk = subprocess.run([sys.executable, "-m", "py_compile", tmp], capture_output=True, text=True)
        if chk.returncode:
            log(f"downloaded file does not compile, keeping the current one: {chk.stderr.strip()[:200]}")
            os.remove(tmp)
            return 1
        os.chmod(tmp, os.stat(here).st_mode)
        os.replace(tmp, here)
    r = subprocess.run([sys.executable, here, "--version"], capture_output=True, text=True)
    new = (r.stdout or r.stderr).strip().replace("gg ", "") or "?"
    print(f"gg {old} -> {new}" + ("  (already up to date)" if new == old else ""))
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="tui",
                    help="tui [ROOT] (default) | ROOT (777 / #777 / owner/repo#777 / @login: tui on it) | graph [ROOT] "
                         "(text graph) | show ID | ask ID \"question\" | review PR | tutorial | update | config [KEY [VALUE]] | todo | check | mcp | cache [clear …]")
    ap.add_argument("arg", nargs="?", help="ID for show|ask, PR for review, initial root for tui")
    ap.add_argument("question", nargs="?", help="ask: the question")
    ap.add_argument("extra", nargs="*", help=argparse.SUPPRESS)
    ap.add_argument("--version", action="version", version=f"gg {VERSION}")
    ap.add_argument("--repo", "-r", action="append",
                    help="owner/name on github.com, or host/owner/name for GitHub Enterprise (repeatable)")
    ap.add_argument("--user", "-u", help="view as this GitHub login: 'me' for the tui home lists (my turn / mentions / "
                                         "mine / waiting). Only the perspective changes, not the gh login.")
    ap.add_argument("--layout", "-l", choices=["tree", "log"], default="tree")
    ap.add_argument("--state", choices=["open", "all"], default="open")
    ap.add_argument("--comments", choices=["linked", "all", "none"], default=None,
                    help="linked = only comments that reference #N/@someone (default for graph/show); "
                         "all = every comment (default for tui); none = fold comment links into the item")
    ap.add_argument("--no-people", action="store_true")
    ap.add_argument("--no-closed-neighbors", action="store_true")
    ap.add_argument("--root", help="777 | #777 | owner/repo#777 | @login")
    ap.add_argument("--hops", type=int, default=2)
    ap.add_argument("--max-age", type=float, default=15, help="cache TTL minutes")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--width", "-w", type=int, default=60)
    ap.add_argument("--translate", "-t", choices=["zh", "all", "none"], default=DEFAULT_TRANSLATE,
                    help=f"translate titles/excerpts to {TR_LANG} via claude -p (default: %(default)s)")
    ap.add_argument("--summary", "-S", action="store_true",
                    help="one-line comment summaries via claude -p (graph/show; tui has it on by default)")
    ap.add_argument("--no-summary", action="store_true", help="tui: keep comment first lines instead of summaries")
    ap.add_argument("--tour", action="store_true", help="tui: start with the guided tour")
    ap.add_argument("--no-tour", action="store_true", help="tui: never offer the first-run tour")
    ap.add_argument("--depth", type=int, default=1, help="tui: initial tree expansion depth (default 1)")
    ap.add_argument("--days", type=int, default=7, help="tui home: 'opened in the last N days' window (default 7)")
    ap.add_argument("--theme", choices=list(THEMES), help="colour theme (default: config/env, else dark)")
    ap.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                    help="ANSI colours (auto = when stdout is a terminal)")
    ap.add_argument("--json", action="store_true", help="review: the review as JSON on stdout")
    ap.add_argument("--print", action="store_true", dest="print_",
                    help="review: print the result instead of opening the TUI")
    ap.add_argument("--no-ai", action="store_true", help="review: the diff only, never call the AI CLI")
    ap.add_argument("--no-verify", action="store_true",
                    help="review: skip the pass that tries to disprove each finding")
    a = ap.parse_intermixed_args(argv)
    cache_hygiene()
    if a.theme:
        global THEME
        THEME = a.theme
    if a.user:
        ME[:] = [a.user.lstrip("@").lower()]
    if a.cmd not in ("graph", "tui", "show", "ask", "review", "update", "config", "todo", "check", "tutorial", "mcp", "cache", "ai") and ROOT_RE.match(a.cmd):
        a.arg, a.cmd = a.cmd, "tui"      # `gg 777` = tui starting on #777
    if a.cmd == "graph" and a.arg and ROOT_RE.match(a.arg) and not a.root:
        a.root = a.arg                   # `gg graph 777`
    if a.cmd == "tutorial":
        a.cmd = "tui"
        a.tour = True
    if a.cmd == "update":
        return update()
    if a.cmd == "config":
        return config_cmd([x for x in (a.arg, a.question) if x is not None] + (a.extra or []))
    if a.cmd == "check":
        try:
            return check_cmd(resolve_repos(a.repo, interactive=True))
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    if a.cmd == "mcp":
        mcp_serve()
        return 0
    if a.cmd == "ai":
        return ai_cmd([x for x in (a.arg,) if x])
    if a.cmd == "cache":
        return cache_cmd([x for x in (a.arg, a.question) if x is not None] + (a.extra or []))
    if a.cmd == "todo" and a.arg in ("done", "remove", "clear-done"):
        if a.arg == "clear-done":
            entries = [e for e in load_todo() if not e.get("done")]
            print(f"kept {len(entries)} open entr{'y' if len(entries) == 1 else 'ies'} -> {save_todo(entries)}")
            return 0
        if not a.question:
            ap.error(f"todo {a.arg} needs an id (750, #750, owner/name#750, a comment url)")
        print(todo_finish(a.question, remove=a.arg == "remove"))
        return 0
    if a.cmd == "todo":
        entries = load_todo()
        if not entries:
            print(f"nothing marked yet (press m in gg tui). file: {todo_md_path()}")
            return 0
        print(render_todo_md(entries), end="")
        print(f"\n<!-- {todo_md_path()} -->")
        return 0
    if a.cmd == "tui":
        try:
            repos = resolve_repos(a.repo, interactive=True)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        tui({"repos": repos, "state": a.state, "comments": a.comments or "all",
             "people": not a.no_people, "closed_neighbors": not a.no_closed_neighbors,
             "max_age_min": a.max_age, "width": a.width, "translate": a.translate,
             "layout": a.layout, "hops": a.hops, "root": a.arg or a.root, "summary": not a.no_summary,
             "depth": a.depth, "days": a.days, "start_tour": a.tour, "tutorial": not a.no_tour})
        return 0
    try:
        a.repo = resolve_repos(a.repo, interactive=True)
        a.comments = a.comments or "linked"
        if a.cmd == "ask":
            if not a.arg or not a.question:
                ap.error('ask needs an ID and a question: gg ask 4563 "why does it reference #3859?"')
            g = build_graph(a.repo, a.state, a.max_age, a.refresh)
            log(f"asking {model_label(ASK_MODEL)}…")
            print(ask_claude(g, resolve_root(g, a.arg), a.question))
        elif a.cmd == "review":
            if not a.arg:
                ap.error('review needs a PR: gg review 123 (also #123, owner/name#123, a pull request URL)')
            return do_review(a.arg, a.repo, refresh=a.refresh, as_json=a.json, no_ai=a.no_ai,
                             verify=not a.no_verify,
                             to_tui=not (a.print_ or a.json), opts=a,
                             color=a.color == "always" or (a.color == "auto" and sys.stdout.isatty()))
        elif a.cmd == "show":
            if not a.arg:
                ap.error("show needs an ID")
            print(do_show(a.arg, repos=a.repo, state=a.state, max_age_min=a.max_age, refresh=a.refresh,
                          translate=a.translate))
        elif a.cmd == "graph":
            rows, g = graph_rows(repos=a.repo, layout=a.layout, state=a.state, comments=a.comments,
                                 people=not a.no_people, closed_neighbors=not a.no_closed_neighbors,
                                 root=a.root, hops=a.hops, max_age_min=a.max_age, refresh=a.refresh,
                                 width=a.width, translate=a.translate, summary=a.summary)
            color = a.color == "always" or (a.color == "auto" and sys.stdout.isatty())
            print(ansi_rows(rows, g) if color else "\n".join(r.text for r in rows))
        else:
            ap.error(f"unknown command {a.cmd}")
    except (GhError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if USAGE["calls"]:
        log(usage_line())
    return 0


if __name__ == "__main__":
    sys.exit(main())
