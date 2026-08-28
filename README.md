# gg (gitgraph)

[한국어](README.ko.md)

Draws how GitHub issues, PRs, comments and `@mentions` link to each other as an ASCII graph — on the command line or in a curses TUI with keyboard and mouse. Works with github.com and GitHub Enterprise. No Python package dependencies.

```
2026-08-13 #750 [PR] mtfs: running out of space must not disable the filesystem  @Daejun7Park  (1 comments, 1 linked)
├─ → refs 2026-08-13 #748 [I] Filling the device leaves the filesystem permanently unmountable  @Daejun7Park
│  ├─ ⇢ ← cited-by #749 #763:o+1d · comment #748:o+0d
│  └─ +0d o @Daejun7Park » #749/#750 posted; checkpoint retry storm fix and measurements
└─ → refs 2026-08-13 #749 [PR][closed] recovery: a full inode zone is the end of the scan, not a bad address  @Daejun7Park
```

## Install

Requirements: python3 ≥ 3.9 and the `gh` CLI, logged in (`gh auth login`; for GitHub Enterprise `gh auth login -h host`). The `claude` CLI (or a compatible variant, see *Settings*) is optional — it is used only for translation, comment summaries and questions; without it those features are off and the graph still works.

```
pipx install git+https://github.com/Daejun/gitgraph        # recommended (no pipx: pip install --user git+…)
# or a single file:
curl -fsSL https://raw.githubusercontent.com/Daejun/gitgraph/main/gitgraph.py -o ~/.local/bin/gg && chmod +x ~/.local/bin/gg
gg update                                                    # later: refresh from GitHub (works for all three install styles)
gg --version
```

### Which repo

Run `gg` inside a git repo, or in a directory that contains repos (2 levels deep): every remote whose URL points at a GitHub host (`github.com`, any `github.*` host, or a host you are logged in to with `gh`) is a candidate — `origin` first, then remotes named `github*`, so a repo whose `origin` is GitLab but has a `github` remote works too; if several are found you are asked which (`a` = all, the first is *primary* and its items are shown as bare `#N`). If the repo found is a fork, gg switches to the repo it was forked from (that is where the issues/PRs are; `-r` the fork explicitly to look at the fork). Otherwise pass `-r owner/name` (repeatable) or store a default: `gg config repos owner/name`. GitHub Enterprise repos are written `host/owner/name`.

## Usage

```
gg                        # overview of everything open, as a tree
gg -l log                 # git log --graph style timeline
gg 768 --hops 1           # neighbourhood of #768 (also #768, owner/repo#768)
gg @someone               # around a person
gg show 748               # one node in detail: every edge, comments, body
gg ask 4563 "why does it mention #3859?"   # one-shot question to claude, with the item as context
gg tui [768]              # interactive TUI; a number starts on that tree
gg config [KEY [VALUE]]   # persistent settings
gg todo                   # print the markdown of everything marked with m in the tui
gg check [-r owner/name]  # diagnose: gh accounts for the host, access per account, open counts, GraphQL fields
gg update                 # update this installation
```

Options: `-r owner/name` · `-u LOGIN` (view as that person in the TUI home; only the perspective changes, not the gh login) · `--state open|all` (all fetches every issue/PR — slow) · `--comments linked|all|none` (linked = only comments that reference `#N`/`@someone`, default for graph/show; all = default for tui) · `--no-people` · `--no-closed-neighbors` · `--max-age MIN` (cache TTL, default 15) · `--refresh` · `-w N` (title width) · `-t zh|all|none` (translation, default zh) · `-S` (comment summaries) · `--color auto|always|never` · `--theme dark|light|basic` · TUI only: `--days N` (home window, 7), `--no-summary`.

## Settings (`gg config`)

`gg config` shows every setting and where it comes from; `gg config KEY VALUE` stores it in `~/.config/gitgraph/config.json`; `gg config unset KEY` removes it. Precedence: CLI option > `GITGRAPH_*` environment variable > config file > default.

