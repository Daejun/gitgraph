#!/usr/bin/env python3
"""gg (gitgraph) - GitHub issue / PR / comment / @mention relation graph rendered as ASCII.

Usage:
  gg [777]                                the TUI (default), optionally starting on #777 (also owner/repo#777, @login)
  gg tutorial                             the TUI with the guided tour
  gg graph [777] [--hops 2]               text graph: overview of open items, or the neighbourhood of one item
  gg show 777                             details of one node
  gg ask 4563 "why does it mention #3859?"   # one-shot question to claude with the item as context
  gg update                               update this installation from GitHub
  gg config [KEY [VALUE]]                 show / set persistent settings (~/.config/gitgraph/config.json)
  gg todo                                 print the markdown of everything marked with m in the tui
  gg check [-r owner/name]                diagnose: gh accounts for the host, access, open counts, GraphQL fields
  gg mcp                                  MCP stdio server for Claude Code in another window (claude mcp add -s user gg -- gg mcp)
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

VERSION = "0.10.2"
REPO_URL = "https://github.com/Daejun/gitgraph"
RAW_URL = "https://raw.githubusercontent.com/Daejun/gitgraph/main/gitgraph.py"
CACHE_DIR = os.path.expanduser("~/.cache/gitgraph")
CONFIG_PATH = os.path.expanduser("~/.config/gitgraph/config.json")
# key -> (env var, default, help)
CONFIG_KEYS = {
    "claude_bin": ("GITGRAPH_CLAUDE", "claude", "binary used for translation / summaries / questions; a variant such as "
                                               "`cla` gets the same arguments except --model (its own default model is used)"),
    "repos": ("GITGRAPH_REPOS", "", "default repos, comma separated (owner/name or host/owner/name)"),
    "me": ("GITGRAPH_ME", "", "logins that count as \"me\", comma separated (default: gh accounts)"),
    "lang": ("GITGRAPH_LANG", "Korean", "language for translations, summaries and answers"),
    "translate": ("GITGRAPH_TRANSLATE", "zh", "zh | all | none"),
    "tr_model": ("GITGRAPH_TR_MODEL", "haiku", "model for translation / summaries (claude only)"),
    "ask_model": ("GITGRAPH_ASK_MODEL", "sonnet", "model for `a` / `gg ask` (claude only)"),
    "batch": ("GITGRAPH_BATCH", "10", "tui: nodes per translate/summary call"),
    "retries": ("GITGRAPH_RETRIES", "3", "gh api retries on transient network errors"),
    "theme": ("GITGRAPH_THEME", "dark", "colour theme: dark | light | basic (8 colours, no dim — e.g. PuTTY)"),
    "todo_file": ("GITGRAPH_TODO", "~/gitgraph-todo.md", "markdown written from the marks made with m in the tui (for the next session)"),
    "side_width": ("GITGRAPH_SIDE_WIDTH", "0.4", "tui: fraction of the width for the side column"),
    "expand_focused": ("GITGRAPH_EXPAND_FOCUSED", "true", "tui: give the focused side panel more height (accordion)"),
    "expanded_weight": ("GITGRAPH_EXPANDED_WEIGHT", "2", "tui: how much taller the focused side panel is"),
    "screen_mode": ("GITGRAPH_SCREEN_MODE", "normal", "tui: normal | half | full (+ / _ cycle at runtime)"),
    "border": ("GITGRAPH_BORDER", "rounded", "tui: rounded | single | double | bold | hidden"),
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


def config_cmd(args):
    """gg config            show everything and where each value comes from
       gg config KEY VALUE  store VALUE in ~/.config/gitgraph/config.json
       gg config KEY        show one value
       gg config unset KEY  remove it from the file"""
    if not args:
        for k, (env, default, help_) in CONFIG_KEYS.items():
            src = "env" if os.environ.get(env) else ("config" if CONFIG.get(k) else "default")
            print(f"{k:12} = {cfg(k) or '(empty)':24} [{src:7}]  {help_}")
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


def github_remotes(d):
    """[(rank, repo)] for every remote of the git repo at d whose URL points at a GitHub host.
    rank: 0 = origin, 1 = a remote named github*, 2 = anything else."""
    r = subprocess.run(["git", "-C", d, "remote", "-v"], capture_output=True, text=True)
    if r.returncode != 0:
        return []
    out, seen = [], set()
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2 or (len(parts) > 2 and parts[2] != "(fetch)"):
            continue
        name, url = parts[0], parts[1]
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
        if parent and parent not in out:
            log(f"{repo} is a fork of {parent}; using {parent} (pass -r {repo} to look at the fork itself)")
            out.append(parent)
        elif repo not in out:
            out.append(repo)
    return out


def resolve_repos(explicit=None, interactive=False):
    """-r > $GITGRAPH_REPOS > repos found under cwd (ask if several; forks -> their parent)."""
    if explicit:
        return list(explicit)
    if ENV_REPOS:
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
        return unfork([cands[0][0]])
    if interactive:
        return unfork(choose_repos(cands))
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


TRANSIENT_RE = re.compile(r"TLS handshake timeout|connection reset|i/o timeout|timeout|EOF|"
                          r"temporarily unavailable|no such host|HTTP 5\d\d|502|503|504", re.I)
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


def graphql(query, variables=None, host=DEFAULT_HOST):
    """Run a GraphQL query through `gh api graphql` against `host` (github.com or a GitHub Enterprise host).

    If the active account gets NOT_FOUND (private repo not visible), retry
    with the other accounts registered for that host; the account that works
    is moved to the front for the rest of the process.
    """
    accts = gh_accounts(host) or [None]
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
            if i:
                accts.insert(0, accts.pop(i))
            return data.get("data", {})
        types = {e.get("type") for e in errs}
        partial = data.get("data") or {}
        if errs and types <= {"NOT_FOUND"} and any(v is not None for v in partial.values()):
            # partial NOT_FOUND (e.g. one alias in a stub batch): usable; a null repository is not
            if i:
                accts.insert(0, accts.pop(i))
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
  repository(owner:$owner,name:$name){ issues(first:100, after:$after, states:$states){
    pageInfo{hasNextPage endCursor} nodes{ number updatedAt } } } }
"""
Q_LIST_PR = """
query($owner:String!,$name:String!,$after:String,$states:[PullRequestState!]) {
  repository(owner:$owner,name:$name){ pullRequests(first:100, after:$after, states:$states){
    pageInfo{hasNextPage endCursor} nodes{ number updatedAt } } } }
"""
ITEM_BATCH = 10


