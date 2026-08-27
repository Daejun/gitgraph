# gg (gitgraph)

[한국어](README.ko.md)

Draws how GitHub issues, PRs, comments and `@mentions` link to each other as an ASCII graph, on the command line or in a curses TUI. No Python package dependencies.

## Install

Needs python3 ≥ 3.9 and the `gh` CLI (`gh auth login` done). The `claude` CLI is optional: it is used only for translation, comment summaries and questions; without it those features are silently off and the graph still works.

```
pipx install git+https://github.com/Daejun/gitgraph        # recommended (no pipx: pip install --user git+…)
# or a single file:
curl -fsSL https://raw.githubusercontent.com/Daejun/gitgraph/main/gitgraph.py -o ~/.local/bin/gg && chmod +x ~/.local/bin/gg
gg update                                                    # later: refresh from GitHub
```

Run it inside a git repo (or a directory that contains repos): the GitHub repo is taken from `origin`; if several are found you are asked which. Or pass `-r owner/name`. For a fixed repo put `export GITGRAPH_REPOS=owner/name` in your shell rc.

Environment: `GITGRAPH_REPOS`, `GITGRAPH_ME` (logins that count as "me"), `GITGRAPH_LANG` (default Korean), `GITGRAPH_TRANSLATE` (zh|all|none), `GITGRAPH_TR_MODEL` (haiku), `GITGRAPH_ASK_MODEL` (sonnet), `GITGRAPH_BATCH` (10).

## Settings (`gg config`)

`gg config` shows every setting and where it comes from; `gg config KEY VALUE` stores it in `~/.config/gitgraph/config.json`; `gg config unset KEY` removes it. Precedence: CLI option > environment variable > config file > default. Keys: `claude_bin`, `repos`, `me`, `lang`, `translate`, `tr_model`, `ask_model`, `batch`, `retries` (each also has a `GITGRAPH_*` variable, listed by `gg config`).

`claude_bin` lets a Claude-compatible variant do the translation / summary / question calls, e.g. `gg config claude_bin cla`. It receives the same arguments as `claude` (`-p --no-session-persistence --output-format json <prompt>`) **except `--model`**: a variant keeps its own default model; `tr_model` / `ask_model` only apply to the real `claude`.

## Usage

```
gg                        # overview of everything open, as a tree
gg -l log                 # git log --graph style timeline
gg 768 --hops 1           # neighbourhood of #768 (also #768, owner/repo#768)
gg @someone               # around a person
gg show 748               # one node in detail: every edge, comments, body
gg ask 4563 "why does it mention #3859?"   # one-shot question to claude, with the item as context
gg tui [768]              # interactive TUI; a number starts on that tree
gg update                 # update this installation
```

Options: `-r owner/name` (repeatable, the first one is primary), `-u LOGIN` (view as that person; only the perspective changes, not the gh login), `--state open|all`, `--comments linked|all|none` (linked = only comments that reference `#N`/`@someone`, default for graph/show; all = default for tui), `--no-people`, `--no-closed-neighbors`, `--max-age MIN` (cache TTL, default 15), `--refresh`, `-w N` (title width), `-t zh|all|none` (translation, default zh), `-S` (comment summaries), `--color auto|always|never`.

## Line format

```
2026-08-13 #750 [PR] title  @author        # date = when the issue/PR was opened; [PR] blue, [I] green
  +5d o @author » summary                  # comment: +N days after opening; » = one-line summary
```

`[draft]` / `[merged]` / `[closed]` appear only when an item is not simply open. Every `@login` gets a colour of its own (256-colour palette, assigned in order of first appearance).

## Edges

| edge | source |
|---|---|
| `→ refs` / `← cited-by` | `#N`, `owner/repo#N` or GitHub URLs in bodies and comments, plus GitHub's CROSS_REFERENCED timeline events (references coming from closed items). `→` = this item references that one, `←` = that one references this. |
| `→ closes` / `← closed-by` | the PR's `closingIssuesReferences` |
| `→ mentions` / `← mentioned-by` | `@login` (2+ chars, not all digits) |
| `o` | issue comments, PR review bodies, inline review comments |

`#N` inside code fences and in pasted kernel logs / stack traces (`[ 123.4]`, `Tainted:`, `PID:`, `PLATFORM --` …) is ignored. Small numbers (≤ 20) count only after a reference word such as `PR`, `issue`, `see`, `fixes` (so `overwrite #5` is not a reference).

## Translation

Titles and comment first lines containing Chinese characters (`-t zh`, default) are translated into `GITGRAPH_LANG` (default Korean) with `claude -p --model haiku`; `-t all` also translates English. Results are cached in `~/.cache/gitgraph/translations.json`, so each string is asked once. `show` also prints the original title. Only the Claude Code login is needed, no API key.