| key | env | default | meaning |
|---|---|---|---|
| `claude_bin` | `GITGRAPH_CLAUDE` | `claude` | binary for translation / summaries / questions. A Claude-compatible variant (e.g. `cla`) gets the same arguments (`-p --no-session-persistence --output-format json <prompt>`) **except `--model`** — it keeps its own default model |
| `repos` | `GITGRAPH_REPOS` | | default repos, comma separated |
| `me` | `GITGRAPH_ME` | gh accounts | logins that count as "me" in the TUI home |
| `lang` | `GITGRAPH_LANG` | `Korean` | language of translations, summaries and answers |
| `translate` | `GITGRAPH_TRANSLATE` | `zh` | `zh` (Chinese only) / `all` / `none` |
| `tr_model` · `ask_model` | `GITGRAPH_TR_MODEL` · `GITGRAPH_ASK_MODEL` | `haiku` · `sonnet` | models (real `claude` only) |
| `batch` | `GITGRAPH_BATCH` | `10` | TUI: nodes per translate/summary call |
| `retries` | `GITGRAPH_RETRIES` | `3` | `gh api` retries on transient network errors |
| `side_width` · `expand_focused` · `expanded_weight` · `screen_mode` · `border` | `GITGRAPH_SIDE_WIDTH` … | `0.4` · `true` · `2` · `normal` · `rounded` | TUI layout (see below) |
| `todo_file` | `GITGRAPH_TODO` | `~/gitgraph-todo.md` | markdown written from the marks made with `m` |
| `theme` | `GITGRAPH_THEME` | `dark` | colour theme, like vim's `bg=`: `dark` (256 colours), `light` (darker tones for a light background), `basic` (8 colours, no dim, no dark blue — PuTTY and other plain terminals). `--theme` for one run, `T` in the TUI to cycle |

## Line format

```
2026-08-13 #750 [PR] title  @author        # date = when the issue/PR was opened; [PR] blue, [I] green
  +5d o @author » summary                  # comment: +N days after opening; » = one-line summary
```

`[draft]` / `[merged]` / `[closed]` appear only when an item is not simply open. Every `@login` gets a colour of its own (256-colour palette, assigned in order of first appearance). Colours are on when stdout is a terminal (`--color`).

## Edges