def list_open(repo):
    """{(is_pr, number): updatedAt} for every open issue and PR — a light query (no bodies)."""
    host, owner, name = split_repo(repo)
    out = {}
    for q, is_pr in ((Q_LIST, False), (Q_LIST_PR, True)):
        after = None
        while True:
            d = graphql(q, {"owner": owner, "name": name, "after": after, "states": ["OPEN"]}, host)
            conn = (d.get("repository") or {}).get("pullRequests" if is_pr else "issues")
            if conn is None:
                raise GhError(f"{repo}: repository not found on {host}")
            for n in conn["nodes"]:
                out[(is_pr, n["number"])] = n["updatedAt"]
            if not conn["pageInfo"]["hasNextPage"]:
                break
            after = conn["pageInfo"]["endCursor"]
    return out


def fetch_items(repo, is_pr, numbers):
    """Full records (bodies, comments, cross references) of the given issue or PR numbers, ITEM_BATCH per query."""
    host, owner, name = split_repo(repo)
    fields = PR_FIELDS if is_pr else ISSUE_FIELDS
    kind = "pullRequest" if is_pr else "issue"
    items = []
    for i in range(0, len(numbers), ITEM_BATCH):
        batch = numbers[i:i + ITEM_BATCH]
        aliases = " ".join(f"n{n}: {kind}(number:{n}){{ {fields} }}" for n in batch)
        d = graphql(f'query {{ repository(owner:"{owner}", name:"{name}") {{ {aliases} }} }}', host=host)
        rep_ = d.get("repository") or {}
        for n in batch:
            node = rep_.get(f"n{n}")
            if node:
                items.append(_norm_item(repo, node, is_pr))
        progress("fetch", len(items), len(numbers), f"{repo}: changed items")
    return items


def refresh_items(repo, cached):
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
            for it in fetch_items(repo, is_pr, nums):
                fresh[(it["is_pr"], it["number"])] = it
    items = [fresh.get(k) or by_key[k] for k in listing if k in fresh or k in by_key]
    items.sort(key=lambda it: it["created"], reverse=True)
    return items, len(changed), len(dropped)


def fetch_repo(repo, state):
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


def load_items(repo, state, max_age_min, refresh=False):
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
            items, _, _ = refresh_items(repo, cached)
            with open(p, "w") as f:
                json.dump({"fetched_at": time.time(), "repo": repo, "state": state, "items": items}, f)
            secure(p)
            return items, time.time()
        except GhError as e:
            log(f"{repo}: incremental refresh failed ({e}); fetching everything")
    items = fetch_repo(repo, state)
    with open(p, "w") as f:
        json.dump({"fetched_at": time.time(), "repo": repo, "state": state, "items": items}, f)
    secure(p)
    return items, time.time()


def resolve_stubs(repo, numbers, max_age_min):
    """Look up title/state for referenced-but-unfetched items, cached per repo."""
    p = _cache_path("stubs", repo)
    cache = {}
    if os.path.exists(p):
        with open(p) as f:
            cache = json.load(f)
    now = time.time()
    need = [n for n in numbers if str(n) not in cache
            or now - cache[str(n)].get("fetched_at", 0) > max_age_min * 60]
    host, owner, name = split_repo(repo)
    for i in range(0, len(need), 50):
        batch = need[i:i + 50]
        progress("stubs", i, len(need), repo)
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
            break
        rep = (data or {}).get("repository") or {}
        for n in batch:
            s = rep.get(f"n{n}")
            if s:
                cache[str(n)] = {"fetched_at": now, "is_pr": s["__typename"] == "PullRequest",
                                 "title": s.get("title"), "state": s.get("state"),
                                 "draft": s.get("isDraft", False), "created": s.get("createdAt"),
                                 "author": _login(s.get("author")), "body": (s.get("body") or "")[:SUM_BODY_CHARS]}
            else:
                cache[str(n)] = {"fetched_at": now, "missing": True}
    with open(p, "w") as f:
        json.dump(cache, f)
    return {int(k): v for k, v in cache.items() if str(k) in {str(n) for n in numbers}}


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