## Comment summaries

With `-S` (CLI) or by default in the TUI, each comment line shows a one-line summary (`» …`) made by haiku instead of its first line, cached by the body's sha1 in `~/.cache/gitgraph/summaries.json` (≤ 4,000 chars per body, ≤ 40 comments / 40,000 chars per call). While a summary is being generated the line reads `» summarizing…` (`» 요약 중…` in Korean); on failure the first line is shown instead.

## Questions (`a`, `gg ask`)

The item's body and all its comments (6,000 chars per comment, 60,000 total) are sent with your question to `claude -p --model $GITGRAPH_ASK_MODEL` (default sonnet); the answer comes back in `GITGRAPH_LANG`. One-shot: no cache, no follow-up.

## TUI

Starts on a **home** screen of sections (Enter/Space on a header folds it, `-`/`+` all). "Me" = your gh accounts (`GITGRAPH_ME` or `-u` override).

| section | content |
|---|---|
| my turn | items I am in (author, commenter or mentioned) where someone else spoke last → `← @who +Nd » summary` |
| mentioning @me | items that mention me, newest mention first |
| opened in the last N days | `--days N` (default 7) |
| active in the last N days | updated in that window but not newly opened |
| waiting on others | I spoke last (or I opened it and nobody commented) |
| opened by me / open PRs by others / stale (30 days) / all open | as named |

Enter opens a tree around the item; Tab switches to the full overview (`--no-home` starts there). Trees start folded to `--depth N` (default 1); nodes carry `▾` open / `▸` folded (`[+N]` hidden lines) / `·` leaf. Translation and summaries run in the background for the rows on screen first (`GITGRAPH_BATCH`, default 10, per call), then the rest; folded nodes are processed when unfolded. The legend sits in the top-right box (`L` hides it). Progress and token usage are shown in the status/title bars. Open items only.

| key | action |
|---|---|
| `↑`/`k` `↓`/`j` PgUp PgDn `g`/`G` | move (in home, PgDn/PgUp jump between sections) |
| Space, `←`/`→` · `1`~`9` · `-`/`+` | fold/unfold · unfold to that depth · depth 1 / all |
| Enter | node: tree around it (re-root) · `⇢`/`mentions` line: jump to the linked node |
| Tab | home ↔ overview |
| Backspace / `b` / `h` / Esc · `f` | back (restores view, root, folds, cursor and perspective; with the preview or answer panel focused: just leave it) · forward |
| preview pane | full text of the row under the cursor at the bottom: `v` hide/show, `J`/`K` scroll, `w` focus (pane grows; ↑↓ PgUp PgDn g G scroll, `w`/Esc back), `{`/`}` height |
| `a` · `A` | ask claude about the row (answer in a panel on the right half; `w` cycles focus list → preview → panel) · hide/show the panel |
| `d` · `o` | details pager · open in the browser |
| `u` | view home as another person (the previous one goes on the back stack) |
| `l` `c` `p` `t` `s` `H` `r` | tree/log · comments mode · people nodes · translation · summaries · hops 1/2/3 · refetch |
| `/` `n` `N` · `<` `>` · `L` · `$` · `?` · `q` | search · horizontal scroll · legend · token usage · help · quit |
| mouse | click = cursor; click `▾/▸` = fold; click a section header = fold; double-click = Enter (on a `@login`: view as that person); right click = open in browser; back/forward buttons = back/forward; click preview/answer panel = focus; wheel = scroll that area without moving the cursor |

## Token usage

Usage reported by `claude -p --output-format json` is accumulated from process start: the TUI title bar shows `tokens 31.2k in / 1.4k out · $0.052 · 7 calls`, `$` shows a per-phase table (translate / summarize / ask); the CLI prints one line on stderr at exit when any call was made. Cache hits cost nothing.

## GitHub Enterprise

Repos on a GitHub Enterprise host are written `host/owner/name` (e.g. `-r ghe.example.com/team/proj`, or found automatically from a remote such as `git@ghe.example.com:team/proj.git`). `gh auth login -h ghe.example.com` must have been done; API calls use `gh api --hostname`, and the accounts/tokens gh holds for that host. References in bodies (`#N`, `owner/name#N`, full URLs) resolve to the item's own host. Not yet tested against a real Enterprise instance — please report what breaks.

## Accounts, cache

If a private repo answers `NOT_FOUND`, the other accounts registered in `gh auth status` are tried automatically (no global account switch). Cache: `~/.cache/gitgraph/` (items per repo, translations, summaries; `--max-age` minutes, `--refresh` to force). TUI logs go to `~/.cache/gitgraph/tui.log`.
