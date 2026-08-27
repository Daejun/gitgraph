#!/usr/bin/env python3
"""Drive `gg tui` in a pseudo-terminal, render its output with the small VT emulator in vt.py and check the screen.

    GITGRAPH_REPOS=owner/name python3 tests/tui_smoke.py        # needs gh login and a fetched/cached repo

Not a unit test: it exercises the real program end to end (no claude calls: --no-summary -t none).
"""
import os
import pty
import select
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from vt import Screen  # noqa: E402

ROWS, COLS = 34, 140


class Session:
    def __init__(self, args):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.environ.update(TERM="xterm-256color", LINES=str(ROWS), COLUMNS=str(COLS), GG_DEBUG="1")
            os.execvp(sys.executable, [sys.executable, os.path.join(os.path.dirname(__file__), "..", "gitgraph.py"), "tui"] + args)
        self.scr = Screen(ROWS, COLS)

    def drain(self, t):
        end = time.time() + t
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                try:
                    self.scr.feed(os.read(self.fd, 65536).decode("utf-8", "replace"))
                except OSError:
                    return

    def wait_for(self, text, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            self.drain(0.5)
            if text in self.scr.text():
                return True
        return False

    def key(self, data, wait=0.6):
        os.write(self.fd, data if isinstance(data, bytes) else data.encode())
        self.drain(wait)

    def mouse(self, code, x, y, press=True):
        os.write(self.fd, f"\x1b[<{code};{x + 1};{y + 1}{'M' if press else 'm'}".encode())

    def text(self):
        return self.scr.text()

    def line(self, i):
        return self.scr.text().splitlines()[i]

    def quit(self):
        self.key("q", 0.3)
        time.sleep(1)
        pid, st = os.waitpid(self.pid, os.WNOHANG)
        return pid != 0 and st == 0


def main():
    fails = []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    s = Session(["--no-summary", "-t", "none"])
    check("panels drawn", s.wait_for("6 People") and "0 Main" in s.line(0))
    s.key("j")
    s.key("\r", 2.0)
    check("Enter sets the item", "#" in s.line(5))
    check("Item panel shows the item", "2 Item" in s.text() and "#" in s.line(5))
    s.key("4")
    s.key("j")
    s.key("\r", 2.0)
    item_after_link = s.line(5)
    check("Links notes", "↳" in s.text())
    s.key("b", 2.0)
    check("Links Enter then back", item_after_link != s.line(5))
    s.key("0")
    s.key("]", 1.0)
    check("main answer tab", "[answer]" in s.line(0))
    s.key("+", 0.8)
    check("half screen: only main", "1 Repo" not in s.text())
    s.key("_", 0.8)
    s.key("3")
    s.key("]", 1.0)
    check("home section tab", "2/9" in s.text())
    s.key("?", 1.0)
    check("key menu popup", "keys — Home panel" in s.text())
    s.key("\x1b", 0.5)
    s.key("O", 1.0)
    check("options popup", "options" in s.text() and "comments:" in s.text())
    s.key("\x1b", 0.5)
    s.key("/", 0.5)
    s.key("한글 검색\x1b", 0.8)       # typed then cancelled: the prompt must accept wide characters without crashing
    s.key("a", 0.5)
    check("prompt popup", "ask about" in s.text())
    s.key("\x1b", 0.5)
    # drag the border from the current position 15 columns to the right
    home_row = next(i for i, l in enumerate(s.text().splitlines()) if "3 Home" in l)
    bx = next((i for i, ch in enumerate(s.line(home_row)) if ch == "╮"), None)   # Home's top border
    if bx is not None:
        s.mouse(0, bx, 10)
        s.mouse(32, bx + 8, 10)
        s.mouse(32, bx + 15, 10)
        s.mouse(0, bx + 15, 10, press=False)
        s.drain(0.8)
        nbx = next((i for i, ch in enumerate(s.line(home_row)) if ch == "╮"), None)
        check("border drag widens the side column", nbx is not None and nbx > bx + 5)
    else:
        check("border found", False)
    check("clean exit", s.quit())
    log = open(os.path.expanduser("~/.cache/gitgraph/tui.log")).read()
    check("no traceback", "Traceback" not in log)
    print(f"\n{len(fails)} failure(s)" if fails else "\nall good")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
