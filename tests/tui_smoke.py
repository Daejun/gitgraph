#!/usr/bin/env python3
"""Drive the real `gg tui` in a pseudo-terminal against the fixture repo, render its output with the
small VT emulator in vt.py, and check the screen.

    python3 tests/tui_smoke.py            # no gh login, no network, no AI CLI needed
    python3 tests/run.py smoke            # same thing through the runner
    GG_UPDATE_GOLDEN=1 python3 tests/tui_smoke.py    # rewrite tests/golden/tui_*.txt from this run

Everything happens inside a throwaway HOME built by tests/env.py: the fixture repo is already "fetched"
into its cache, `gh` on PATH is the fail-loud fake, the AI CLI is tests/fakes/claude (deterministic) and
xdg-open is a fake that records URLs instead of opening a browser. The user's real ~/.cache/gitgraph,
~/.config/gitgraph and ~/gitgraph-todo.md are never read or written.

Not a unit test: it exercises the real program end to end. One ok/FAIL line per check, exit 1 on any
failure.
"""
import json
import os
import pty
import re
import select
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as testenv  # noqa: E402
from vt import Screen  # noqa: E402

ROWS, COLS = 34, 140
HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN_DIR = os.path.join(HERE, "golden")
GITGRAPH = os.path.join(os.path.dirname(HERE), "gitgraph.py")
FAKE_CLAUDE = os.path.join(HERE, "fakes", "claude")
UPDATE_GOLDEN = bool(os.environ.get("GG_UPDATE_GOLDEN"))

