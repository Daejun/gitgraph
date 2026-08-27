#!/usr/bin/env python3
"""gg (gitgraph) - GitHub issue / PR / comment / @mention relation graph rendered as ASCII.

Usage:
  gg [overview options]                   render overview of open items
  gg 777 [--hops 2]                       neighbourhood of one item (also #777, owner/repo#777, @login)
  gg show 777                             details of one node
  gg tui [777]                            interactive curses browser (cursor keys, mouse, fold, focus, search, ask)
  gg update                               update this installation from GitHub
  gg config [KEY [VALUE]]                 show / set persistent settings (~/.config/gitgraph/config.json)

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

VERSION = "0.3.1"
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
        r = subprocess.run(["git", "-C", d, "remote", "get-url", "origin"], capture_output=True, text=True)
        m = _REMOTE_RE.match(r.stdout.strip()) if r.returncode == 0 else None
        if not m:
            continue
        repo = make_repo(m.group("host"), m.group("owner"), m.group("name"))
        rank = (d.count(os.sep), os.path.basename(d) != m.group("name"), d)
        if repo not in best or rank < best[repo][0]:
            best[repo] = (rank, d)
    return sorted(((repo, d) for repo, (rank, d) in best.items()), key=lambda x: best[x[0]][0])


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


def resolve_repos(explicit=None, interactive=False):
    """-r > $GITGRAPH_REPOS > repos found under cwd (ask if several) > built-in default."""
    if explicit:
        return list(explicit)
    if ENV_REPOS:
        return ENV_REPOS
    cands = discover_repos(os.getcwd())
    if not cands:
        raise ValueError(f"no GitHub repo found under {os.getcwd()} — pass -r owner/name or set GITGRAPH_REPOS")
    if len(cands) == 1:
        return [cands[0][0]]
    if interactive:
        return choose_repos(cands)
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
    return items


def _cache_path(kind, repo, state=""):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{kind}__{repo.replace('/', '__')}{'__' + state if state else ''}.json")


def load_items(repo, state, max_age_min, refresh=False):
    p = _cache_path("items", repo, state)
    if not refresh and os.path.exists(p):
        with open(p) as f:
            d = json.load(f)
        if time.time() - d["fetched_at"] < max_age_min * 60:
            return d["items"], d["fetched_at"]
    items = fetch_repo(repo, state)
    with open(p, "w") as f:
        json.dump({"fetched_at": time.time(), "repo": repo, "state": state, "items": items}, f)
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
            f'... on Issue{{ number title state createdAt author{{login}} }} '
            f'... on PullRequest{{ number title state isDraft createdAt author{{login}} }} }}'
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
                                 "author": _login(s.get("author"))}
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


def parse_refs(text, default_repo):
    out = []
    host = repo_host(default_repo)
    for line in _clean_lines(text):
        for m in URL_RE.finditer(line):
            num = int(m.group("num"))
            if num <= SMALL_REF and not _plausible_small_ref(line, m, is_url=True):
                continue
            h = m.group("host").lower()
            if h.startswith("www."):
                h = h[4:]
            out.append((make_repo(h, *m.group("repo").split("/", 1)), num))
        line = URL_RE.sub(" ", line)
        for m in REF_RE.finditer(line):
            num = int(m.group("num"))
            if num <= SMALL_REF and not _plausible_small_ref(line, m):
                continue
            out.append((qualify(m.group("repo"), host) if m.group("repo") else default_repo, num))
    seen, res = set(), []
    for r in out:
        if r[1] > 0 and r not in seen:
            seen.add(r)
            res.append(r)
    return res


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
        r = subprocess.run(cmd + [prompt], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
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
SUM_PROMPT = """Summarize each GitHub comment in the JSON array below in ONE line of {lang}, at most 70 characters. \
Say what the comment does: a finding, a question, a request, a decision, a status update, a measurement, an ack. \
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
ASK_MAX_CHARS = 60000
ASK_PROMPT = """You are helping a developer read a GitHub {kind}. Answer the question using only the material below; \
if it cannot be answered from it, say so. Answer in {lang}; keep identifiers, code, file names, #numbers and technical \
terms in English. Be concise (under 300 words) unless the question asks for more.

=== {label} ===
{context}

=== question ===
{question}"""


def ask_context(g, nid):
    """(kind, label, text): everything worth knowing about one node, capped at ASK_MAX_CHARS."""
    n = g.nodes[nid]
    if n.kind == "comment":
        p = g.nodes.get(n.parent)
        parts = [f"Comment by @{n.author} on {short_date(n.created)} ({rel_days(n, g)} after the item opened) "
                 f"[{n.ckind}{' ' + (n.review_state or '') if n.review_state else ''}]", n.body, ""]
        if p:
            parts += [f"--- the {'PR' if p.is_pr else 'issue'} it belongs to: {g.label_num(p)} {p.title} (by @{p.author}, "
                      f"{short_date(p.created)}, {p.state_label()}) ---", (p.body or "")[:3000]]
        return "comment", f"comment on {g.label_num(p) if p else '?'}", "\n".join(parts)
    if n.kind == "person":
        lines = [f"{n.id} is mentioned in:"]
        for m, t, o in sorted(g.adj[nid], key=lambda e: -g.nodes[e[0]].time):
            if t == "mention":
                src = g.nodes[m]
                lines.append(f"- {node_label(g, src, 200)}")
        return "person", n.id, "\n".join(lines)
    kind = "pull request" if n.is_pr else "issue"
    parts = [f"{g.label_num(n)} {n.title}", f"author @{n.author}, opened {short_date(n.created)}, state {n.state_label()}, "
             f"updated {short_date(n.updated)}, labels: {', '.join(n.labels) or '-'}", n.url or "", "", n.body or "(no body)"]
    for c in g.comments_of(nid):
        tag = f" [{c.ckind}{' ' + (c.review_state or '') if c.review_state else ''}]" if c.ckind != "comment" else ""
        parts += ["", f"--- comment by @{c.author}, {short_date(c.created)} ({rel_days(c, g)}){tag} ---", c.body[:6000]]
    text = "\n".join(parts)
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