| edge | source |
|---|---|
| `→ refs` / `← cited-by` | `#N`, `owner/repo#N` or full URLs in bodies and comments, plus GitHub's CROSS_REFERENCED timeline events (references coming from closed items). `→` = this item references that one, `←` = that one references this. |
| `→ closes` / `← closed-by` | the PR's `closingIssuesReferences` |
| `→ mentions` / `← mentioned-by` | `@login` (2+ chars, not all digits) |
| `o` | issue comments, PR review bodies, inline review comments |
| `⇢ …` | a link to a node drawn elsewhere in the same tree (`#N:o+5d` = the comment on #N made 5 days after it opened) |

`#N` inside code fences and in pasted kernel logs / stack traces (`[ 123.4]`, `Tainted:`, `PID:`, `PLATFORM --` …) is ignored. Small numbers (≤ 20) count only after a reference word such as `PR`, `issue`, `see`, `fixes` (so `overwrite #5` is not a reference). Trees are shortest-path trees from the most-linked open item of each connected component; the header says which one (`tree rooted at #672 (most linked: 9 items)`).

## Translation, summaries, questions (`claude`)

- **Translation** — titles and comment first lines containing Chinese characters (`-t zh`) are translated into `lang` with `tr_model`; `-t all` also translates English. Cached in `~/.cache/gitgraph/translations.json`; `show` prints the original title too.
- **Summaries** (`-S`, default in the TUI) — each comment line shows a one-line summary (issues/PRs get one too, used by the Links panel) (`» …`) instead of its first line, cached by the body's sha1 in `summaries.json` (≤ 4,000 chars per body, ≤ 40 comments / 40,000 chars per call). While one is being generated the line reads `» summarizing…` (`» 요약 중…` in Korean); on failure the first line is shown.
- **Questions** (`a`, `gg ask`) — the question goes to `ask_model` with the item (or the comment under the cursor, marked) plus its metadata, the whole comment thread in order and the linked issues/PRs with the sentence that made each link (up to 90,000 chars); the answer names the comment or #number it relies on. One-shot, no cache.
- Only the Claude Code login is needed, no API key. Usage reported by `--output-format json` is accumulated from process start: the TUI title bar shows `tokens 31.2k in / 1.4k out · $0.052 · 7 calls`, `$` shows a per-phase table; the CLI prints one line on stderr at exit. Most of "in" is cached system prompt, which is cheap.

## TUI

lazygit-style layout: a side column of panels (Repo · Item · Home · Links · Comments · People) and a main panel that shows whatever is selected.

```
╭─1 Repo──────────────────────╮╭─0 Main [content] answer───────────────────────────────╮
│ owner/name  57 open  15:44  ││ #750 [PR] mtfs: running out of space must not …       │
╰─────────────────────────────╯│ @Daejun7Park  2026-08-13  updated 2026-08-19          │
╭─2 Item──────────────────────╮│ https://github.com/owner/name/pull/750                │
│ 2026-08-13 #750 [PR] mtfs: …││                                                       │
│   » one-line summary        ││                                                       │
╰─────────────────────────────╯│                                                       │
╭─3 Home ‹ my turn 2 › 1/9────╮│ Running the device out of space under concurrent …   │
│ 2026-08-17 #763 [I] xfstest…││                                                       │
╭─4 Links─────────────────────╮│                                                       │
│ → refs 2026-08-13 #748 [I] …││                                                       │
╭─5 Comments──────────────────╮│                                                       │
│ +0d o @Daejun7Park » …      ││                                                       │
╭─6 People────────────────────╮│                                                       │
│ @Daejun7Park  author        ││                                                       │
╰─────────────────────────────╯╰───────────────────────────────────────────────────────╯
 ⏎ open item  [ ] section  / search  a ask  o browser   1-5 0 Tab panels  + _ screen  b f back/fwd  ? keys  q quit
```

| panel | shows | Enter |
|---|---|---|
| 1 Repo | repo, open count, fetch time, "me", toggles, token usage | – |
| 2 Item | the current item: title, metadata, `» one-line summary` (or the first line of its body), comment/link counts, URL | read it in main |
| 3 Home | one section at a time (`[` `]`): my turn · todo · mentions · opened · active · waiting · mine · PRs by others · stale · all — same rules as before ("my turn" = items I am in where someone else spoke last, `--days N` window, "me" = gh accounts / `-u` / `u`). In my turn / active / waiting / PRs rows the cursor previews the **latest comment** (that is why main shows a comment there); my turn rows end with why it is on me: `⟵ @who mentioned +5d`, `@who on my PR +14d`, `@who replied +2d` | make it the **current item** — Item, Links, Comments and People follow |
| 4 Links | every edge of the current item and its comments: `→ refs`, `← cited-by`, `→ closes`, `← closed-by` (via which comment). Under each link a `↳` note says **why**, briefly: a one-line reason written by the summarizer from the sentence that made the reference (e.g. `충돌 여부를 확인한 관련 PR`; a short quote around `#N` until it arrives, or when summaries are off), or — when there is no such text (references recorded only by GitHub's timeline, closed items) — a one-line summary of that issue/PR (`» …`, made by the same summarizer as comments once its body has been fetched; `» summarizing…` while pending) | go to that item; on a comment row: its item, with the cursor on that comment (back with `b`/Esc) |
| 5 Comments | the current item's comments `+Nd o @who » summary`, newest on top | read it in main |
| 6 People | author, commenters and mentioned people of the current item, most recently active first | view Home as that person |
| 0 Main | tabs (`[` `]`): **content** = full text of the row under the cursor in the focused side panel (URL first, underlined; body + metadata, or a comment) — it stays put while main itself is focused; **answer** = the last `a` question. Both render markdown: headings, code blocks and `code`, **bold**, links, quotes, bullets | – |

Layout: side column `side_width` (0.4) of the screen; titles are truncated to what fits and re-fitted whenever the widths change (drag, resize, screen mode); the focused side panel is taller (`expand_focused`, `expanded_weight`) and stays that way while main is focused, so the next pick is easy; `+`/`_` cycle screen modes normal → half (the focused panel fills its column) → full (only that panel); narrow terminals (≤ 84 columns) stack the focused side panel above the main panel; borders `border` (rounded · single · double · bold · hidden). Translation and summaries run in the background for the visible rows first (`batch` per call); `» summarizing…` marks a pending one.

| key | action |
|---|---|
| `1`-`6` · `0` · Tab / Shift-Tab | jump to a panel · main · cycle |
| `[` `]` | previous / next tab of the focused panel |
| `+` `_` | screen mode normal → half → full |
| `↑`/`k` `↓`/`j` · PgUp/PgDn `,` `.` · `g`/`G` `<`/`>` · `H`/`L` | move · page · top/bottom · scroll sideways |
| `K` `J` | scroll the main panel from anywhere |
| Enter | see the table above |
| `i` | translate the main content (issue/PR body or comment) in full into `lang`; again = original (cached in `translations_full.json`) |
| `m` | mark the selected issue/PR or comment for my next work and write a note; marked rows show `✎`, Home gets a **todo** section (second tab, after my turn), and the markdown file `todo_file` (default `~/gitgraph-todo.md`; `gg todo` prints it) is rewritten so the next session — or Claude — can pick the work up. `m` again on a marked row: edit the note / mark done / remove. Source of truth: `~/.config/gitgraph/todo.json` |
| `a` · `d` · `o` | ask claude about the selection (answer tab) · details pager · open in the browser (the URL is also the first, underlined line of the content) |
| Esc / `b` · `f` | back (previous item and perspective) · forward |
| `u` · `r` | view Home as another person · refetch |
| `c` `t` `s` `p` `h` | comments mode · translation · summaries · people nodes · hops 1/2/3 |
| `/` `n` `N` · `T` · `$` · `q` | search in the focused panel · colour theme · token usage · quit |
| `?` · `O` · F1 | key menu for the focused panel (Enter runs the action) · options menu (comments / translation / summaries / people / hops / theme / screen) · full help text |
| Hangul IME | shortcuts work while the keyboard is in Hangul mode: the jamo/syllable is mapped back to the 2-set layout keys (`ㅓ` = j, `ㅏ` = k, `자` = w k) |
| mouse | click = focus + select; click a URL line = open it in the browser; double-click = Enter; wheel = scroll that panel without moving the cursor; back/forward buttons; drag the border between the side column and main to resize (`gg config side_width` keeps it) |

Prompts (`a`, `/`, `u`), menus (`?`, `O`), confirmations (`r`) and text (`d`, `$`, F1) open as centred popups; Esc closes them.

Under tmux enable mouse reporting (`set -g mouse on`).

Smoke test: `GITGRAPH_REPOS=owner/name python3 tests/tui_smoke.py` drives the TUI in a pseudo-terminal, renders it with `tests/vt.py` and checks the screen.

## GitHub Enterprise

Repos on an Enterprise host are written `host/owner/name` (`-r ghe.example.com/team/proj`, or found from a remote such as `git@ghe.example.com:team/proj.git`). API calls use `gh api --hostname` with the accounts/tokens gh holds for that host; references in bodies resolve to the item's own host. Not yet verified against a real Enterprise instance — please report what breaks.

## Accounts, network, cache

- If a private repo answers `NOT_FOUND`, the other accounts registered in `gh auth status` for that host are tried automatically (no global account switch); the error names the host and the accounts tried.
- Transient network errors (`TLS handshake timeout`, connection reset, 5xx) are retried `retries` times with backoff; then `gg` prints what to check (`gh api user`, `HTTPS_PROXY`, VPN/DNS).
- Cache: `~/.cache/gitgraph/` — items per repo and state (`--max-age`, `--refresh`), translations, summaries. TUI logs (and stderr) go to `~/.cache/gitgraph/tui.log`.

## License

MIT
