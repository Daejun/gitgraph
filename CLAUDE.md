# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`gg` (gitgraph): a CLI + curses TUI that draws how GitHub issues, PRs, comments and `@mentions` reference
each other. Everything lives in **one module, `gitgraph.py` (~5,500 lines)** — installed as the `gg`
console script (`pyproject.toml`), and also runnable as a single downloaded file (`gg update` handles
git checkout / pipx / pip / single-file installs alike).

No Python package dependencies, python3 ≥ 3.9 only. Runtime needs the `gh` CLI (logged in); an AI CLI
(`claude` by default, also `codex`/`gemini`/`grok`) is optional — without it translation, summaries and
`ask` are off and everything else still works. Keep it that way: no new imports outside the stdlib, and
no feature that breaks when the AI CLI is missing.

`README.md` (English) and `README.ko.md` (Korean) document all user-facing behaviour — options, config
keys, edge kinds, every TUI key. Read them before changing behaviour; **they are kept in sync, so a
user-visible change means editing both.**

## Commands

```
python3 gitgraph.py [args]                                  # run from source (same argv as `gg`)
python3 -m py_compile gitgraph.py                           # syntax check — run after every edit
python3 tests/run.py                                        # everything: compile → unit → golden → smoke
python3 tests/run.py unit                                   # one stage (also: golden, smoke)
python3 tests/run.py -k parse                               # by name
python3 -m unittest tests.test_parse.TestRefs.test_fence    # a single test (stdlib unittest)
GG_UPDATE_GOLDEN=1 python3 tests/tui_smoke.py               # rewrite tests/golden/tui_*.txt from this run
GG_DEBUG=1 python3 gitgraph.py                              # log every key / mouse event to tui.log
tail -f ~/.cache/gitgraph/tui.log                           # TUI stderr + progress (curses hides it)
python3 gitgraph.py check                                   # diagnose gh accounts / access / GraphQL fields
python3 gitgraph.py cache                                   # what is cached locally, sizes and ages
```

There is no lint config. The suite is stdlib `unittest` plus one pty end-to-end test, and **all of it runs
offline**: `tests/env.py` builds a throwaway `HOME` with the fixture repo of `tests/fixtures/repo.json`
already "fetched" into its cache, puts `tests/fakes/` first on `PATH` (a fail-loud `gh`, a deterministic
`claude`, an `xdg-open` that records URLs) and asserts `CACHE_DIR` landed inside that temp home. **Never
let a test touch the real `~/.cache/gitgraph`, `~/.config/gitgraph` or `~/gitgraph-todo.md`** — one of
them deletes files. Use `env.load_module()` (it must import `gitgraph` before anything else does, since
the module resolves CONFIG/CACHE_DIR/ME/THEME at import time) and `env.child_env()` for subprocesses.

`tests/tui_smoke.py` forks a pty, drives the real program with keystrokes, renders the output with the
tiny VT emulator in `tests/vt.py`, prints one `ok`/`FAIL` line per check, compares whole screens against
`tests/golden/tui_*.txt` (timestamps, ages, spinner and progress lines normalised away) and ends by
asserting the temp `tui.log` holds no `Traceback` and no error line. Add a check there for any new TUI
behaviour, and a golden under `tests/golden/rows_*.txt` for a new renderer.

Tests assert **current** behaviour: run the function, look at what it returns, then write the assertion.
Where the current behaviour looks wrong, pin it in a `test_suspected_bug_*` with the reason rather than
changing `gitgraph.py` in the same step.

Manual verification still matters for curses work: run it in a real terminal, including PuTTY with
`--theme basic` (8 colours) and a narrow window (≤ 84 columns switches to the stacked portrait layout).

## Architecture

The file is a straight pipeline, and its `# ---` section banners mark the stages:

```
repo discovery → gh GraphQL fetch + cache → graph model → filters → row renderers → styling
                                                                                    ├→ CLI: ANSI
                                                                                    └→ TUI: curses
```

**Repo discovery** — every git remote pointing at a GitHub host, `origin` first; forks resolve to their
parent (that is where the issues live). Enterprise repos are written `host/owner/name`, and every repo id
carries its host through the whole pipeline (`split_repo`/`qualify`/`repo_host`).

**Fetch** (`graphql`, `load_items`) — every query is a `gh api graphql` process (~0.4s before any
network), so the shape of the fetch is what costs time. A cold cache lists the open numbers first
(`list_open`, both connections paged at once) and then pulls the records in aliased batches through
`fetch_groups`, `FETCH_PARALLEL` queries at a time with the batch size chosen to fill every slot;
pagination inside one connection is the only part that must stay sequential. Past `--max-age` the same
listing feeds `refresh_items`, which re-fetches just what changed. Retried on transient errors, with
multi-account fallback: on `NOT_FOUND` the other accounts `gh auth status` knows for that host are tried
with their own token, without switching the global gh account. Which account can see a repo is worth
knowing *before* the first call — a wrong first guess costs a whole round trip — so it comes from the
checkout's git config (`git_account_hint`) and is then remembered per repo in `accounts.json`
(`_prefer_account`). Cached per repo under `~/.cache/gitgraph/`.