def claude_call(prompt, model, phase, timeout=300):
    """Run `claude -p` (Claude Code login, no API key); return the reply text and add its usage to USAGE."""
    # no --bare: bare mode skips the stored login and answers "Not logged in"
    cmd = [CLAUDE_BIN, "-p", "--no-session-persistence", "--output-format", "json"]
    if IS_CLAUDE:
        cmd += ["--model", model]          # a variant binary (gg config claude_bin cla) keeps its own default model
    try:
        r = subprocess.run(cmd + [prompt], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
                           start_new_session=True)   # no controlling terminal: the child cannot touch our screen
    except FileNotFoundError:
        raise ValueError(f"{CLAUDE_BIN} not found (gg config claude_bin …)") from None
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
        for (k, d), sm in zip(batch, arr):
            if isinstance(sm, str) and sm.strip():
                cache[f"{lang}:{k}"] = sm.strip()
                out[k] = sm.strip()
        with open(p, "w") as f:
            json.dump(cache, f, ensure_ascii=False)
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
    """Full-text translation of an item/comment body: prose only, code fences and log lines stay as they are.
    Cached in translations_full.json. Returns the text."""
    body = (n.body or "")[:TR_FULL_CHARS]
    if not body.strip():
        return ""
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = os.path.join(CACHE_DIR, "translations_full.json")
    cache = {}
    if os.path.exists(p):
        with open(p) as f:
            cache = json.load(f)
    key = f"{lang}:{hashlib.sha1(body.encode('utf-8')).hexdigest()}"
    if key in cache:
        return cache[key]
    kind = "comment" if n.kind == "comment" else ("pull request" if n.is_pr else "issue")
    pieces = _split_prose(body)
    prose = [t for is_p, t in pieces if is_p and t.strip()]
    if not prose:
        return body
    marked = "\n\n<<<SEG>>>\n\n".join(prose)
    tr = claude_call(TR_FULL_PROMPT.format(kind=kind, lang=lang, body=marked) +
                     "\n\n(The text contains <<<SEG>>> separators between independent parts: keep every separator "
                     "exactly, in the same order.)", TR_MODEL, "translate", timeout=600).strip()
    got = [x.strip() for x in tr.split("<<<SEG>>>")]
    if len(got) != len(prose):
        out = tr                      # separators lost: fall back to the plain translation
    else:
        it = iter(got)
        out = "\n".join((next(it) if (is_p and t.strip()) else t) for is_p, t in pieces)
    if out:
        cache[key] = out
        with open(p, "w") as f:
            json.dump(cache, f, ensure_ascii=False)
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
        for (k, _), txt in zip(batch, arr):
            if isinstance(txt, str) and txt.strip():
                cache[f"{lang}:{k}"] = txt.strip()
                out[k] = txt.strip()
        with open(p, "w") as f:
            json.dump(cache, f, ensure_ascii=False)
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
        for t, tr in zip(batch, arr):
            if isinstance(tr, str) and tr.strip():
                cache[f"{lang}:{t}"] = tr
                out[t] = tr
        with open(p, "w") as f:
            json.dump(cache, f, ensure_ascii=False)
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