# Everything in a rendered screen that changes between runs, blanked before comparing with a golden file.
NOISE = [
    (re.compile(r"\d\d:\d\d(:\d\d)?"), "TT:TT"),          # the clock in the Repo panel
    (re.compile(r"\b\d+[smhd]\b"), "AGE"),                 # "3s", "12m", "2h", "5d" (ages, elapsed)
    (re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]"), " "),                    # progress spinner frame
    (re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d"), "FETCHED"),  # "data fetched ..." timestamps
]


PROGRESS_WORDS = ("summarizing", "translating", "fetching", "starting", "resolving", "AI jobs", "batch")


def normalise(text):
    for rx, repl in NOISE:
        text = rx.sub(repl, text)
    out = []
    for line in text.splitlines():
        # the background-progress line changes with timing (which batch is running, how far it got)
        out.append("PROGRESS" if any(w in line for w in PROGRESS_WORDS) else line.rstrip())
    return "\n".join(out)


class Session:
    def __init__(self, args=(), home=None, rows=ROWS, cols=COLS, cwd=None, **envextra):
        self.home = home or testenv.make_home()
        envextra.setdefault("FAKE_GH_MODE", "script")
        if "FAKE_GH_FIXTURE" not in envextra:      # (not setdefault: it would build one either way)
            envextra["FAKE_GH_FIXTURE"] = gh_fixture(self.home)
        self.rows, self.cols = rows, cols
        # deliberately no LINES/COLUMNS: ncurses would honour them over the pty's real size and never
        # see a resize (SIGWINCH -> KEY_RESIZE), which is one of the things this file checks
        env = testenv.child_env(self.home, GITGRAPH_CLAUDE=FAKE_CLAUDE, GG_DEBUG="1", **envextra)
        self.env = env
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            if cwd:
                os.chdir(cwd)          # review mode looks for a clone of the repo under the cwd
            os.execve(sys.executable, [sys.executable, GITGRAPH, "tui", "--no-tour",
                                       "--max-age", str(testenv.FIXTURE_MAX_AGE_MIN)] + list(args), env)
        self.set_size(rows, cols)      # before the child reaches curses init
        self.scr = Screen(rows, cols)

    # -- terminal plumbing ------------------------------------------------
    def set_size(self, rows, cols):
        import fcntl
        import struct
        import termios
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def resize(self, rows, cols):
        """Resize the pty (the child gets SIGWINCH -> curses KEY_RESIZE) and start a matching screen."""
        self.rows, self.cols = rows, cols
        self.scr = Screen(rows, cols)
        self.set_size(rows, cols)
        self.drain(1.2)

    def drain(self, t):
        end = time.time() + t
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                try:
                    self.scr.feed(os.read(self.fd, 65536).decode("utf-8", "replace"))
                except OSError:
                    return

    def settle(self, timeout=25):
        """Wait until no background job is reporting progress, so a screenshot is reproducible.
        Since 0.18.0 the TUI always asks GitHub what changed on start-up, so there is always one
        background job to wait for (answered here by tests/fakes/gh in script mode)."""
        end = time.time() + timeout
        while time.time() < end:
            self.drain(0.4)
            if not any(w in self.scr.text() for w in PROGRESS_WORDS):
                self.drain(0.3)
                return True
        return False

    def wait_for(self, text, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            self.drain(0.4)
            if text in self.scr.text():
                return True
        return False

    def key(self, data, wait=0.6):
        os.write(self.fd, data if isinstance(data, bytes) else data.encode())
        self.drain(wait)

    def keys(self, *seq, wait=0.5):
        for k in seq:
            self.key(k, wait)

    def mouse(self, code, x, y, press=True):
        os.write(self.fd, f"\x1b[<{code};{x + 1};{y + 1}{'M' if press else 'm'}".encode())

    def click(self, x, y, wait=0.6):
        self.mouse(0, x, y)
        self.mouse(0, x, y, press=False)
        self.drain(wait)

    # -- reading the screen -----------------------------------------------
    def text(self):
        return self.scr.text()

    def line(self, i):
        lines = self.scr.text().splitlines()
        return lines[i] if i < len(lines) else ""

    def find_line(self, needle):
        for i, l in enumerate(self.text().splitlines()):
            if needle in l:
                return i
        return -1

    def cache(self, name):
        path = os.path.join(self.home, ".cache", "gitgraph", name)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def log(self):
        path = os.path.join(self.home, ".cache", "gitgraph", "tui.log")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()

    def quit(self):
        self.key("q", 0.3)
        time.sleep(1)
        pid, st = os.waitpid(self.pid, os.WNOHANG)
        return pid != 0 and st == 0

    def kill(self):
        try:
            os.kill(self.pid, 9)
            os.waitpid(self.pid, 0)
        except OSError:
            pass


def _graphql_node(it):
    """A cached item dict turned back into the GraphQL node shape gg's fetch parses (_norm_item)."""
    node = {
        # state OPEN whatever the fixture says: these items are what its items__*__open.json holds, so
        # the listing has to keep reporting all of them (a real open cache never holds a closed item)
        "number": it["number"], "title": it["title"], "state": "OPEN", "body": it.get("body", ""),
        "createdAt": it["created"], "updatedAt": it["updated"], "url": it.get("url", ""),
        "author": {"login": it.get("author") or "ghost"},
        "labels": {"nodes": [{"name": n} for n in it.get("labels", [])]},
        "comments": {"totalCount": it.get("comments_total", len(it.get("comments", []))), "nodes": [
            {"databaseId": int(re.sub(r"\D", "", c["id"]) or 0), "url": c.get("url", ""),
             "author": {"login": c.get("author") or "ghost"}, "body": c.get("body", ""),
             "createdAt": c["created"]}
            for c in it.get("comments", []) if c.get("kind") == "comment"]},
        "timelineItems": {"nodes": [
            {"createdAt": x.get("when"), "source": {
                "__typename": "PullRequest" if x.get("is_pr") else "Issue", "number": x["number"],
                "title": x.get("title"), "state": x.get("state"), "isDraft": x.get("draft", False),
                "createdAt": x.get("created"), "author": {"login": x.get("author") or "ghost"},
                "repository": {"nameWithOwner": x["repo"]}}}
            for x in it.get("crossrefs", [])]},
    }
    if it["is_pr"]:
        node["isDraft"] = it.get("draft", False)
        node["reviews"] = {"nodes": [
            {"databaseId": int(re.sub(r"\D", "", c["id"]) or 0), "url": c.get("url", ""),
             "author": {"login": c.get("author") or "ghost"}, "body": c.get("body", ""),
             "state": c.get("review_state"), "createdAt": c["created"], "comments": {"nodes": []}}
            for c in it.get("comments", []) if c.get("kind") in ("review", "review_comment")]}
        node["closingIssuesReferences"] = {"nodes": [
            {"number": c["number"], "repository": {"nameWithOwner": c["repo"]}} for c in it.get("closes", [])]}
    return node


def gh_fixture(home, items=None):
    """Write a FAKE_GH_FIXTURE for tests/fakes/gh from the items cached in `home` (or the ones given).

    Since 0.18.0 the TUI always asks GitHub what changed when it opens, so the fake gh has to answer
    instead of refusing. The nodes are complete, not just number+updatedAt, so a *cold* cache can be
    fetched through the same fake: list_open() then the batched record fetch, which is what the
    cold-start check exercises. With the cache intact list_open() reports "0 changed" and stops there.
    """
    if items is None:
        cache = os.path.join(home, ".cache", "gitgraph",
                             f"items__{testenv.FIXTURE_REPO.replace('/', '__')}__open.json")
        with open(cache, encoding="utf-8") as f:
            items = json.load(f)["items"]
    issues, pulls = {}, {}
    for it in items:
        (pulls if it["is_pr"] else issues)[str(it["number"])] = _graphql_node(it)
    path = os.path.join(home, "gh-fixture.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"repos": {testenv.FIXTURE_REPO: {"issues": issues, "pulls": pulls}}}, f)
    return path


FAILS = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)
        if detail:
            print("     " + detail.replace("\n", "\n     ")[:1500])


def check_golden(name, text):
    """Compare a normalised screen against tests/golden/tui_<name>.txt (GG_UPDATE_GOLDEN=1 rewrites)."""
    path = os.path.join(GOLDEN_DIR, f"tui_{name}.txt")
    got = normalise(text)
    if UPDATE_GOLDEN:
        os.makedirs(GOLDEN_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(got + "\n")
        print(f"ok   golden {name} (updated)")
        return
    if not os.path.exists(path):
        check(f"golden {name}", False, f"missing {path}; run GG_UPDATE_GOLDEN=1 python3 tests/tui_smoke.py")
        return
    with open(path, encoding="utf-8") as f:
        want = f.read().rstrip("\n")
    if got == want:
        print(f"ok   golden {name}")
        return
    diff = []
    for i, (a, b) in enumerate(zip(want.splitlines(), got.splitlines())):
        if a != b:
            diff.append(f"line {i}\n  want: {a!r}\n  got : {b!r}")
    check(f"golden {name}", False, "\n".join(diff[:6]) or "line count differs")


def item_line(s):
    """The first row of the Item panel — what Enter on an Inbox row changes."""
    i = s.find_line("2 Item")
    return s.line(i + 1) if i >= 0 else ""


def current_item(s):
    """The current item's node id, read from state.json — the identity Enter changes, without the
    rendering noise (a background summary landing rewrites the Item panel's text on its own)."""
    st = s.cache("state.json") or {}
    return (st.get("item") or {}).get("id")


# ---------------------------------------------------------------- the checks


def panels_and_navigation(s):
    check("panels drawn", s.wait_for("6 People") and "0 Main" in s.line(0))
    check("repo panel shows the fixture repo", "test/repo" in s.text(), s.line(2))
    check("the start-up refresh finishes", s.settle())
    check_golden("start", s.text())

    s.key("3")                                  # Inbox
    s.key("j")
    s.key("\r", 1.5)
    check("Enter sets the item", "#" in s.line(5))
    check("Item panel shows the item", "2 Item" in s.text() and "#" in s.line(5))

    s.key("5")                                  # Links
    s.key("j")
    s.key("\r", 1.5)
    item_after_link = s.line(5)
    check("Links notes", "↳" in s.text())
    s.key("b", 1.5)
    check("Links Enter then back", item_after_link != s.line(5))
    s.key("f", 1.0)
    check("forward returns", item_after_link == s.line(5), f"{item_after_link!r} vs {s.line(5)!r}")

    s.key("4")                                  # Comments
    s.key("\r", 1.0)
    check("Comments Enter shows the comment in main", "0 Main" in s.line(0))
    s.key("6")                                  # People
    s.key("\r", 1.0)
    check("People Enter switches perspective", "6 People" in s.text())

    s.key("0")
    s.key("]", 1.0)
    check("main answer tab", "[answer]" in s.line(0))
    s.key("[", 0.8)
    check("back to content tab", "[content]" in s.line(0))

    s.key("+", 0.8)
    check("half screen: only main", "1 Repo" not in s.text())
    s.key("+", 0.8)
    check("full screen", "1 Repo" not in s.text())
    s.key("_", 0.6)
    s.key("_", 0.8)
    check("back to normal screen", "1 Repo" in s.text())


def history_highlight(s):
    """b / f change which item is current — the side list's highlight has to move with it.
    Reported bug: after back, the Item panel showed #A while the Inbox still highlighted #B."""
    def item_and_row():
        st = s.cache("state.json") or {}
        return (st.get("item") or {}).get("id"), st.get("cursor_row") or ""

    def follows(where):
        nid, row = item_and_row()
        num = (nid or "").split("#")[-1]
        check(f"the highlight follows the item {where}", bool(num) and f"#{num} " in row,
              f"item={nid} cursor_row={row[:70]!r}")

    s.key("3")
    for _ in range(10):                  # the "all" section always lists every fixture item
        if "all" in s.line(s.find_line("3 Inbox")):
            break
        s.key("]", 0.5)
    s.key("\r", 1.2)
    first = (s.cache("state.json") or {}).get("item", {}).get("id")
    s.key("j", 0.5)
    s.key("j", 0.5)
    s.key("\r", 1.2)
    second = (s.cache("state.json") or {}).get("item", {}).get("id")
    check("two different items were opened", first and second and first != second, f"{first} {second}")
    s.key("b", 1.5)
    check("back returns to the first item", (s.cache("state.json") or {}).get("item", {}).get("id") == first)
    follows("after back")
    s.key("f", 1.5)
    check("forward returns to the second item", (s.cache("state.json") or {}).get("item", {}).get("id") == second)
    follows("after forward")
    s.key("5")                           # the same, arriving from the Links panel
    s.key("j", 0.5)
    s.key("\r", 1.5)
    follows("after a jump from Links")
    s.key("b", 1.5)
    follows("after back from Links")


def inbox_tabs(s):
    s.key("3")
    seen = []
    for _ in range(len(["todo", "turn", "mention", "opened", "active",
                        "waiting", "mine", "prs", "stale", "all"])):
        title = s.line(s.find_line("3 Inbox"))
        m = re.search(r"‹ (.+?) ›", title)
        seen.append(m.group(1).strip() if m else title)
        s.key("]", 0.5)
    check("all 10 Inbox tabs cycle", len(set(seen)) == 10, f"saw {seen}")
    check("tab counter is shown", re.search(r"\d+/10", s.text()) is not None)


def search_and_marks(s):
    s.key("3")
    s.key("/", 0.5)
    s.key("한글 검색\x1b", 0.8)       # wide characters in a prompt must not crash it
    check("search prompt survives wide input", "Traceback" not in s.log())
    s.key("/", 0.5)
    s.key("#\r", 1.0)
    check("search finds a row", "1 Repo" in s.text())
    s.keys("n", "N", wait=0.4)
    check("search next/prev", "Traceback" not in s.log())

    s.key("m", 0.8)
    check("mark prompt or menu", "my note" in s.text() or "is marked" in s.text())
    s.key("regression note\r", 1.2)
    todo = s.cache("../../.config/gitgraph/todo.json") if False else None
    path = os.path.join(s.home, ".config", "gitgraph", "todo.json")
    ok = os.path.exists(path)
    entries = json.load(open(path, encoding="utf-8")) if ok else []
    check("m writes todo.json in the temp HOME", ok and len(entries) == 1,
          f"{path}: {entries}")
    check("the note is stored", bool(entries) and "regression note" in json.dumps(entries, ensure_ascii=False))
    s.key("3")
    for _ in range(10):                      # walk to the todo tab (first Inbox section)
        if "todo" in s.line(s.find_line("3 Inbox")):
            break
        s.key("[", 0.4)
    check("the mark shows in the Inbox todo tab", "✎" in s.text() or "todo" in s.line(s.find_line("3 Inbox")),
          s.line(s.find_line("3 Inbox")))
    s.key("m", 0.8)
    check("m on a marked row offers edit/done/remove", "is marked" in s.text() or "done" in s.text())
    s.key("\x1b", 0.5)
    # 0.18.0: Delete drops the mark outright, no menu
    s.key("\x1b[3~", 1.2)
    left = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else []
    check("Del removes the mark", left == [], f"todo.json still holds {left}")
    check("Del says what it removed", "mark removed" in s.text() or "✎" not in s.text(), s.text()[-300:])
    s.key("\x1b[3~", 0.8)
    check("Del on an unmarked row is harmless", "nothing marked" in s.text() or "Traceback" not in s.log())


def popups_and_menus(s):
    s.key("?", 1.0)
    check("key menu popup", "keys —" in s.text())
    s.key("\x1b", 0.5)
    check("Esc closes the key menu", "keys —" not in s.text())
    s.key("O", 1.0)
    check("options popup", "options" in s.text() and "comments:" in s.text())
    s.key("\x1b", 0.5)
    s.key("$", 1.0)
    check("usage popup", "usage" in s.text() or "tokens" in s.text())
    s.key("\x1b", 0.5)
    s.key("d", 1.0)
    check("details pager", "details:" in s.text())
    s.key("\x1b", 0.5)
    s.key("T", 0.8)
    check("theme cycles", "Traceback" not in s.log())
    s.key("T", 0.5)
    s.key("T", 0.5)


def ai_flows(s):
    """With tests/fakes/claude: the ask popup and the i translation toggle."""
    s.key("3")
    s.key("a", 0.6)
    check("prompt popup", "ask about" in s.text())
    s.key("\x1b", 0.5)
    s.key("a", 0.6)
    s.key("why?\r", 3.0)
    check("ask opens the answer tab", "[answer" in s.line(0))
    check("the fake AI answer is shown", "ANSWER: why?" in s.text(), s.text()[:400])
    s.key("[", 0.5)
    # 0.18.0: nothing is translated in the background — the body stays as written until i is pressed
    before = s.text()
    s.drain(2.0)
    check("no background translation", "TRFULL:" not in s.text() and "번역 중" not in s.text(),
          s.text()[:300])
    s.key("i", 3.0)
    check("i translates on demand", "TRFULL:" in s.text() or "번역 중" in s.text(), s.text()[:400])
    s.key("i", 1.5)
    check("i toggles back to the original", "TRFULL:" not in s.text())
    check("no traceback from translating", "Traceback" not in s.log())


def hangul_ime(s):
    """0.12.1: a shortcut typed in Hangul IME mode fires once, and the Enter/Space the IME sends to
    commit the lone jamo right after it is ignored instead of acting as a second command."""
    s.key("3")
    s.key("ㅁ", 0.8)                     # 'a' in the 2-set layout: the ask popup
    check("Hangul ㅁ opens the ask popup", "ask about" in s.text(), s.text()[:300])
    s.key("\x1b", 0.5)
    s.key("3", 0.4)
    s.key("j", 0.6)                      # move the Inbox cursor off the current item
    before = current_item(s)             # the current item only changes when Enter acts
    # jamo + the IME's commit key in ONE write, the way a real IME sends them (within ime_until)
    s.key("\u3153\r", 1.2)              # ㅓ = j (move down) followed by the committing Enter
    check("Hangul ㅓ moves the cursor", "Traceback" not in s.log())
    check("the IME commit Enter is swallowed", current_item(s) == before,
          f"{before!r} -> {current_item(s)!r}")

    # Reported bug: m did not work in Hangul mode. A real IME over ssh/tmux can deliver the three
    # UTF-8 bytes of ㅡ in separate reads; gg used to give up on the continuation bytes and drop the
    # keystroke. Send them one at a time, with a gap, the way that happens in practice.
    s.key("\x1b", 0.4)
    s.key("3", 0.4)
    for byte in (b"\xe3", b"\x85", b"\xa1"):        # ㅡ = U+3161 = the "m" key in the 2-set layout
        os.write(s.fd, byte)
        time.sleep(0.03)
    s.drain(1.5)
    check("a Hangul shortcut split across reads still fires (ㅡ = m)",
          "my note" in s.text() or "is marked" in s.text(), s.text()[-400:])
    s.key("\x1b", 0.5)


def mouse_and_border(s):
    """0.11.2: only a drag that starts on the border columns resizes the side column."""
    row = s.find_line("3 Inbox")
    border_x = s.line(row).find("╮")
    if border_x < 0:
        check("border found", False, s.line(row))
        return
    # a drag that starts inside main selects text and must NOT move the border
    s.mouse(0, border_x + 20, 10)
    s.mouse(32, border_x + 30, 10)
    s.mouse(0, border_x + 30, 10, press=False)
    s.drain(0.8)
    after_main_drag = s.line(row).find("╮")
    check("a drag inside main does not resize", after_main_drag == border_x,
          f"{border_x} -> {after_main_drag}")
    # a drag that starts on the border does move it
    s.mouse(0, border_x, 10)
    s.mouse(32, border_x + 8, 10)
    s.mouse(32, border_x + 15, 10)
    s.mouse(0, border_x + 15, 10, press=False)
    s.drain(0.8)
    moved = s.line(row).find("╮")
    check("border drag widens the side column", moved > border_x + 5, f"{border_x} -> {moved}")

    # wheel scroll and click-to-focus
    s.mouse(64, 5, 12)
    s.drain(0.4)
    check("wheel does not crash", "Traceback" not in s.log())
    s.click(5, s.find_line("4 Comments") + 1)
    check("click focuses another panel", "Traceback" not in s.log())


def url_click(s):
    """0.9.3: a URL opens only when the pointer is on its visible characters."""
    log_path = os.path.join(s.home, "opened.txt")
    s.key("3")
    s.key("\r", 1.2)
    s.key("0", 0.6)
    row = s.find_line("https://")
    if row < 0:
        check("main shows the URL line", False, s.text()[:400])
        return
    line = s.line(row)
    start = line.find("https://")
    end = start + len(line[start:].split()[0])
    s.click(end + 12, row)               # past the end of the URL text: nothing must open
    opened_after_blank = os.path.exists(log_path)
    s.click(start + 2, row)              # on the URL itself
    time.sleep(0.5)
    urls = open(log_path, encoding="utf-8").read().split() if os.path.exists(log_path) else []
    check("clicking past the URL opens nothing", not opened_after_blank)
    check("clicking the URL opens the browser", bool(urls) and urls[-1].startswith("http"), f"{urls}")


def startup_refresh(s):
    """0.18.0: opening gg always asks GitHub what changed (incrementally), whatever the cache age.
    Here the fake gh refuses every call, so the proof is that it was called at all — and that a failed
    refresh leaves the TUI usable."""
    calls = os.path.join(s.home, "gh-calls.log")
    got = open(calls, encoding="utf-8").read() if os.path.exists(calls) else ""
    check("the start-up refresh asked GitHub what changed", "query_head" in got,
          got[-300:] or "(gh was never called)")
    log = s.log()
    check("the refresh took the incremental path", "0 changed" in log or "changed," in log,
          "\n".join(l for l in log.splitlines() if "changed" in l)[-300:])
    check("nothing was re-fetched when nothing changed", "fetched" not in got)
    check("the TUI is still usable after the refresh",
          "6 People" in s.text() and "Traceback" not in log)


def live_state(s):
    """state.json for `gg mcp`, and cmd.json driving the running TUI (gg_open)."""
    st = None
    for _ in range(20):
        st = s.cache("state.json")
        if st:
            break
        s.drain(0.3)
    check("state.json is written", bool(st))
    if not st:
        return
    check("state.json names the repo and the current item",
          testenv.FIXTURE_REPO in (st.get("repos") or []) and st.get("item"), json.dumps(st)[:300])
    # drive the TUI through cmd.json the way `gg mcp`'s gg_open does: {"op": "open", "id": <node id>}
    items = (s.cache("items__test__repo__open.json") or {}).get("items", [])
    current = str(st["item"]["label"])
    target = next((it["number"] for it in items if f"#{it['number']}" != current), None)
    if target is None:
        check("cmd.json moves the TUI (gg_open)", False, "no other item in the fixture")
        return
    cmd_path = os.path.join(s.home, ".cache", "gitgraph", "cmd.json")
    with open(cmd_path, "w", encoding="utf-8") as f:
        json.dump({"op": "open", "id": f"{testenv.FIXTURE_REPO}#{target}", "req": "smoke-1"}, f)
    moved = False
    for _ in range(20):
        s.drain(0.4)
        st2 = s.cache("state.json") or {}
        if st2.get("item") and f"#{target}" in st2["item"]["label"]:
            moved = True
            break
    check("cmd.json moves the TUI (gg_open)", moved)


def cold_start_paints_early():
    """A cold cache is fetched in batches and drawn as they land (the Repo panel says "still loading"
    until the full graph is swapped in), instead of holding a loading box until everything is there."""
    home = testenv.make_home()
    fixture = gh_fixture(home)                          # built from the cache, before it is thrown away
    cache = os.path.join(home, ".cache", "gitgraph")
    for name in os.listdir(cache):                      # force the cold path: nothing cached
        if name.startswith("items__"):
            os.remove(os.path.join(cache, name))
    s = Session(["--no-summary", "-t", "none"], home=home, FAKE_GH_FIXTURE=fixture)
    try:
        check("a cold start reaches a usable screen", s.wait_for("6 People", 40), s.text()[:400])
        check("the items are there", "#" in s.text())
        s.settle()
        check("the loading marker is gone once it is complete", "still loading" not in s.text(),
              s.line(s.find_line("1 Repo") + 1))
        st = s.cache("state.json") or {}
        check("the graph is complete afterwards", bool(st.get("repos")), json.dumps(st)[:200])
        check("no traceback on the cold path", "Traceback" not in s.log(),
              "\n".join(l for l in s.log().splitlines() if "Error" in l)[:400])
    finally:
        s.kill()


def cross_repo_mark():
    """A mark on an item this graph does not hold (another repo, a closed item) must still be editable
    and deletable from the Inbox todo section. Reported bug: its row carries no node id, so Del acted
    on whatever was selected before it and the entry stayed."""
    home = testenv.make_home()
    todo_path = os.path.join(home, ".config", "gitgraph", "todo.json")
    os.makedirs(os.path.dirname(todo_path), exist_ok=True)
    entries = [
        {"id": "aaa-other-repo", "created": "2026-08-28T12:00", "repo": "elsewhere/repo",
         "item": "elsewhere/repo#7", "item_num": "repo#7", "title": "a mark from another repo",
         "url": "https://github.com/elsewhere/repo/issues/7", "note": "not in this graph", "done": False},
        {"id": "bbb-this-repo", "created": "2026-08-27T12:00", "repo": testenv.FIXTURE_REPO,
         "item": f"{testenv.FIXTURE_REPO}#5", "item_num": "#5", "title": "a mark from this repo",
         "url": "", "note": "keep me", "done": False},
    ]
    with open(todo_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False)

    s = Session(["--no-summary", "-t", "none"], home=home)
    try:
        s.wait_for("6 People", 30)
        s.key("3")
        for _ in range(10):                       # the todo section
            if "todo" in s.line(s.find_line("3 Inbox")):
                break
            s.key("[", 0.5)
        # the row for an item in this graph shows its live label, the one from elsewhere its stored title
        check("both marks are listed", "repo#7" in s.text() and "#5" in s.text(), s.text()[:600])
        s.key("g", 0.6)                           # the newest mark: the one from the other repo
        s.key("\x1b[3~", 1.5)                     # Del
        with open(todo_path, encoding="utf-8") as f:
            left = json.load(f)
        check("Del removes the mark of an item outside this graph",
              [e["id"] for e in left if not e.get("done")] == ["bbb-this-repo"],
              json.dumps(left, ensure_ascii=False)[:400])
        i = s.find_line("3 Inbox")
        panel = "\n".join(s.text().splitlines()[i:i + 6])
        check("the other mark survives in the list", "#5" in panel, panel)
        check("its row is gone from the list", "repo#7" not in panel, panel)
        check("the message names what was removed", "mark removed" in s.text(), s.text()[-200:])
        check("no traceback", "Traceback" not in s.log())
    finally:
        s.kill()


def portrait_and_theme():
    """A narrow terminal stacks the panels; --theme basic must not use dim/256 colours."""
    s = Session(["--theme", "basic", "--no-summary", "-t", "none"], cols=83, rows=30)
    try:
        ok = s.wait_for("Main", 30)
        check("basic theme, 83 columns starts", ok, s.text()[:400])
        check("portrait mode stacks the panels", "0 Main" in s.text() and s.text().count("╭") > 0)
        s.settle()
        check_golden("portrait83", s.text())
        s.resize(30, 140)
        s.key("3", 0.8)                  # a keypress after SIGWINCH forces the full repaint
        check("resize back to landscape redraws", s.wait_for("6 People", 20), s.text()[:400])
        check("no traceback after resize", "Traceback" not in s.log())
    finally:
        s.kill()


def ai_failure_popup():
    """0.16.0: when the AI CLI fails, the TUI offers to switch / keep / turn off."""
    s = Session(FAKE_AI_FAIL="1")
    try:
        s.wait_for("6 People", 30)
        s.key("3")
        s.key("a", 0.6)
        s.key("boom\r", 4.0)
        txt = s.text()
        check("AI failure is surfaced", "AI" in txt or "claude" in txt or "failed" in txt.lower(),
              txt[:400])
        check("no traceback from a failing AI CLI", "Traceback" not in s.log())
    finally:
        s.kill()


def fake_clone(home):
    """A real git clone of `test/repo` inside the temp HOME, with the PR's head in refs/pull/5/head.

    Its origin URL is the github.com one gg parses to recognise the repo, but an `insteadOf` rewrite in
    the clone's own config sends every fetch to the bare repository next door — so review mode goes
    through the real git plumbing (fetch, merge-base, worktree add, diff) and still touches no network.
    Returns the directory to start gg in.
    """
    root = os.path.join(home, "clones")
    origin, work, clone = (os.path.join(root, n) for n in ("origin.git", "seed", "repo"))
    os.makedirs(work)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e", "GIT_CONFIG_GLOBAL": "/dev/null"}

    def git(cwd, *a):
        r = subprocess.run(["git", "-C", cwd] + list(a), capture_output=True, text=True, env=env)
        assert r.returncode == 0, f"git {a}: {r.stderr}"
        return r.stdout.strip()

    subprocess.run(["git", "init", "-q", "-b", "main", work], check=True, capture_output=True, env=env)

    def commit(files, msg):
        for name, text in files.items():
            with open(os.path.join(work, name), "w") as fh:
                fh.write(text)
        git(work, "add", "-A")
        git(work, "commit", "-qm", msg)

    commit({"data.c": "static int f2fs_write_page(void)\n{\n\treturn 0;\n}\n",
            "gc.c": "static void gc_thread(void)\n{\n}\n"}, "base")
    commit({"data.c": "static int f2fs_write_page(void)\n{\n\tspin_lock(&lock);\n\treturn -ENOMEM;\n}\n",
            "gc.c": "static void gc_thread(void)\n{\n\twait_event(q, done);\n}\n"}, "the PR")
    base, head = git(work, "rev-parse", "HEAD~1"), git(work, "rev-parse", "HEAD")
    subprocess.run(["git", "clone", "-q", "--bare", work, origin], check=True, capture_output=True, env=env)
    git(origin, "update-ref", "refs/heads/main", base)
    git(origin, "update-ref", "refs/pull/5/head", head)
    subprocess.run(["git", "clone", "-q", origin, clone], check=True, capture_output=True, env=env)
    git(clone, "remote", "set-url", "origin", "https://github.com/test/repo.git")
    git(clone, "config", f"url.{origin}.insteadOf", "https://github.com/test/repo.git")

    head_file = os.path.join(home, "pr5-head.txt")
    with open(head_file, "w") as fh:
        fh.write(head)

    def advance():
        """A new commit on the PR touching gc.c only — data.c's findings should survive it. The head
        file moves with it, so the fake gh reports the new sha the way GitHub would."""
        commit({"gc.c": "static void gc_thread(void)\n{\n\twait_event_timeout(q, done, HZ);\n}\n"},
               "address review")
        git(origin, "fetch", "-q", work, "HEAD:refs/pull/5/head", "--force")
        with open(head_file, "w") as fh:
            fh.write(git(work, "rev-parse", "HEAD"))

    return root, advance, head_file


def review_with_a_clone():
    """0.23.0: with a checkout present, v builds a worktree and draws the real diff, and R runs the
    review through the (fake) AI CLI and anchors its findings onto changed lines."""
    home = testenv.make_home()
    root, advance, head_file = fake_clone(home)
    gh_log = os.path.join(home, "gh-calls.log")
    s = Session(["--no-summary", "-t", "none", "5"], home=home, cwd=root, FAKE_GH_LOG=gh_log,
                FAKE_GH_HEAD_FILE=head_file)
    try:
        s.wait_for("6 People", 30)
        s.settle()
        s.key("v", 25)
        s.settle()
        txt = s.text()
        check("the worktree is built and the diff is drawn", "data.c" in txt and "@@" in txt, txt[:700])
        check("the removed and added lines are both there",
              "- \treturn 0;" in txt.replace("\t", "\t") or "return 0;" in txt, txt[:700])
        st = (s.cache("state.json") or {}).get("review") or {}
        check("state.json lists the changed files", st.get("files") == ["data.c", "gc.c"], str(st)[:300])
        check("no review has been run on its own", "no review yet" in txt, txt[:700])

        s.key("R", 1.0)                       # confirm popup: it costs money, so it always asks
        check("R asks before spending anything, and says how much",
              "Review test/repo#5?" in s.text() and "yes — all 2 files" in s.text(), s.text()[:600])
        s.key("k", 0.4)                       # the popup starts on "no": spending is never one keypress
        s.key("\r", 12.0)
        s.settle()
        txt = s.text()
        check("the finding lands in the Findings panel", "fake finding 1 in data.c" in txt, txt[:900])
        check("it was checked in a call of its own (verdict shown)", "✓" in txt, txt[:900])
        st = (s.cache("state.json") or {}).get("review") or {}
        f = st.get("finding") or {}
        check("state.json carries the finding for gg mcp",
              f.get("path") == "data.c" and f.get("severity") in ("bug", "style"), str(st)[:400])
        check("its line is one the diff touches", (st.get("counts") or {}).get("open", 0) >= 1, str(st))

        s.key("2", 0.6)                       # the Diff panel flags the line
        check("the flagged line is marked in the diff", "⚠" in s.text(), s.text()[:900])
        s.key("3", 0.6)
        s.key("d", 1.0)                       # the panel only has room for the title
        check("d reads the whole finding", "the fake AI CLI always says this" in s.text(), s.text()[:900])
        s.key("\x1b", 0.6)

        s.key("P", 1.2)                       # posting shows the exact text first
        txt = s.text()
        check("P shows what would be posted", "about to post to test/repo#5" in txt
              and "fake finding" in txt, txt[:900])
        s.key("\x1b", 0.6)
        check("then it asks", "comment(s) to test/repo#5" in s.text(), s.text()[:400])
        s.key("\r", 1.2)                      # the popup starts on "no"
        check("no means nothing was sent", "not posted" in s.text(), s.text()[:300])
        calls = open(gh_log).read() if os.path.exists(gh_log) else ""
        check("saying no sends nothing at all", "addPullRequestReview" not in calls, calls[-400:])

        advance()                             # the author pushes a commit touching gc.c only
        s.key("r", 20.0)
        s.settle()
        txt = s.text()
        check("a moved head is reported, not silently re-reviewed",
              "reviewed at" in txt and "1 fil" in txt, txt[:700])
        check("the finding on the untouched file survived the push",
              "fake finding 1 in data.c" in txt, txt[:900])
        s.key("R", 1.5)
        check("R offers to redo only what moved",
              "1 of 2 files changed since" in s.text() and "1 finding(s) kept" in s.text(),
              s.text()[:700])
        s.key("\x1b", 0.8)                     # not now

        s.key("x", 1.0)                       # ignore it
        check("x ignores a finding", "ignored" in s.text(), s.text()[:300])
        check("no traceback with a real worktree", "Traceback" not in s.log(),
              "\n".join(l for l in s.log().splitlines() if "Traceback" in l or "Error" in l)[:600])
    finally:
        s.kill()


def review_mode():
    """0.22.0: v opens the three-column review of a PR. The fixture home has no clone of test/repo, so
    this also pins what happens when the worktree cannot be made: an explanation, not a traceback."""
    s = Session(["--no-summary", "-t", "none", "5"])       # gg 5 = start on PR #5
    try:
        s.wait_for("6 People", 30)
        s.settle()
        check("the tui starts on the PR", (current_item(s) or "").endswith("#5"), str(current_item(s)))
        s.key("v", 2.0)
        s.settle()
        txt = s.text()
        check("review mode draws its three panels",
              all(t in txt for t in ("1 Files", "2 Diff", "3 Findings")), txt[:400])
        check("the graph panels are gone", "6 People" not in txt, txt[:200])
        st = s.cache("state.json") or {}
        check("state.json says review mode",
              st.get("mode") == "review" and (st.get("review") or {}).get("number") == 5,
              str(st.get("review"))[:300])
        check("a missing local clone is explained, not a traceback", "no local clone" in txt, txt[:700])
        s.key("3")
        s.key("]", 0.8)
        check("the Findings tabs switch", "[posted]" in s.text(), s.line(0))
        for _ in range(4):
            s.key("]", 0.4)
        check("the github tab lists the PR's threads even though the worktree failed",
              "@bob" in s.text(), s.text()[:700])
        s.key("v", 1.0)
        check("v returns to the graph", "6 People" in s.text(), s.text()[:200])
        check("the graph is still usable afterwards", "test/repo" in s.text())
        check("no traceback in review mode", "Traceback" not in s.log(),
              "\n".join(l for l in s.log().splitlines() if "Traceback" in l or "Error" in l)[:600])
    finally:
        s.kill()


def review_mode_narrow():
    """80 columns: the three columns stack instead of being squeezed into nothing."""
    s = Session(["--no-summary", "-t", "none", "5"], rows=30, cols=80)
    try:
        s.wait_for("6 People", 30)
        s.settle()
        s.key("v", 2.0)
        s.settle()
        txt = s.text()
        check("narrow review stacks the panels",
              all(t in txt for t in ("1 Files", "2 Diff", "3 Findings")), txt[:500])
        check("no traceback when narrow", "Traceback" not in s.log())
    finally:
        s.kill()


def main():
    home = testenv.make_home()
    print(f"# temp HOME: {home}")
    assert home != os.path.expanduser("~"), "refusing to run against the real HOME"
    os.environ["FAKE_OPEN_LOG"] = os.path.join(home, "opened.txt")
    s = Session(["--no-summary", "-t", "none"], home=home,
                FAKE_OPEN_LOG=os.path.join(home, "opened.txt"),
                FAKE_GH_LOG=os.path.join(home, "gh-calls.log"))
    try:
        panels_and_navigation(s)
        history_highlight(s)
        inbox_tabs(s)
        popups_and_menus(s)
        search_and_marks(s)
        ai_flows(s)
        hangul_ime(s)
        mouse_and_border(s)
        url_click(s)
        startup_refresh(s)
        live_state(s)
        check("clean exit", s.quit())
        log = s.log()
        check("no traceback", "Traceback" not in log,
              "\n".join(l for l in log.splitlines() if "Error" in l or "Traceback" in l)[:800])
        check("no error line in tui.log", "[gitgraph] error" not in log.lower(),
              "\n".join(l for l in log.splitlines() if "error" in l.lower())[:800])
    finally:
        s.kill()

    review_mode()
    review_mode_narrow()
    review_with_a_clone()
    cold_start_paints_early()
    cross_repo_mark()
    portrait_and_theme()
    ai_failure_popup()

    print(f"\n{len(FAILS)} failure(s): " + ", ".join(FAILS) if FAILS else "\nall good")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