def prepare_summaries(g):
    """Attach a one-line summary to every comment node of g. Returns the number summarized."""
    g.summarized = 0
    entries, targets = {}, defaultdict(list)
    for n in g.nodes.values():
        if n.kind != "comment" or not n.body.strip():
            continue
        key = hashlib.sha1(n.body.encode("utf-8")).hexdigest()
        if key not in entries:
            parent = g.nodes.get(n.parent)
            entries[key] = {"item": (parent.tr_title or parent.title) if parent else "",
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
        for repo, num in parse_refs(it["body"], it["repo"]):
            t = g.ensure_item(repo, num)
            g.add_edge(iid, t.id, "ref")
            parsed_pairs.add((iid, t.id))
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
            for repo, num in parse_refs(c["body"], it["repo"]):
                t = g.ensure_item(repo, num)
                g.add_edge(cid, t.id, "ref")
                parsed_pairs.add((iid, t.id))
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
        info = resolve_stubs(repo, sorted(set(nums)), max_age_min)
        for num, v in info.items():
            n = g.nodes[g.item_id(repo, num)]
            if v.get("missing"):
                continue
            n.is_pr, n.title, n.state = v["is_pr"], v["title"], v["state"]
            n.draft, n.created, n.author = v["draft"], v["created"], v["author"]
    g.finalize()
    g.fetched_at = fetched_at
    return g


def apply_filters(g, comments="linked", people=True, closed_neighbors=True):
    """Return a new Graph restricted per display options."""
    h = Graph(g.primary)
    h.fetched_at = g.fetched_at
    h.show_linked = comments != "none"
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
    h.fetched_at, h.show_linked = g.fetched_at, g.show_linked
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
STYLE_ANSI = {
    "pre": "\033[2m", "link": "\033[2m", "head": "\033[1m", "meta": "\033[2m", "stub": "\033[2m",
    "out": "\033[34m", "in": "\033[35m", "closes": "\033[1;34m", "closedby": "\033[1;35m",
    "issue": "\033[1;32m", "pr": "\033[1;94m", "draft": "\033[94m", "merged": "\033[1;35m", "closed": "\033[1;31m",
    "comment": "\033[36m", "person": "\033[33m", "fold": "\033[1;33m",
    "sum": "\033[38;5;180m", "pending": "\033[2;3m",
}
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
    if row.kind == "mention":
        i = t.find("  ← ")
        head = segments(Row(t[:i] if i >= 0 else t, row.nid), g)
        return head + ([(t[i:], "in")] if i >= 0 else [])
    segs = []
    m = _PRE_TREE.match(t) or _PRE_LOG.match(t)
    if m:
        segs.append((m.group(0), "pre"))
        t = t[m.end():]
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
        i = t.find("  ")
        if i >= 0:
            segs.append((t[:i], "stub" if n.stub else ""))
            segs.append((t[i:], "meta"))
        else:
            segs.append((t, "stub" if n.stub else ""))
    return [x for x in segs if x[0]]


PERSON_PALETTE_256 = [39, 208, 42, 205, 226, 51, 141, 203, 118, 214, 81, 171, 190, 99, 209, 45]
PERSON_PALETTE_8 = [1, 2, 3, 4, 5, 6]
PERSON_COLOR = {}   # login (lower) -> palette index, assigned in order of first appearance
_LOGIN_RE = re.compile(r"@[A-Za-z0-9][A-Za-z0-9-]*")


def person_index(login):
    key = login.lower().lstrip("@")
    if key not in PERSON_COLOR:
        PERSON_COLOR[key] = len(PERSON_COLOR)
    return PERSON_COLOR[key]


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


def ansi_style(st):
    if st.startswith("person:"):
        i = person_index(st[7:])
        if os.environ.get("TERM", "").endswith("256color") or os.environ.get("COLORTERM"):
            return f"\033[38;5;{PERSON_PALETTE_256[i % len(PERSON_PALETTE_256)]}m"
        return f"\033[3{PERSON_PALETTE_8[i % len(PERSON_PALETTE_8)]}m"
    return STYLE_ANSI.get(st, "")


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


HELP = """gg tui keys

  Up/k  Down/j    move one row            PgUp/PgDn  page       g / G  top / bottom
  Space           fold / unfold this node (tree layout)          - / +  fold to depth 1 / unfold all
  1..9            unfold to that depth (start-up depth is 1; --depth N changes it)
                  ▾ = expanded   ▸ = folded ([+N] = hidden descendants)   · = leaf
  Left / Right    fold / unfold
  Tab             home (my turn / mentions / opened / active / waiting / mine / PRs / stale / all)  <->  overview
                  home: Enter or Space on a section header folds it; - / + fold / unfold all sections
                  home: PgDn / PgUp jump to the next / previous section
  Enter           on a node: focus on it (re-root, N hops)
                  on a "⇢" or "mentions" line: jump to the linked node
  Backspace/b/Esc back to the previous view (with the preview or answer panel focused: just leave it)
  f               forward again        mouse: back/forward buttons = back/forward, right click = open in browser
  v               show / hide the preview pane (full text of the row under the cursor)
  w               focus the preview pane (it grows to most of the screen): Up/Down, PgUp/PgDn, g/G scroll; w/Esc returns
  J / K           scroll the preview pane without changing focus      { / }  shrink / grow it
  L               show / hide the legend box in the top-right corner
  a               ask claude a one-shot question about the row under the cursor (body + all comments as context);
                  the answer appears in a panel on the right half of the screen
  A               hide / show that answer panel        w  cycles focus list -> preview -> answer panel
  u               view the home lists as another person (@login); empty = back to my gh accounts
                  (the previous person is on the back stack: Backspace / back button restores them)
  d               details: edges, comments with their targets, body
  o               open in the browser (xdg-open)
  l               toggle tree / log         c  cycle comments linked/all/none   p  toggle people
  t               toggle translation        s  toggle comment summaries          H  cycle hops 1/2/3
  r               refetch from GitHub
  /  n  N         search / next / previous  < >  horizontal scroll
  mouse           click = move the cursor; click ▾/▸ = fold/unfold; click a section header = fold it;
                  double-click = Enter (on a @login: view as that person); right click = open in browser;
                  click preview / answer panel = focus it;
                  wheel = scroll that area without moving the cursor; back / forward buttons = back / forward
  $               claude token usage since start (translate / summarize / ask; also in the title bar)
  ?               this help                 q  quit
"""


class Tui:
    COMMENTS_CYCLE = ["linked", "all", "none"]

    def __init__(self, scr, opts):
        import curses
        self.curses = curses
        self.scr = scr
        self.o = dict(opts)
        self.root, self.hist, self.fwd = None, [], []
        self.collapsed, self.cur, self.top, self.hs = set(), 0, 0, 0
        self.query, self.msg, self.rows = "", "", []
        self.pane_on, self.pane_h, self.pv, self.pv_key = True, 0, 0, None   # preview pane
        self.pane_focus, self.legend_on = False, True
        self.home_folded = {"stale", "all"}
        self.ask_thread, self.ask_state, self.last_answer = None, None, None
        self.side, self.side_on, self.side_scroll, self.side_focus = None, False, 0, False   # right-hand answer panel
        self.o.setdefault("summary", True)
        self.tr_saved = self.o["translate"] if self.o["translate"] != "none" else "zh"
        self.progress, self.worker, self.enrich_pending, self.t0, self.bg_error = None, None, False, time.time(), None
        self.view = "graph" if (self.o.get("root") or not self.o.get("home", True)) else "home"
        self.enriched = set()   # node ids already sent for translation / summary (on demand, per view)
        self.me = ME or [a.lower() for a in gh_accounts()]
        global PROGRESS
        PROGRESS = self.on_progress
        curses.curs_set(0)
        scr.keypad(True)
        self.layout = {"body": 0, "pane": 0, "lw": 0, "sw": 0}
        self.mouse_ev, self.last_click = None, (0.0, -1)
        self.scroll_free = False   # True after a wheel scroll: the view may leave the cursor off screen
        sys.stdout.write("\033[?1000h\033[?1006h")   # press/release reports in SGR form; parsed in read_key()
        sys.stdout.flush()
        try:
            curses.start_color()
            curses.use_default_colors()
            for i, c in enumerate((curses.COLOR_GREEN, curses.COLOR_MAGENTA, curses.COLOR_RED,
                                   curses.COLOR_CYAN, curses.COLOR_YELLOW, curses.COLOR_BLUE), 1):
                curses.init_pair(i, c, -1)
            self.person_pal = PERSON_PALETTE_256 if curses.COLORS >= 256 else PERSON_PALETTE_8
            for i, c in enumerate(self.person_pal):
                curses.init_pair(20 + i, c, -1)
            curses.init_pair(7, 180 if curses.COLORS >= 256 else curses.COLOR_YELLOW, -1)   # summaries
        except curses.error:
            pass
        self.load(refresh=False)

    # ---- background work + progress ----
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
               (self.ask_thread is not None and self.ask_thread.is_alive())

    def progress_text(self):
        sp = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time() * 8) % 10]
        if self.ask_thread is not None and self.ask_thread.is_alive():
            el = int(time.time() - self.ask_state["t0"])
            return f"{sp} asking {model_label(ASK_MODEL)} about {self.ask_state['label']}: {trunc(self.ask_state['q'], 60)}  {el}s"
        p = self.progress or {}
        el = int(time.time() - self.t0)
        els = f"{el // 60}m{el % 60:02d}s" if el >= 60 else f"{el}s"
        names = {"fetch": "fetching issues/PRs", "stubs": "resolving referenced items",
                 "translate": f"translating titles ({model_label(TR_MODEL)})",
                 "summarize": f"summarizing comments ({model_label(TR_MODEL)})",
                 "error": "error"}
        phase, done, total = p.get("phase", "starting"), p.get("done", 0), p.get("total")
        if total:
            f = int(20 * min(done, total) / total)
            bar = f" [{'#' * f}{'.' * (20 - f)}] {done}/{total}"
        elif phase == "fetch":
            bar = f" {done} items"
        else:
            bar = ""
        return f"{sp} {names.get(phase, phase)}{bar}  {p.get('detail', '')}  {els}"

    def draw_loading(self, what):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        lines = [f"gg — {what}", "", self.progress_text(), "",
                 "first run: GitHub fetch takes seconds; translation/summary batches take about a minute each",
                 "(cached afterwards)   q = quit"]
        for i, t in enumerate(lines):
            self.put(max(h // 2 - 3 + i, 0), max((w - dw(t)) // 2, 0), t, self.curses.A_BOLD if i == 0 else 0)
        scr.refresh()

    # ---- data ----
    def load(self, refresh):
        self.g = None
        self.worker = None
        self.enriched = set()

        def work():
            self.g = build_graph(self.o["repos"], self.o["state"], self.o["max_age_min"], refresh)

        th = self.run_bg(work)
        self.scr.timeout(100)
        while th.is_alive():
            self.draw_loading("fetching from GitHub" if refresh else "loading")
            if self.scr.getch() == ord("q"):
                raise SystemExit
        self.scr.timeout(-1)
        if self.g is None:
            raise SystemExit(f"gg: cannot load the graph: {self.bg_error}")
        if self.o.get("root") and not self.root and not self.hist:
            try:
                self.root = resolve_root(self.g, self.o["root"])
            except ValueError as e:
                self.msg = str(e)
        self.rebuild()
        if self.view == "graph" and self.o["layout"] == "tree":
            self.fold_below(self.o.get("depth", 1))
        self.cur = 0
        self.move(1)

    def rebuild_graph(self):
        g2 = apply_filters(self.g, self.o["comments"], self.o["people"], self.o["closed_neighbors"])
        if self.root and self.root in g2.nodes:
            g2 = subgraph(g2, focus(g2, self.root, self.o["hops"]))
        else:
            self.root = None
        self.cg = g2

    def rebuild(self):
        """Recompute the displayed graph (filters / focus) and its rows; enrich in the background."""
        self.rebuild_graph()
        self.rebuild_rows()

    def enrich(self):
        """On-demand: translate / summarize only the nodes that are in the current rows and not done yet."""
        want_tr, want_sum = self.o["translate"] != "none", self.o["summary"]
        if not (want_tr or want_sum):
            return
        if self.busy():
            return  # picked up when the worker finishes (rows are rebuilt then)
        h, _ = self.scr.getmaxyx()
        onscreen = range(self.top, min(self.top + h, len(self.rows)))
        order = list(onscreen) + [i for i in range(len(self.rows)) if i not in onscreen]
        ids = []
        for i in order:   # what is on screen first, then the rest of the view, ENRICH_BATCH nodes per call
            r = self.rows[i]
            for nid in (r.nid, r.jump if r.kind == "mention" else None):
                if nid and nid in self.g.nodes and nid not in self.enriched and nid not in ids:
                    ids.append(nid)
        ids = set(ids[:ENRICH_BATCH])
        if not ids:
            return
        parents = {self.g.nodes[i].parent for i in ids if self.g.nodes[i].kind == "comment"} - {None}
        sub = subgraph(self.g, ids | parents)   # Node objects are shared, so results land on self.g too
        mode = self.o["translate"]
        pending = [self.g.nodes[i] for i in ids if self.g.nodes[i].kind == "comment" and want_sum
                   and not self.g.nodes[i].summary and self.g.nodes[i].body.strip()]
        for n in pending:
            n.summary_pending = True

        def work():
            try:
                prepare_translations(sub, mode)
                if want_sum:
                    prepare_summaries(sub)
            finally:
                for n in pending:
                    n.summary_pending = False

        self.enriched |= ids
        if os.environ.get("GG_DEBUG"):
            log(f"enrich: {len(ids)} ids, {len(pending)} pending summaries, rows={len(self.rows)} top={self.top}")
        self.worker = self.run_bg(work)
        if pending:
            self.rows = self.build_rows()   # same structure, only the "» 요약 중…" texts change

    def build_rows(self):
        if self.view == "home":
            return self.home_rows()
        if self.root:
            return focus_rows(self.cg, self.root, self.o["layout"], self.o["width"], self.collapsed,
                              marks=True, legend=False)
        return overview_rows(self.cg, self.o["layout"], self.o["width"], self.collapsed, marks=True, legend=False)

    def rebuild_rows(self, keep="auto"):
        """keep: node id to leave the cursor on; "auto" = the node under the cursor now; None = don't try."""
        if keep == "auto":
            keep = self.current_nid()
        self.rows = self.build_rows()
        self.cur = min(self.cur, max(len(self.rows) - 1, 0))
        if keep:
            self.goto(keep, quiet=True, unfold=False)
        if not self.valid(self.cur):
            self.move(1)
        self.enrich()

    # ---- home view ----
    def home_rows(self):
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

        def item_row(n, src=None):
            deg = item_degree(cg, n.id) if n.id in cg.nodes else 0
            text = item_label(g, n, w) + (f"  ⇢ {deg} links" if deg else "")
            r = Row(text, n.id)
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
        mine = newest(n for n in items if (n.author or "").lower() in me)
        others_prs = newest(n for n in items if n.is_pr and (n.author or "").lower() not in me)
        stale = by_update(n for n in items if ts(n.updated) < now - 30 * 86400)
        who = ", ".join("@" + m for m in self.me) or "(no gh account found; set GITGRAPH_ME)"
        sections = [
            ("turn", f"my turn — someone else spoke last on an item I am in", [item_row(n, lc) for n, lc in my_turn]),
            ("mention", f"mentioning {who}", [item_row(n, src) for n, src in mentioned]),
            ("opened", f"opened in the last {days} days", [item_row(n) for n in opened]),
            ("active", f"active in the last {days} days (updated, not newly opened)", [item_row(n, last_comment(n)) for n in active]),
            ("waiting", "waiting on others — I spoke last", [item_row(n, lc) for n, lc in waiting]),
            ("mine", "opened by me", [item_row(n) for n in mine]),
            ("prs", "open PRs by others", [item_row(n, last_comment(n)) for n in others_prs]),
            ("stale", "stale — no update for 30 days", [item_row(n) for n in stale]),
            ("all", f"all open items ({len(items)})", [item_row(n) for n in newest(items)]),
        ]
        rows = [Row(f"gg — {g.primary}" + (f"   (data fetched {datetime.fromtimestamp(g.fetched_at).strftime('%Y-%m-%d %H:%M')})"
                    if g.fetched_at else "") + "   open items only", kind="head"),
                Row("Enter/Space on a section = fold/unfold   Enter on an item = tree around it   Tab = full overview   ? = keys",
                    kind="head")]
        for key, title, body in sections:
            rows.append(Row(""))
            folded = key in self.home_folded
            mark = "▸" if folded else "▾"
            rows.append(Row(f"{mark} == {title}: {len(body)} ==" + (f"  [+{len(body)}]" if folded else ""), None, key, "sec"))
            if not folded:
                rows.extend(body)
        return rows

    # ---- rows / cursor ----
    def valid(self, i):
        return (0 <= i < len(self.rows) and self.rows[i].kind in ("", "link", "mention", "sec")
                and self.rows[i].text.strip() != "")

    def current_nid(self):
        return self.rows[self.cur].nid if self.rows and 0 <= self.cur < len(self.rows) else None

    def move(self, delta):
        self.scroll_free = False
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

    def find_row(self, nid, near=None):
        """Row index showing nid; when it appears several times (home sections), the one closest to `near`."""
        hits = [i for i, r in enumerate(self.rows) if r.nid == nid and r.kind in ("", "mention")]
        if not hits:
            return None
        return min(hits, key=lambda i: abs(i - near)) if near is not None else hits[0]

    def goto(self, nid, quiet=False, unfold=True):
        i = self.find_row(nid, near=self.cur if quiet else None)
        if i is None and unfold and self.collapsed:
            self.collapsed.clear()
            self.rebuild_rows(keep=None)
            i = self.find_row(nid)
        if i is None:
            if not quiet:
                self.msg = f"{nid} is not in this view"
            return False
        self.cur = i
        return True

    def indent(self, i):
        t = self.rows[i].text
        m = re.search(r"[├└]─ ", t)
        return m.start() if m else 0

    def has_kids(self, i):
        return i + 1 < len(self.rows) and self.rows[i + 1].kind != "head" and self.indent(i + 1) > self.indent(i)

    def depth(self, i):
        t = self.rows[i].text
        return self.indent(i) // 3 + 1 if re.search(r"[├└]─ ", t) else 0

    def fold_below(self, depth):
        """Fold every node at tree depth >= depth (root = 0), keeping the cursor's node visible."""
        keep = self.current_nid()
        self.collapsed.clear()
        self.rebuild_rows(keep=None)
        self.collapsed = {r.nid for i, r in enumerate(self.rows)
                          if r.kind == "" and r.nid and self.depth(i) >= depth and self.has_kids(i)}
        self.rebuild_rows(keep=keep)

    def toggle_fold(self, want=None):
        r = self.rows[self.cur] if self.rows else None
        if not r or r.kind != "" or not r.nid or self.o["layout"] != "tree":
            return
        folded = r.nid in self.collapsed
        if want is None:
            want = not folded
        if want and not folded and (self.has_kids(self.cur)):
            self.collapsed.add(r.nid)
        elif not want and folded:
            self.collapsed.discard(r.nid)
        else:
            return
        self.rebuild_rows(keep=r.nid)

    # ---- actions ----
    def enter(self):
        if not self.rows:
            return
        r = self.rows[self.cur]
        if r.kind == "sec":
            self.toggle_section(r.jump)
        elif r.kind == "link" and r.jump:
            if not self.goto(r.jump):
                self.focus_on(r.jump)
        elif r.kind in ("", "mention") and r.nid:
            self.focus_on(r.nid)

    def next_section(self, direction):
        """home: jump to the next / previous section header and scroll it to the top of the list."""
        idx = [i for i, r in enumerate(self.rows) if r.kind == "sec"]
        if not idx:
            return
        if direction > 0:
            nxt = [i for i in idx if i > self.cur]
            target = nxt[0] if nxt else idx[-1]
        else:
            prv = [i for i in idx if i < self.cur]
            target = prv[-1] if prv else idx[0]
        self.cur = target
        self.top = max(target - 1, 0)
        self.scroll_free = False

    def toggle_section(self, key):
        self.home_folded ^= {key}
        i = self.cur
        self.rebuild_rows(keep=None)
        self.cur = min(i, len(self.rows) - 1)
        if not self.valid(self.cur):
            self.move(-1)

    def snapshot(self):
        return (self.view, self.root, self.cur, self.top, set(self.collapsed), set(self.home_folded), list(self.me))

    def restore(self, st):
        self.view, self.root, cur, top, self.collapsed, self.home_folded, self.me = st
        self.rebuild_graph()
        self.rebuild_rows(keep=None)          # restore the saved fold state and cursor exactly
        self.cur, self.top = min(cur, max(len(self.rows) - 1, 0)), top
        self.pane_focus = self.side_focus = False

    def forward(self):
        if not self.fwd:
            self.msg = "nothing to go forward to"
            return
        self.hist.append(self.snapshot())
        self.restore(self.fwd.pop())

    def focus_on(self, nid):
        self.hist.append(self.snapshot())
        self.fwd = []
        self.view, self.root, self.collapsed, self.cur, self.top = "graph", nid, set(), 0, 0
        self.rebuild()
        if self.o["layout"] == "tree":
            self.fold_below(self.o.get("depth", 1))
        self.cur = 0
        self.move(1)

    def back(self):
        if not self.hist:
            if self.view == "graph" and self.o.get("home", True):
                self.fwd.append(self.snapshot())
                self.go_home()
            else:
                self.msg = "already at the top"
            return
        self.fwd.append(self.snapshot())
        self.restore(self.hist.pop())

    def go_home(self, push=False, clear=True):
        if push:
            self.hist.append(self.snapshot())
            self.fwd = []
        elif clear:
            self.hist = []
        self.view, self.root, self.collapsed, self.cur, self.top = "home", None, set(), 0, 0
        self.rebuild()
        self.cur = 0
        self.move(1)

    def view_as(self, me):
        """Switch the home perspective (u key / double-click on @login); the previous one goes on the back stack."""
        self.hist.append(self.snapshot())
        self.fwd = []
        self.me = [m.lower() for m in me]
        self.msg = "viewing as " + (", ".join("@" + m for m in self.me) or "(nobody)")
        if self.view == "home":
            self.rebuild_rows(keep=None)
            self.cur = 0
            self.move(1)
        else:
            self.go_home(clear=False)

    def go_overview(self):
        self.view, self.root, self.hist, self.collapsed, self.cur, self.top = "graph", None, [], set(), 0, 0
        self.rebuild()
        if self.o["layout"] == "tree":
            self.fold_below(self.o.get("depth", 1))
        self.cur = 0
        self.move(1)

    def node_url(self, nid):
        n = self.g.nodes.get(nid)
        if not n:
            return None
        if n.kind == "person":
            return f"https://{repo_host(self.g.primary)}/{n.title}"
        if n.url:
            return n.url
        host, owner, name = split_repo(n.repo)
        return f"https://{host}/{owner}/{name}/issues/{n.number}"

    def open_browser(self):
        url = self.node_url(self.current_nid())
        if not url:
            return
        try:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.msg = f"opened {url}"
        except OSError as e:
            self.msg = f"cannot open browser: {e}"

    def details(self):
        nid = self.current_nid()
        if not nid:
            return
        self.pager(render_show(self.g, nid, width=200).splitlines(), f"details: {nid}")

    def prompt_line(self, label, maxlen=400):
        """Read one line of input on the bottom line. Returns "" when cancelled/empty."""
        h, w = self.scr.getmaxyx()
        self.curses.echo()
        self.curses.curs_set(1)
        self.scr.timeout(-1)   # block for the whole line even while a background worker is running
        try:
            self.scr.addstr(h - 1, 0, label.ljust(w - 1))
            q = self.scr.getstr(h - 1, dw(label), maxlen).decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            q = ""
        self.curses.noecho()
        self.curses.curs_set(0)
        return q

    def search(self):
        q = self.prompt_line("/")
        if q:
            self.query = q
            self.search_next(1)

    def ask(self):
        if self.ask_thread is not None and self.ask_thread.is_alive():
            self.msg = "a question is still running"
            return
        r = self.rows[self.cur] if self.rows else None
        nid = r.nid if r else None
        if r and r.kind == "link" and r.jump:
            nid = r.jump
        if not nid or nid not in self.g.nodes:
            self.msg = "put the cursor on an issue, PR, comment or person first"
            return
        n = self.g.nodes[nid]
        label = self.g.label_num(n) if n.kind == "item" else (f"comment on {self.g.label_num(self.g.nodes[n.parent])}"
                                                              if n.kind == "comment" else n.id)
        q = self.prompt_line(f"ask claude about {label}: ")
        if not q:
            return
        import threading
        self.ask_state = {"nid": nid, "label": label, "q": q, "t0": time.time(), "answer": None, "error": None}
        st = self.ask_state
        self.side = {"title": f"asking {model_label(ASK_MODEL)} · {label}", "text": f"Q: {q}\n\n(waiting for the answer…)"}
        self.side_on, self.side_scroll = True, 0

        def work():
            try:
                st["answer"] = ask_claude(self.g, nid, q)
            except Exception as e:  # noqa: BLE001
                st["error"] = str(e)

        self.ask_thread = threading.Thread(target=work, daemon=True)
        self.ask_thread.start()

    def show_answer(self):
        """Put the finished answer into the right-hand panel and focus it."""
        st = self.ask_state
        if not st or st["answer"] is None and st["error"] is None:
            self.msg = "no answer yet (a = ask)"
            return
        self.side = {"title": f"{model_label(ASK_MODEL)} · {st['label']}",
                     "text": f"Q: {st['q']}\n\n" + (st["answer"] or f"error: {st['error']}")}
        self.side_on, self.side_scroll, self.side_focus, self.pane_focus = True, 0, True, False

    def side_width(self, w):
        return w // 2 if (self.side_on and self.side and w >= 80) else 0

    def draw_side(self, x0, sw, height):
        c = self.curses
        for y in range(height):
            self.put(y, x0 - 1, "│", c.A_DIM)
        lines = wrap(self.side["text"], sw - 1)
        body = height - 1
        self.side_scroll = max(0, min(self.side_scroll, max(len(lines) - body, 0)))
        pos = f" {self.side_scroll + 1}-{min(self.side_scroll + body, len(lines))}/{len(lines)}"
        title = clip(self.side["title"], 0, sw - 1 - dw(pos)) + pos
        hint = "[focused: ↑↓ PgUp PgDn scroll, w/Esc back]" if self.side_focus else "(w focus, A hide, a ask)"
        self.put(0, x0, title + " " * max(0, sw - 1 - dw(title)), c.A_BOLD | c.A_REVERSE)
        for i in range(body):
            j = self.side_scroll + i
            if j >= len(lines):
                break
            self.put(1 + i, x0, lines[j])
        self.put(height - 1, x0, clip(hint, 0, sw - 1), c.A_DIM)

    def search_next(self, direction):
        if not self.query:
            self.msg = "no search query (press /)"
            return
        q = self.query.lower()
        n = len(self.rows)
        for k in range(1, n + 1):
            i = (self.cur + direction * k) % n
            if self.valid(i) and q in self.rows[i].text.lower():
                self.cur = i
                return
        self.msg = f"not found: {self.query}"

    # ---- preview pane ----
    def preview_lines(self, width):
        r = self.rows[self.cur] if self.rows and 0 <= self.cur < len(self.rows) else None
        if not r:
            return []
        nid = r.jump if r.kind in ("link", "mention") and r.jump else r.nid
        n = self.g.nodes.get(nid) if nid else None
        if not n:
            return wrap(r.text.strip(), width)
        g = self.g
        out, body = [], ""
        if n.kind == "item":
            out.append(f"{g.label_num(n)} {kind_tag(n)} {n.tr_title or n.title or '(unresolved)'}")
            if n.tr_title:
                out.append(f"original: {n.title}")
            meta = [f"@{n.author}" if n.author else "", short_date(n.created),
                    f"updated {short_date(n.updated)}" if n.updated else "", ", ".join(n.labels)]
            out.append("  ".join(x for x in meta if x))
            if n.url:
                out.append(n.url)
            if n.stub:
                out.append("(not fetched: closed item or other repo — press o to open it)")
            body = n.body
        elif n.kind == "comment":
            head = f"{g.label_num(g.nodes[n.parent])} comment  @{n.author}  {short_date(n.created)}"
            if n.ckind == "review":
                head += f"  [{(n.review_state or 'review').lower()}]"
            elif n.ckind == "review_comment":
                head += "  [inline review comment]"
            out.append(head)
            if n.summary:
                out.append(f"» {n.summary}")
            if n.url:
                out.append(n.url)
            body = n.body
        else:
            out.append(f"{n.id}  mentioned {n.mention_count} times:")
            for m, t, o in sorted(g.adj[nid], key=lambda e: -g.nodes[e[0]].time):
                if t == "mention":
                    out.append("  " + node_label(g, g.nodes[m], 120))
        if body.strip():
            out.append("")
            out.extend(wrap(body, width))
        return out

    # ---- drawing ----
    def style_attr(self, st):
        c = self.curses
        if st.startswith("person:"):
            pal = getattr(self, "person_pal", PERSON_PALETTE_8)
            return c.color_pair(20 + person_index(st[7:]) % len(pal))
        return {"pre": c.A_DIM, "link": c.A_DIM, "head": c.A_BOLD, "meta": c.A_DIM, "stub": c.A_DIM,
                "out": c.color_pair(6), "in": c.color_pair(2),
                "closes": c.color_pair(6) | c.A_BOLD, "closedby": c.color_pair(2) | c.A_BOLD,
                "issue": c.color_pair(1) | c.A_BOLD, "pr": c.color_pair(6) | c.A_BOLD, "draft": c.color_pair(6),
                "merged": c.color_pair(2) | c.A_BOLD, "closed": c.color_pair(3) | c.A_BOLD,
                "comment": c.color_pair(4), "person": c.color_pair(5),
                "fold": c.color_pair(5) | c.A_BOLD, "sum": c.color_pair(7),
                "pending": c.A_DIM | getattr(c, "A_ITALIC", 0)}.get(st, 0)

    def put_row(self, y, row, hs, width, extra=0):
        """Draw a row from display column hs, colouring each segment; fill to width when extra is set."""
        x, col = 0, 0
        for text, st in colorize_people(segments(row, self.cg)):
            attr = self.style_attr(st) | extra
            buf = []
            for ch in text:
                w = _cw(ch)
                if col + w > hs + width:
                    break
                if col >= hs:
                    buf.append(ch)
                elif col + w > hs:
                    buf.append(" ")
                col += w
            if buf:
                sub = "".join(buf)
                try:
                    self.scr.addstr(y, x, sub, attr)
                except self.curses.error:
                    pass
                x += dw(sub)
        if extra and x < width:
            try:
                self.scr.addstr(y, x, " " * (width - x), extra)
            except self.curses.error:
                pass

    def status(self, msg):
        self.msg = msg
        self.draw()

    def put(self, y, x, text, attr=0, fill=False):
        h, w = self.scr.getmaxyx()
        s = clip(text, 0, w - 1 - x)
        if fill:
            s += " " * max(0, w - 1 - x - dw(s))
        try:
            self.scr.addstr(y, x, s, attr)
        except self.curses.error:
            pass

    def draw(self):
        c = self.curses
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        if not self.pane_h:
            self.pane_h = max(6, h // 3)
        pane = min(self.pane_h, h - 8) if self.pane_on and h >= 14 else 0
        if pane and self.pane_focus:
            pane = max(pane, h - 9)   # focused: the preview takes most of the screen, 6 list rows stay visible
        body = h - 2 - (pane + 1 if pane else 0)
        sw = self.side_width(w)
        lw = w - 1 - (sw + 1 if sw else 0)   # width of the left (list + preview) area
        self.layout = {"body": body, "pane": pane, "lw": lw, "sw": sw}
        self.top = max(0, min(self.top, max(len(self.rows) - body, 0)))
        if not self.scroll_free:
            if self.cur < self.top:
                self.top = self.cur
            if self.cur >= self.top + body:
                self.top = self.cur - body + 1
        for i in range(body):
            idx = self.top + i
            if idx >= len(self.rows):
                break
            self.put_row(i, self.rows[idx], self.hs, lw, c.A_REVERSE if idx == self.cur else 0)
        if self.legend_on and lw >= 70:
            leg = (["YYYY-MM-DD opened   +Nd comment, days after opening   [I] issue  [PR] pull  (state shown only if not open)",
                    "→ refs  ← cited-by   → closes  ← closed-by   → mentions",
                    "⇢ link drawn elsewhere   ▾ open  ▸ folded [+N]  · leaf   L hide"]
                   if self.o["layout"] == "tree" or self.view == "home" else
                   ["* issue/PR   o comment   @ person   newest on top",
                    "lanes = references;  → what a comment points at   L hide"])
            legw = max(dw(x) for x in leg) + 2
            for i, x in enumerate(leg):
                self.put(i, lw - legw, " " + x + " " * (legw - 1 - dw(x)), c.A_REVERSE | c.A_DIM)
        if pane:
            lines = self.preview_lines(lw - 2)
            if self.pv_key != self.cur:
                self.pv_key, self.pv = self.cur, 0
            self.pv = max(0, min(self.pv, max(len(lines) - pane, 0)))
            label = (f" preview {self.pv + 1}-{min(self.pv + pane, len(lines))}/{len(lines)}  "
                     + ("[focused: ↑↓ PgUp PgDn g G scroll, w/Esc back] " if self.pane_focus
                        else "(J/K scroll, w focus, v hide) "))
            self.put(body, 0, clip("─" * 2 + label + "─" * max(0, lw - 2 - dw(label)), 0, lw),
                     c.A_BOLD if self.pane_focus else c.A_DIM)
            for i in range(pane):
                if self.pv + i >= len(lines):
                    break
                self.put(body + 1 + i, 1, clip(lines[self.pv + i], 0, lw - 1))
        if sw:
            self.draw_side(lw + 1, sw, h - 2)
        where = "home" if self.view == "home" else (f"focus {self.root} (hops {self.o['hops']})" if self.root else "overview")
        title = (f" gitgraph  {self.o['layout']}  {where}  comments={self.o['comments']} "
                 f"people={'on' if self.o['people'] else 'off'} translate={self.o['translate']} "
                 f"summary={'on' if self.o['summary'] else 'off'} me={','.join('@' + m for m in self.me) or '-'}  "
                 f"| {usage_line()}  "
                 f"row {self.cur + 1}/{len(self.rows)}")
        self.put(h - 2, 0, title, c.A_BOLD | c.A_REVERSE, fill=True)
        r = self.rows[self.cur] if self.rows and self.cur < len(self.rows) else None
        info = self.msg or (f"{r.nid}" + (f"  → {r.jump}" if r and r.jump else "") if r and r.nid else "")
        if self.busy():
            self.put(h - 1, 0, self.progress_text(), c.A_BOLD)
        elif self.bg_error:
            self.put(h - 1, 0, f"background work failed: {self.bg_error}", c.color_pair(3))
        else:
            self.put(h - 1, 0, f"{info}   ?=help q=quit", c.A_DIM)
        scr.refresh()

    def pager(self, lines, title):
        """Scrollable full-screen text pager."""
        c = self.curses
        top, hs = 0, 0
        while True:
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            body = h - 1
            for i in range(body):
                if top + i >= len(lines):
                    break
                self.put(i, 0, clip(lines[top + i], hs, w - 1))
            self.put(h - 1, 0, f" {title}  {top + 1}-{min(top + body, len(lines))}/{len(lines)}  "
                               f"j/k PgUp/PgDn scroll  < > hscroll  q/Esc back", c.A_BOLD | c.A_REVERSE, fill=True)
            self.scr.refresh()
            k = self.read_key()
            if k in (ord("q"), 27, 10, 13, c.KEY_ENTER, ord("d")):
                return
            elif k in (c.KEY_DOWN, ord("j")):
                top = min(top + 1, max(len(lines) - body, 0))
            elif k in (c.KEY_UP, ord("k")):
                top = max(top - 1, 0)
            elif k == c.KEY_NPAGE or k == ord(" "):
                top = min(top + body, max(len(lines) - body, 0))
            elif k == c.KEY_PPAGE:
                top = max(top - body, 0)
            elif k == ord("g"):
                top = 0
            elif k == ord("G"):
                top = max(len(lines) - body, 0)
            elif k == ord(">"):
                hs += 20
            elif k == ord("<"):
                hs = max(hs - 20, 0)

    # ---- mouse (SGR events parsed in read_key: b = button code, press = True for press) ----
    def on_mouse(self):
        c = self.curses
        ev = self.mouse_ev
        if not ev:
            return
        b, x, y, press = ev
        if not press or b & 32:      # releases and motion are ignored
            return
        base = b & ~28               # strip shift/meta/ctrl
        L = self.layout
        in_side = bool(L["sw"]) and x > L["lw"]
        in_pane = (not in_side) and bool(L["pane"]) and L["body"] < y <= L["body"] + L["pane"]
        in_list = (not in_side) and y < L["body"]
        if base in (64, 65):         # wheel
            d = -3 if base == 64 else 3
            if in_side:
                self.side_scroll = max(0, self.side_scroll + d)
            elif in_pane:
                self.pv = max(0, self.pv + d)
            elif in_list:
                self.top = max(0, self.top + d)   # scroll the view; the cursor stays where it is
                self.scroll_free = True
            return
        if base == 2:                # right button: open the row under the mouse in the browser
            if in_list:
                idx = self.top + y
                if idx < len(self.rows) and self.valid(idx):
                    self.cur = idx
            self.open_browser()
            return
        if base == 128:              # back button: leave a focused panel first, otherwise go back
            if self.pane_focus or self.side_focus:
                self.pane_focus = self.side_focus = False
            else:
                self.back()
            return
        if base == 129:              # forward button
            self.forward()
            return
        if base != 0:                # only the left button does something below
            return
        if in_side:
            self.side_focus, self.pane_focus = True, False
            return
        if in_pane:
            self.pane_focus, self.side_focus = True, False
            return
        if not in_list:
            return
        idx = self.top + y
        if idx >= len(self.rows) or not self.valid(idx):
            return
        self.pane_focus = self.side_focus = False
        self.scroll_free = False
        now = time.time()
        double = (now - self.last_click[0] < 0.4) and self.last_click[1] == idx
        self.last_click = (now, idx)
        self.cur = idx
        r = self.rows[idx]
        if os.environ.get("GG_DEBUG"):
            log("spans " + " ".join(f"{dw(r.text[:m.start()]) - self.hs}:{m.group(0)}" for m in _LOGIN_RE.finditer(r.text)))
        if double:
            self.last_click = (0.0, -1)
            for m in _LOGIN_RE.finditer(r.text):    # double-click on a @login -> that person's perspective
                c0 = dw(r.text[:m.start()]) - self.hs
                if c0 <= x < c0 + dw(m.group(0)):
                    self.view_as([m.group(0)[1:]])
                    return
            self.enter()
            return
        if r.kind == "sec":
            self.toggle_section(r.jump)
            return
        m = re.search(r"[▾▸·] ", r.text)
        if m:
            col = dw(r.text[:m.start()]) - self.hs
            if col <= x <= col + 1:
                self.toggle_fold()

    # ---- input ----
    ESC_SEQ = {"A": "KEY_UP", "B": "KEY_DOWN", "C": "KEY_RIGHT", "D": "KEY_LEFT", "H": "KEY_HOME", "F": "KEY_END",
               "5~": "KEY_PPAGE", "6~": "KEY_NPAGE", "1~": "KEY_HOME", "4~": "KEY_END", "3~": "KEY_DC"}

    def read_key(self):
        """getch() that reassembles CSI sequences curses sometimes hands over byte by byte (ESC [ B),
        and parses SGR mouse reports (ESC [ < b ; x ; y M/m) that curses announces as KEY_MOUSE."""
        c = self.curses
        k = self.scr.getch()
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

    # ---- main loop ----
    def run(self):
        c = self.curses
        while True:
            if self.worker is not None and not self.worker.is_alive():
                self.worker = None
                self.progress = None
                self.rebuild_rows()
            if self.ask_thread is not None and not self.ask_thread.is_alive():
                self.ask_thread = None
                self.show_answer()
            self.scr.timeout(150 if self.busy() else -1)
            self.draw()
            k = self.read_key()
            if k == -1:
                continue
            self.msg = ""
            h, w = self.scr.getmaxyx()
            page = max(h - 3, 1)
            if os.environ.get("GG_DEBUG"):
                log(f"key={k!r} cur={self.cur} rows={len(self.rows)} folded={len(self.collapsed)} view={self.view} "
                    f"root={self.root} me={self.me} enriched={len(self.enriched)}")
            if k == ord("q"):
                return
            if k == c.KEY_MOUSE:
                self.on_mouse()
                continue
            if k == ord("w"):   # cycle focus: list -> preview -> answer panel -> list
                h_, w_ = self.scr.getmaxyx()
                if self.pane_focus:
                    self.pane_focus = False
                    self.side_focus = bool(self.side_width(w_))
                elif self.side_focus:
                    self.side_focus = False
                else:
                    self.pane_focus = self.pane_on
                    if not self.pane_focus:
                        self.side_focus = bool(self.side_width(w_))
                continue
            if self.side_focus:
                sh = max(h - 4, 1)
                if k in (c.KEY_DOWN, ord("j"), ord("J")):
                    self.side_scroll += 1
                elif k in (c.KEY_UP, ord("k"), ord("K")):
                    self.side_scroll = max(self.side_scroll - 1, 0)
                elif k in (c.KEY_NPAGE, ord(" ")):
                    self.side_scroll += sh
                elif k == c.KEY_PPAGE:
                    self.side_scroll = max(self.side_scroll - sh, 0)
                elif k == ord("g"):
                    self.side_scroll = 0
                elif k == ord("G"):
                    self.side_scroll = 10 ** 9
                elif k in (27, c.KEY_BACKSPACE, 127, 8, ord("b"), ord("h")):
                    self.side_focus = False
                elif k == c.KEY_MOUSE:
                    self.on_mouse()
                elif k == ord("A"):
                    self.side_on, self.side_focus = False, False
                elif k == ord("a"):
                    self.side_focus = False
                    self.ask()
                elif k == ord("q"):
                    return
                continue
            if k == ord("L"):
                self.legend_on = not self.legend_on
                continue
            if self.pane_focus:
                ph = max(h - 10, 1)
                if k in (c.KEY_DOWN, ord("j"), ord("J")):
                    self.pv += 1
                elif k in (c.KEY_UP, ord("k"), ord("K")):
                    self.pv = max(self.pv - 1, 0)
                elif k in (c.KEY_NPAGE, ord(" ")):
                    self.pv += ph
                elif k == c.KEY_PPAGE:
                    self.pv = max(self.pv - ph, 0)
                elif k == ord("g"):
                    self.pv = 0
                elif k == ord("G"):
                    self.pv = 10 ** 9   # clamped in draw()
                elif k in (27, c.KEY_BACKSPACE, 127, 8, ord("b"), ord("h")):
                    self.pane_focus = False   # "back" while the preview is focused just leaves it
                elif k == ord("v"):
                    self.pane_on, self.pane_focus = False, False
                elif k == c.KEY_MOUSE:
                    self.on_mouse()
                continue
            elif k in (c.KEY_DOWN, ord("j")):
                self.move(1)
            elif k in (c.KEY_UP, ord("k")):
                self.move(-1)
            elif k == c.KEY_NPAGE:
                self.next_section(1) if self.view == "home" else self.move(page)
            elif k == c.KEY_PPAGE:
                self.next_section(-1) if self.view == "home" else self.move(-page)
            elif k in (ord("g"), c.KEY_HOME):
                self.scroll_free = False
                self.cur = 0
                self.move(1) if not self.valid(0) else None
            elif k in (ord("G"), c.KEY_END):
                self.scroll_free = False
                self.cur = len(self.rows) - 1
                if not self.valid(self.cur):
                    self.move(-1)
            elif k == 9:  # Tab
                self.go_overview() if self.view == "home" else self.go_home()
            elif self.view == "home" and k in (ord(" "), c.KEY_LEFT, c.KEY_RIGHT):
                r = self.rows[self.cur] if self.rows else None
                if r and r.kind == "sec":
                    self.toggle_section(r.jump)
                else:
                    self.msg = "Enter opens the tree; Space on a section header folds it; Tab = full overview"
            elif self.view == "home" and k in (ord("-"), ord("+"), ord("l")):
                if k == ord("-"):
                    self.home_folded = {"turn", "mention", "opened", "active", "waiting", "mine", "prs", "stale", "all"}
                elif k == ord("+"):
                    self.home_folded = set()
                self.rebuild_rows()
            elif k == ord(" "):
                self.toggle_fold()
            elif k == c.KEY_LEFT:
                self.toggle_fold(True)
            elif k == c.KEY_RIGHT:
                self.toggle_fold(False)
            elif k == ord("-"):
                self.fold_below(1)
            elif ord("1") <= k <= ord("9"):
                self.fold_below(k - ord("0"))
            elif k == ord("+"):
                nid = self.current_nid()
                self.collapsed.clear()
                self.rebuild_rows(keep=nid)
            elif k in (10, 13, c.KEY_ENTER):
                self.enter()
            elif k in (c.KEY_BACKSPACE, 127, 8, 27, ord("h"), ord("b")):
                self.back()
            elif k == ord("f"):
                self.forward()
            elif k == ord("d"):
                self.details()
            elif k == ord("a"):
                self.ask()
            elif k == ord("u"):
                who = self.prompt_line("view as @login (empty = my gh accounts): ").lstrip("@")
                self.view_as([who] if who else (ME or [a.lower() for a in gh_accounts()]))
            elif k == ord("A"):
                if self.side:
                    self.side_on = not self.side_on
                    self.side_focus = False
                else:
                    self.msg = "no answer yet (a = ask)"
            elif k == ord("o"):
                self.open_browser()
            elif k == ord("l"):
                self.o["layout"] = "log" if self.o["layout"] == "tree" else "tree"
                self.rebuild_rows()
            elif k == ord("c"):
                i = self.COMMENTS_CYCLE.index(self.o["comments"])
                self.o["comments"] = self.COMMENTS_CYCLE[(i + 1) % 3]
                self.rebuild()
            elif k == ord("p"):
                self.o["people"] = not self.o["people"]
                self.rebuild()
            elif k == ord("t"):
                if self.o["translate"] == "none":
                    self.o["translate"] = self.tr_saved
                else:
                    self.tr_saved, self.o["translate"] = self.o["translate"], "none"
                    for n in self.g.nodes.values():
                        n.tr_title = n.tr_excerpt = None
                self.enriched.clear()
                self.rebuild_rows()
            elif k == ord("v"):
                self.pane_on = not self.pane_on
            elif k == ord("J"):
                self.pv += 1
            elif k == ord("K"):
                self.pv = max(self.pv - 1, 0)
            elif k == ord("}"):
                self.pane_h = min(self.pane_h + 2, h - 8)
            elif k == ord("{"):
                self.pane_h = max(self.pane_h - 2, 3)
            elif k == ord("s"):
                self.o["summary"] = not self.o["summary"]
                if not self.o["summary"]:
                    for n in self.g.nodes.values():
                        n.summary = None
                self.enriched.clear()
                self.rebuild_rows()
            elif k == ord("H"):
                self.o["hops"] = self.o["hops"] % 3 + 1
                if self.root:
                    self.rebuild()
            elif k == ord("r"):
                self.load(refresh=True)
            elif k == ord("/"):
                self.search()
            elif k == ord("n"):
                self.search_next(1)
            elif k == ord("N"):
                self.search_next(-1)
            elif k == ord(">"):
                self.hs += 20
            elif k == ord("<"):
                self.hs = max(self.hs - 20, 0)
            elif k == ord("?"):
                self.pager(HELP.splitlines(), "help")
            elif k == ord("$"):
                self.pager(usage_report(), "claude token usage")
            elif k == c.KEY_RESIZE:
                pass


def tui(opts):
    import curses
    import locale
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")   # plain Esc responds quickly; read_key() reassembles sequences
    os.makedirs(CACHE_DIR, exist_ok=True)
    # keep the curses screen clean: gh / translation progress goes to a log file
    errf = open(os.path.join(CACHE_DIR, "tui.log"), "a")
    os.dup2(errf.fileno(), 2)
    try:
        curses.wrapper(lambda scr: Tui(scr, opts).run())
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code)
    finally:
        sys.stdout.write("\033[?1006l\033[?1000l")
        sys.stdout.flush()


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
    ap.add_argument("cmd", nargs="?", default="graph",
                    help="graph (default) | ROOT (777 / #777 / owner/repo#777 / @login) | tui [ROOT] | show ID | "
                         "ask ID \"question\" | update | config [KEY [VALUE]]")
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
    ap.add_argument("--depth", type=int, default=1, help="tui: initial tree expansion depth (default 1)")
    ap.add_argument("--days", type=int, default=7, help="tui home: 'opened in the last N days' window (default 7)")
    ap.add_argument("--no-home", action="store_true", help="tui: start at the full overview instead of the home list")
    ap.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                    help="ANSI colours (auto = when stdout is a terminal)")
    a = ap.parse_intermixed_args(argv)
    if a.user:
        ME[:] = [a.user.lstrip("@").lower()]
    if a.cmd not in ("graph", "tui", "show", "ask", "update", "config") and ROOT_RE.match(a.cmd):
        a.root, a.cmd = a.cmd, "graph"   # `gg 777`
    if a.cmd == "update":
        return update()
    if a.cmd == "config":
        return config_cmd([x for x in (a.arg, a.question) if x is not None] + (a.extra or []))
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
             "depth": a.depth, "days": a.days, "home": not a.no_home})
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