def build_graph(repos, state, max_age_min, refresh=False):
    g = Graph(repos[0])
    fetched_at = None
    all_items = []
    for repo in repos:
        items, fa = load_items(repo, state, max_age_min, refresh)
        fetched_at = min(fetched_at, fa) if fetched_at else fa
        all_items.extend(items)

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
    for n in g.nodes.values():
        if n.kind == "item" and n.stub and (n.title is None or n.state is None):
            by_repo[n.repo].append(n.number)
    for repo, nums in by_repo.items():
        info = resolve_stubs(repo, sorted(set(nums)), max(max_age_min, 24 * 60))   # referenced items: a day
        for num, v in info.items():
            n = g.nodes[g.item_id(repo, num)]
            if v.get("missing"):
                continue
            n.is_pr, n.title, n.state = v["is_pr"], v["title"], v["state"]
            n.draft, n.created, n.author = v["draft"], v["created"], v["author"]
            n.body = v.get("body") or n.body
    g.finalize()
    g.fetched_at = fetched_at
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
# labels
# --------------------------------------------------------------------------
def trunc(s, n):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


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
    rel = f"{rel} " if rel else ""
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
        "people": ([39, 208, 42, 205, 226, 51, 141, 203, 118, 214, 81, 171, 190, 99, 209, 45], [1, 2, 3, 4, 5, 6]),
    },
    "light": {  # light background: darker tones, grey instead of dim
        "pre": (8, 8, False, False), "link": (8, 8, False, False), "meta": (8, 8, False, False),
        "stub": (8, 8, False, False), "pending": (8, 8, False, False), "head": (None, None, True, False),
        "out": (4, 4, False, False), "in": (5, 5, False, False), "closes": (4, 4, True, False), "closedby": (5, 5, True, False),
        "issue": (22, 2, True, False), "pr": (4, 4, True, False), "draft": (4, 4, False, False),
        "merged": (5, 5, True, False), "closed": (1, 1, True, False), "comment": (30, 6, False, False),
        "url": (4, 4, True, False), "fold": (130, 3, True, False), "md_h": (130, 3, True, False), "md_code": (240, 0, False, False), "md_quote": (8, 8, False, False), "md_bold": (None, None, True, False), "sum": (94, 3, False, False),
        "people": ([18, 88, 22, 90, 130, 24, 54, 94, 28, 124, 30, 91, 52, 58, 23, 89], [1, 2, 3, 4, 5, 6]),
    },
    "basic": {  # 8 colours only, no dim, no dark blue: PuTTY and other plain terminals
        "pre": (8, 8, False, False), "link": (8, 8, False, False), "meta": (8, 8, False, False),
        "stub": (8, 8, False, False), "pending": (8, 8, False, False), "head": (None, None, True, False),
        "out": (6, 6, False, False), "in": (5, 5, False, False), "closes": (6, 6, True, False), "closedby": (5, 5, True, False),
        "issue": (2, 2, True, False), "pr": (3, 3, True, False), "draft": (3, 3, False, False),
        "merged": (5, 5, True, False), "closed": (1, 1, True, False), "comment": (6, 6, False, False),
        "url": (6, 6, True, False), "fold": (7, 7, True, False), "md_h": (3, 3, True, False), "md_code": (7, 7, False, False), "md_quote": (8, 8, False, False), "md_bold": (None, None, True, False), "sum": (7, 7, False, False),
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
_DATE_HEAD = re.compile(r"^(?:\d{4}-\d\d-\d\d|\+\d+d) ")


def segments(row, g):
    t = row.text
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
    __slots__ = ("text", "nid", "jump", "kind")

    def __init__(self, text, nid=None, jump=None, kind=""):
        self.text, self.nid, self.jump, self.kind = text, nid, jump, kind
        # kind: "" node line | "head" section header | "link" cross-link line | "conn" log connector


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
  a               ask claude about the selection (answer tab in main)     d  details pager     o  open in browser
  i               translate the main content (issue/PR body or comment) in full; press again for the original
  C               open Claude Code in a tmux pane (or full screen) that can see what gg shows: it uses the
                  `gg mcp` server (register once: claude mcp add -s user gg -- gg mcp) — tools gg_state, gg_context,
                  gg_todo, gg_show, gg_graph, gg_open, gg_mark
  m               mark the selected issue/PR or comment for my next work and write a note; marked rows show ✎,
                  Home has a "todo" section, and ~/gitgraph-todo.md (gg config todo_file) is rewritten for the
                  next session (also `gg todo`). m again on a marked row: edit the note / mark done / remove
  Esc / b         back (previous item and perspective)     f  forward
  u               view Inbox as another person      r  refetch from GitHub
  F2              guided tour of the screen (also: gg tutorial)
  c t s p h       comments mode · translation · summaries · people nodes · hops (for the CLI tree / Links depth)
  / n N           search in the focused panel      T  colour theme      $  token usage      ?  this help     q  quit
  Hangul IME      shortcuts still work while the keyboard is in Hangul mode (ㅓ = j, ㅏ = k, 자 = w k …)
  mouse           click = focus a panel (its cursor stays); a click inside the focused panel selects the row;
                  click ‹ › in the Inbox title (or a tab name in Main's title) = switch tab;
                  double-click = Enter; click on a URL's text = open it in the browser;
                  wheel = scroll that panel without moving the cursor; back/forward buttons;
                  drag the border between the side column and main to resize (gg config side_width keeps it)
  O               options menu (comments / translation / summaries / people / hops / theme / screen)
  ?               key menu for the focused panel (Enter runs the action)     F1  this text

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
LIST_KINDS = ("", "link", "mention", "sec")


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
        hits = [i for i, r in enumerate(self.rows) if r.nid == nid and r.kind in ("", "mention")]
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
    HOME_TABS = [("turn", "my turn"), ("todo", "todo"), ("mention", "mentions"), ("opened", "opened"), ("active", "active"),
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
        self.show_tr = False   # main content: show the full-text translation (i toggles; runs claude on demand)
        self.last_side = "home"   # the side list panel that stays expanded while main is focused
        self.tr_thread, self.tr_pending = None, None
        self.collapsed = set()
        self.focus, self.screen = "home", cfg("screen_mode") if cfg("screen_mode") in ("normal", "half", "full") else "normal"
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
        }
        self.visible = []                  # panel keys drawn in the current layout
        self.title_zones = {}              # panel -> [(x0, x1, action)] clickable parts of its title bar
        self.msg, self.answer = "", None
        self.progress, self.worker, self.t0, self.bg_error = None, None, time.time(), None
        self.enriched = set()
        self.ask_thread, self.ask_state = None, None
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
        sys.stdout.flush()
        self.load(refresh=False)

    # ------------------------------------------------------------------ background work
    def on_progress(self, phase, done, total, detail=""):
        self.progress = {"phase": phase, "done": done, "total": total, "detail": detail}

    def run_bg(self, fn):
        import threading

        def body():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.bg_error = str(e)
                self.progress = {"phase": "error", "done": 0, "total": None, "detail": str(e)}

        self.t0, self.progress, self.bg_error = time.time(), None, None
        th = threading.Thread(target=body, daemon=True)
        th.start()
        return th

    def busy(self):
        return (self.worker is not None and self.worker.is_alive()) or \
               (self.ask_thread is not None and self.ask_thread.is_alive()) or \
               (self.tr_thread is not None and self.tr_thread.is_alive())

    def progress_text(self):
        sp = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time() * 8) % 10]
        if self.ask_thread is not None and self.ask_thread.is_alive():
            el = int(time.time() - self.ask_state["t0"])
            return f"{sp} asking {model_label(ASK_MODEL)} about {self.ask_state['label']}  {el}s"
        if self.tr_thread is not None and self.tr_thread.is_alive():
            el = int(time.time() - self.tr_pending[1])
            return f"{sp} translating {self.tr_pending[0]} in full ({model_label(TR_MODEL)})  {el}s"
        p = self.progress or {}
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
        h, w = scr.getmaxyx()
        lines = [f"gg — {what}", "", self.progress_text(), "", "q = quit"]
        for i, t in enumerate(lines):
            self.put(max(h // 2 - 2 + i, 0), max((w - dw(t)) // 2, 0), t, self.curses.A_BOLD if i == 0 else 0)
        scr.refresh()

    def load(self, refresh):
        self.g, self.worker, self.enriched = None, None, set()

        def work():
            self.g = build_graph(self.o["repos"], self.o["state"], self.o["max_age_min"], refresh)

        th = self.run_bg(work)
        self.scr.timeout(100)
        while th.is_alive():
            self.draw_loading("fetching from GitHub" if refresh else "loading")
            if self.scr.getch() == ord("q"):
                raise SystemExit
        self.scr.timeout(-1)
        self.scr.clear()
        if self.g is None:
            raise SystemExit(f"gg: cannot load the graph: {self.bg_error}")
        self.rebuild_graph()
        if self.o.get("root") and self.item is None:
            try:
                self.item = resolve_root(self.g, self.o["root"])
                self.subject = self.item
                self.focus = "main"
                self.panels["main"].tab = 0
            except ValueError as e:
                self.msg = str(e)
        self.refresh_all()
        if self.item is None:
            self.focus = "home"
        if not CONFIG.get("tutorial_done") and self.o.get("tutorial", True):
            if self.popup_menu("first run — take a 2-minute tour of the screen? (F2 later)", [("yes", True), ("no", False)]) is True:
                self.tutorial()
            else:
                CONFIG["tutorial_done"] = True
                save_config()
        if self.o.get("start_tour"):
            self.tutorial()

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
        if self.subject is None or self.subject not in self.g.nodes:
            self.subject = self.item
        self.enrich()

    def repo_rows(self):
        g = self.g
        n_items = sum(1 for n in g.nodes.values() if n.kind == "item" and not n.stub)
        fa = datetime.fromtimestamp(g.fetched_at).strftime("%H:%M") if g.fetched_at else "?"
        me = ",".join("@" + m for m in self.me) or "-"
        return [Row(f"{g.primary}  {n_items} open  {fa}  me={me}", kind="head"),
                Row(f"{THEME} c={self.o['comments']} t={self.o['translate']} s={'on' if self.o['summary'] else 'off'} "
                    f"h={self.o['hops']} | {usage_line().replace('tokens ', '')}", kind="head")]

    def item_rows(self):
        """The current item: label, metadata, one-line summary, url — Enter shows it in main."""
        g, w = self.g, self.o["width"]
        n = g.nodes.get(self.item)
        if not n:
            return [Row("(no current item — Enter on a row in Inbox)", kind="head")]
        n_links = sum(1 for r in self.panels["links"].rows if r.kind == "")
        meta = [f"updated {short_date(n.updated)}" if n.updated else "", ", ".join(n.labels),
                f"{n.comments_total} comments" if n.comments_total else "", f"{n_links} links" if n_links else ""]
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
                r.text += f"  ← @{src.author} {rel_days(src, g)} {what}"
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
                text += f"  · {e['note']}"
            r = Row(text, e["item"] if n else None, e.get("comment") if e.get("comment") in g.nodes else None,
                    "mention" if e.get("comment") in g.nodes else "")
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
            lines = wrap(self.answer or "(no answer yet — press a)", max(p.rect[3], 40))
        p.scroll_only = True
        rows, in_code = [], False
        for t in lines:
            if re.match(r"https?://\S+$", t):
                rows.append(Row(t, kind="url"))
                continue
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
                out.append(f"({PENDING_TEXT.replace('요약', '번역') if TR_LANG.lower().startswith('korean') else 'translating…'})")
        if body.strip():
            out.append("")
            out.extend(wrap(self.reflow(body), width))
        return out

    def translate_content(self):
        """i: toggle between the original and a full translation of the main content (translated on demand)."""
        nid = self.subject or self.item
        n = self.g.nodes.get(nid) if nid else None
        if not n or n.kind not in ("item", "comment") or not (n.body or "").strip():
            self.msg = "nothing to translate here"
            return
        if self.show_tr:
            self.show_tr = False
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
        import threading
        label = self.g.label_num(n) if n.kind == "item" else f"comment on {self.g.label_num(self.g.nodes[n.parent])}"
        self.tr_pending = (label, time.time(), nid)

        def work():
            try:
                n.tr_body = translate_body(n, self.g) or None
            except Exception as e:  # noqa: BLE001
                self.msg = f"translation failed: {e}"

        self.tr_thread = threading.Thread(target=work, daemon=True)
        self.tr_thread.start()
        self.refresh_main()

    # ------------------------------------------------------------------ selection / history
    def snapshot(self):
        return (self.item, self.subject, list(self.me), self.panels["main"].tab, self.panels["home"].tab, set(self.collapsed))

    def restore(self, st):
        self.item, self.subject, self.me, self.panels["main"].tab, self.panels["home"].tab, self.collapsed = st
        self.refresh_all()

    def set_item(self, nid, push=True):
        if not nid or nid not in self.g.nodes or self.g.nodes[nid].kind == "person":
            return
        if push:
            self.hist.append(self.snapshot())
            self.fwd = []
        self.item, self.subject, self.collapsed = nid, nid, set()
        self.panels["main"].top = 0
        self.refresh_all()

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
                if self.MAIN_TABS[self.panels["main"].tab] == "content":
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
        want_tr, want_sum = self.o["translate"] != "none", self.o["summary"]
        if not (want_tr or want_sum) or self.busy():
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
        ids = set(ids[:ENRICH_BATCH])
        whys = [pr for pr in getattr(self, "link_pairs", []) if pr in self.g.ctx and pr not in self.g.why
                and pr not in self.enriched] if want_sum else []
        whys = whys[:ENRICH_BATCH]
        if not ids and not whys:
            return
        parents = {self.g.nodes[i].parent for i in ids if self.g.nodes[i].kind == "comment"} - {None}
        sub = subgraph(self.g, ids | parents)
        mode = self.o["translate"]
        pending = [self.g.nodes[i] for i in ids if self.g.nodes[i].kind == "comment" and want_sum
                   and not self.g.nodes[i].summary and self.g.nodes[i].body.strip()]
        for n in pending:
            n.summary_pending = True

        self.why_pending = set(whys)

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
                self.why_pending = set()

        self.enriched |= ids | set(whys)
        self.worker = self.run_bg(work)
        if pending or whys:
            self.refresh_all()

    # ------------------------------------------------------------------ actions
    def enter(self):
        p = self.panels[self.focus]
        r = p.current()
        if not r:
            return
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

    def open_browser(self):
        url = self.node_url(self.subject or self.item)
        if not url:
            return
        try:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.msg = f"opened {url}"
        except OSError as e:
            self.msg = f"cannot open browser: {e}"

    def details(self):
        nid = self.subject or self.item
        if nid and nid in self.g.nodes:
            self.pager(render_show(self.g, nid, width=200).splitlines(), f"details: {nid}")

    # ------------------------------------------------------------------ popups (centred boxes over the screen)
    def popup_rect(self, want_h, want_w):
        h, w = self.scr.getmaxyx()
        bh, bw = min(want_h, h - 2), min(want_w, w - 2)
        return (h - bh) // 2, (w - bw) // 2, bh, bw

    def popup_frame(self, title, want_h, want_w):
        """Draw the current screen, then a centred box; returns the content rect (y, x, h, w)."""
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
        top, hs = 0, 0
        while True:
            y, x, hh, ww = self.popup_frame(title, len(lines) + 3, max(dw(l) for l in lines + [hint]) + 2 if lines else 40)
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
        top = 0
        while True:
            width = max([dw(l) for l, _ in items] + [dw(title) + 4]) + 6
            y, x, hh, ww = self.popup_frame(title, len(items) + 2, width)
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
        try:
            while True:
                y, x, hh, ww = self.popup_frame(label, 4, max(60, dw(label) + 4))
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
        """m: mark the selection for my next work, with a note; on a marked row: edit / done / remove."""
        nid = self.subject or self.item
        n = self.g.nodes.get(nid) if nid else None
        if not n or n.kind not in ("item", "comment"):
            self.msg = "select an issue, PR or comment first"
            return
        label = self.g.label_num(n) if n.kind == "item" else f"comment by @{n.author} on {self.g.label_num(self.g.nodes[n.parent])}"
        e = self.marked(nid)
        if e is None:
            note = self.popup_prompt(f"mark {label} — my note (Enter = none, Esc = cancel): ")
            if note is None:
                return
            self.todo.append(todo_entry(self.g, nid, note))
            path = save_todo(self.todo)
            self.msg = f"marked {label} → {path.replace(os.path.expanduser('~'), '~')}"
        else:
            choice = self.popup_menu(f"{label} is marked: {e.get('note') or '(no note)'}",
                                     [("edit the note", "edit"), ("mark done", "done"), ("remove the mark", "remove"), ("cancel", None)])
            if choice == "edit":
                e["note"] = self.popup_prompt("note: ", e.get("note") or "")
            elif choice == "done":
                e["done"] = True
            elif choice == "remove":
                self.todo.remove(e)
            else:
                return
            self.msg = f"todo updated → {save_todo(self.todo).replace(os.path.expanduser('~'), '~')}"
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
        self.panels["main"].tab = self.MAIN_TABS.index("answer")
        self.refresh_main()

        def work():
            try:
                st["answer"] = ask_claude(self.g, nid, q)
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

    def put_row(self, y, x, row, hs, width, extra=0):
        col, cx = 0, x
        for text, st in colorize_people(segments(row, self.g)):
            attr = self.style_attr(st) | extra
            buf = []
            for ch in text:
                cw = _cw(ch)
                if col + cw > hs + width:
                    break
                if col >= hs:
                    buf.append(ch)
                elif col + cw > hs:
                    buf.append(" ")
                col += cw
            if buf:
                sub = "".join(buf)
                try:
                    self.scr.addstr(y, cx, sub, attr)
                except self.curses.error:
                    pass
                cx += dw(sub)
        if extra and cx < x + width:
            try:
                self.scr.addstr(y, cx, " " * (x + width - cx), extra)
            except self.curses.error:
                pass

    def draw_box(self, key):
        p = self.panels[key]
        y, x, hh, ww = p.rect
        if ww == 0:
            return
        c = self.curses
        focused = key == self.focus
        tl, tr, bl, br, hz, vt = self.border
        attr = (self.style_attr("fold") | c.A_BOLD) if focused else self.dim()
        num = self.SIDE.index(key) + 1 if key in self.SIDE else 0
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
                for i, t in enumerate(p.tabs):
                    lab = f"[{t}]" if i == p.tab else t
                    zones.append((dw(title), dw(title) + dw(lab), ("tab", i)))
                    title += lab + " "
                title = title.rstrip()
        self.title_zones[key] = [(x - 1 + 2 + a, x - 1 + 2 + b, act) for a, b, act in zones]   # screen columns
            if key == "main":
                sub = self.g.nodes.get(self.subject) if self.subject else None
                what = (self.g.label_num(sub) if sub and sub.kind == "item" else
                        (f"comment on {self.g.label_num(self.g.nodes[sub.parent])}" if sub and sub.kind == "comment" else
                         (self.subject or "")))
                title += f"  · {what}"
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
                break
            r = p.rows[idx]
            extra = 0
            if not p.scroll_only and idx == p.cur:
                extra = c.A_REVERSE if focused else c.A_UNDERLINE
            self.put_row(y + i, x, r, p.hs, ww, extra)
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
        while True:
            y, x, hh, ww = self.popup_frame(title, len(lines) + 3, max(dw(l) for l in lines + [hint]) + 2)
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
        "repo": "r refetch  c t s p h toggles  T theme  $ tokens",
        "item": "⏎ read in main  m mark  i translate  a ask  o browser  d details",
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
                "repos": self.o["repos"], "me": self.me, "focus": self.focus,
                "inbox_tab": self.HOME_TABS[self.panels["home"].tab][1],
                "item": node_info(self.item), "subject": node_info(self.subject),
                "cursor_row": r.text.strip() if r else "", "answer": (self.answer or "")[:4000],
                "todo_open": sum(1 for e in self.todo if not e.get("done"))}

    def write_state(self):
        st = self.state_snapshot()
        sig = (st["item"], st["subject"], st["focus"], st["inbox_tab"], st["cursor_row"], st["me"], st["todo_open"], st["answer"])
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
            if cmd.get("op") == "open" and cmd.get("id") in self.g.nodes:
                self.set_item(cmd["id"])
                self.focus = "item"
                msg = f"gg now shows {self.g.label_num(self.g.nodes[cmd['id']])}"
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
        scr.erase()
        h, w = scr.getmaxyx()
        self.layout()
        widths = tuple(self.panels[k].rect[3] for k in self.SIDE + ["main"])
        if widths != getattr(self, "_widths", None):
            self._widths = widths
            self.refresh_all()          # "…" truncation follows the new panel widths
            self.layout()
        for key in self.visible:
            self.draw_box(key)
        for key in self.SIDE:                         # collapsed title bars in the tiny layout
            if key not in self.visible and self.panels[key].rect[3] and self.screen == "normal" and w > 84:
                self.draw_box(key)
        if self.busy():
            bottom = self.progress_text()
            attr = c.A_BOLD
        elif self.bg_error:
            bottom, attr = f"background work failed: {self.bg_error}", self.style_attr("closed")
        elif self.msg:
            bottom, attr = self.msg, c.A_BOLD
        else:
            bottom = (f"{self.HINTS.get(self.focus, '')}   1-6 0 Tab panels  + _ screen  b f back/fwd  ? keys  q quit"
                      if w >= 110 else f"{self.HINTS.get(self.focus, '')[:max(0, w - 30)]}  1-6 0 Tab  + _  ? q")
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
            self.scr.nodelay(True)
            try:
                for _ in range(n):
                    b = self.scr.getch()
                    if b == -1:
                        break
                    bs += bytes([b])
            finally:
                self.scr.nodelay(False)
            ch = bs.decode("utf-8", "replace")
            keys = hangul_keys(ch)
            if keys:
                for extra in reversed(keys[1:]):
                    c.ungetch(ord(extra))
                return ord(keys[0])
            return ord(ch) if len(ch) == 1 else k
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
        if b & 32:                                   # motion with a button held: drag the side/main border
            if self.dragging and w > 40:
                self.side_width = min(0.8, max(0.15, (x + 1) / w))
            return
        if not press:
            if self.dragging:
                self.dragging = False
                self.msg = f"side width {self.side_width:.2f}   (keep it: gg config side_width {self.side_width:.2f})"
            return
        base = b & ~28
        bx = self.border_x()
        if base == 0 and bx is not None and bx - 1 <= x <= bx + 1 and self.screen == "normal" and w > 84:
            self.dragging = True
            return
        key = self.panel_at(x, y)
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
                    self.switch_tab(key, act, relative=(key == "home"))
                    return
            self.focus = key
            return
        if key != self.focus:                    # first click on another panel: focus it, keep its cursor
            self.focus = key
            self.update_subject()
            self.last_click = (0.0, -1, "")
            return
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
        self.enrich()

    def cycle_focus(self, d):
        order = self.SIDE + ["main"]
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
        ("*", "m", "mark for my next work (with a note) / edit, done, remove", ord("m")),
        ("*", "C", "open Claude Code next to gg (tmux pane / full screen), connected via gg mcp", ord("C")),
        ("*", "d", "details", ord("d")), ("*", "o", "open in the browser", ord("o")),
        ("*", "b f", "back / forward", ord("b")), ("*", "u", "view Inbox as another person", ord("u")),
        ("*", "F2", "guided tour of the screen", 0),
        ("*", "/", "search in this panel", ord("/")), ("*", "O", "options menu (toggles)", ord("O")),
        ("*", "r", "refetch from GitHub", ord("r")), ("*", "T", "colour theme", ord("T")),
        ("*", "$", "token usage", ord("$")), ("*", "q", "quit", ord("q")),
        ("main", "K J", "scroll", ord("J")),
    ]

    def key_menu(self):
        items = [(f"{keys:6} {desc}", code) for ctx, keys, desc, code in self.KEYMENU if ctx in ("*", self.focus)]
        code = self.popup_menu(f"keys — {self.panels[self.focus].title} panel", items)
        if code is not None and code != ord("q"):
            self.handle_key(code)

    def options_menu(self):
        o = self.o
        items = [(f"comments: {o['comments']}  (cycle)", ord("c")),
                 (f"translation: {o['translate']}  (toggle)", ord("t")),
                 (f"summaries: {'on' if o['summary'] else 'off'}  (toggle)", ord("s")),
                 (f"people nodes: {'on' if o['people'] else 'off'}  (toggle)", ord("p")),
                 (f"hops: {o['hops']}  (cycle 1/2/3)", ord("h")),
                 (f"theme: {THEME}  (cycle)", ord("T")),
                 (f"screen mode: {self.screen}  (cycle)", ord("+")),
                 (f"side width: {self.side_width:.2f}  (drag the border with the mouse; gg config side_width)", None),
                 ("refetch from GitHub", ord("r"))]
        code = self.popup_menu("options", items)
        if code is not None:
            self.handle_key(code)

    def _run(self):
        c = self.curses
        while True:
            if self.worker is not None and not self.worker.is_alive():
                self.worker, self.progress = None, None
                self.refresh_all()
            if self.ask_thread is not None and not self.ask_thread.is_alive():
                self.ask_thread = None
                self.refresh_main()
            if self.tr_thread is not None and not self.tr_thread.is_alive():
                self.tr_thread = None
                self.refresh_main()
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
        # ---- panels ----
        if ord("1") <= k <= ord("6"):
            self.focus = self.SIDE[k - ord("1")]
            self.update_subject()
            return True
        if k == ord("0"):
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
        elif k == ord("C"):
            self.launch_claude()
        elif k == ord("i"):
            self.translate_content()
        elif k == ord("d"):
            self.details()
        elif k == ord("o"):
            self.open_browser()
        elif k == ord("u"):
            who = self.prompt_line("view as @login (empty = my gh accounts): ").lstrip("@")
            self.view_as([who] if who else (ME or [a.lower() for a in gh_accounts()]))
        elif k == ord("r"):
            if self.confirm("Refetch everything from GitHub?"):
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
                lines.append(f"  - note: {e['note']}")
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
    for name, path, size, mtime, atime, group, _ in cache_files():
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
                   (what not in ("all", "items", "ai", "logs", "state") and group == "items" and what.replace("/", "__") in name))
            if hit:
                os.remove(path)
                n += 1
        print(f"removed {n} file(s) from {CACHE_DIR.replace(home, '~')}")
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
    print("clear: gg cache clear all | items | ai | logs | owner/name")
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
                                         "comment or item shown in the main panel, focused panel, Inbox tab, last answer.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "gg_context", "description": "Full material for what is on screen in gg (or a given id): body, metadata, "
                                           "the whole comment thread in order, linked issues/PRs with the sentence that made "
                                           "each link. Use before answering questions about it.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string", "description": "777 / owner/repo#777 / @login; default: what gg shows"}}}},
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
]


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
                                                              "shows; gg_mark adds a mark with a note — do these only when asked.")}})
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
                         "(text graph) | show ID | ask ID \"question\" | tutorial | update | config [KEY [VALUE]] | todo | check | mcp | cache [clear …]")
    ap.add_argument("arg", nargs="?", help="ID for show|ask / initial root for tui")
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
    a = ap.parse_intermixed_args(argv)
    cache_hygiene()
    if a.theme:
        global THEME
        THEME = a.theme
    if a.user:
        ME[:] = [a.user.lstrip("@").lower()]
    if a.cmd not in ("graph", "tui", "show", "ask", "update", "config", "todo", "check", "tutorial", "mcp", "cache") and ROOT_RE.match(a.cmd):
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
    if a.cmd == "cache":
        return cache_cmd([x for x in (a.arg, a.question) if x is not None] + (a.extra or []))
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