**Graph model** (`Node`, `Graph`, `build_graph`) — three node kinds (`item`, `comment`, `person`) and four
edge types (`ref`, `closes`, `mention`, `comment`). Edges come from parsing bodies and comments
(`parse_refs_ctx` — code fences, kernel logs and implausible small numbers are filtered out) plus
GitHub's timeline cross-references for sources that were not parsed. Two side maps hang off the graph:
`g.ctx[(src, dst)]` = the sentence that made a reference, `g.why[(src, dst)]` = the AI one-line reason
shown under a link. Items only referenced (closed, other repos) start as *stubs* and are filled in later.

**Rows are the display contract.** Every renderer (`tree_rows`, `log_rows`, `home_sections`,
`links_rows`, `comments_rows`, …) yields `Row(text, nid, jump, kind)`; `segments()` splits a row's text
into `(text, style)` pairs; `ansi_rows()` maps styles to ANSI for the CLI and `Tui.style_attr()` to curses
attributes. New output means producing Rows — never print or draw directly, or it will lack colour in one
of the two frontends and be invisible to the cursor logic.

**AI layer** — `claude_call()` → `_ai_call()` dispatches per backend (`claude -p --output-format json`
with token/cost accounting into `USAGE`; `codex exec -o FILE`; anything else `-p PROMPT`). Prompts and
their caps live next to their callers (`TR_PROMPT`, `SUM_PROMPT`, `ASK_PROMPT`, `TR_FULL_PROMPT`).
Results are cached by text hash in JSON files merged through `cache_merge()` under a lock, because
several jobs finish at once. Every failure is appended to `AI_FAILURES`, which the TUI turns into the
"switch to another AI CLI / keep / turn off" popup.

**TUI** (`Panel`, `Tui`, ~2,100 lines) — lazygit-style: a side column of panels
(Repo · Item · Inbox · Comments · Links · People) plus a main panel showing whatever is selected.
`Panel` owns rows/cursor/scroll/tabs; `Tui.layout()` computes rectangles from `side_width`,
`expand_focused`, `screen_mode` and portrait mode; `Tui.draw()` overwrites cells and only clears on
resize (clearing every frame flickers on PuTTY), and popups repaint just their own box. All slow work
runs in daemon threads via `run_bg()` and is reaped in the main loop; `enrich()` starts up to
`AI_PARALLEL` small AI jobs at a time for the nodes in the *visible* rows first, so summaries never
starve behind a long translation. A cold cache is drawn while it is still arriving: `Tui.load()` feeds
each fetched batch through `assemble_graph(..., resolve=False)` (the network-free half of
`build_graph`) and the finished graph is swapped in by the same `_new_g` path a background refresh
uses. `docs/PLAN-lazygit-layout.md` (Korean) is the design doc for this
layout and its lazygit↔`gg config` mapping.

**Live state + MCP** — the TUI writes `~/.cache/gitgraph/state.json` every frame and polls `cmd.json`,
so `gg mcp` (a stdio JSON-RPC server in the same file, tools `gg_state`/`gg_context`/`gg_todo`/`gg_show`/
`gg_graph`/`gg_open`/`gg_mark`/`gg_todo_done`) can read what the user is looking at and drive the running
TUI. Marks made with `m` are stored in `~/.config/gitgraph/todo.json` (the source of truth) and mirrored
to a markdown file; answers go to `qa.json` anchored to the item or comment.

## Conventions

- **Config**: `CONFIG_KEYS` at the top is the single source — a new setting is one entry there, read with
  `cfg(key)`. Precedence is CLI option > `GITGRAPH_*` env > `~/.config/gitgraph/config.json` > default,
  and `gg config` documents itself from that table.
- **Version**: `VERSION` at `gitgraph.py:33` is what `gg --version` and `gg update` compare;
  `pyproject.toml` carries the same number for the packaged install — bump both when releasing.
- **Commit messages**: `<version>: what changed, in user-visible terms` on one line, lowercase, several
  clauses separated by commas or semicolons (see `git log`). Docs-only commits are `README: …`.
- **Terminal width**: East-Asian wide characters and CJK are everywhere in this data. Use `dw()`,
  `trunc()`, `clip()`, `slice_cols()`, `wrap()`, `char_at()` — never `len()` — for anything measured in
  columns.
- **Caches are disposable** and hold private-repo bodies: files are 0600 under a 0700 directory
  (`secure()`), `cache_hygiene()` drops unused repo data after 30 days and caps the AI caches and the log.
  Anything new written under `~/.cache/gitgraph/` needs an entry in `CACHE_KINDS` so `gg cache` can list
  and clear it.
- Korean strings appear in user-facing text when `lang` is Korean (`PENDING_TEXT`, `⟳ 번역 중…`,
  `WEAK_WHY`); keep the English fallback beside them.
