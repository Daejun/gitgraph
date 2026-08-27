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

Run `gg` inside a git repo, or in a directory that contains repos (2 levels deep): the GitHub repo is read from `origin`; if several are found you are asked which (`a` = all, the first is *primary* and its items are shown as bare `#N`). Otherwise pass `-r owner/name` (repeatable) or store a default: `gg config repos owner/name`. GitHub Enterprise repos are written `host/owner/name`.

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
gg update                 # update this installation
```

Options: `-r owner/name` · `-u LOGIN` (view as that person in the TUI home; only the perspective changes, not the gh login) · `--state open|all` (all fetches every issue/PR — slow) · `--comments linked|all|none` (linked = only comments that reference `#N`/`@someone`, default for graph/show; all = default for tui) · `--no-people` · `--no-closed-neighbors` · `--max-age MIN` (cache TTL, default 15) · `--refresh` · `-w N` (title width) · `-t zh|all|none` (translation, default zh) · `-S` (comment summaries) · `--color auto|always|never` · TUI only: `--depth N` (initial fold depth, 1), `--days N` (home window, 7), `--no-home`, `--no-summary`.

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
- **Summaries** (`-S`, default in the TUI) — each comment line shows a one-line summary (`» …`) instead of its first line, cached by the body's sha1 in `summaries.json` (≤ 4,000 chars per body, ≤ 40 comments / 40,000 chars per call). While one is being generated the line reads `» summarizing…` (`» 요약 중…` in Korean); on failure the first line is shown.
- **Questions** (`a`, `gg ask`) — the item's body and all its comments (6,000 chars per comment, 60,000 total) go with your question to `ask_model`; one-shot, no cache.
- Only the Claude Code login is needed, no API key. Usage reported by `--output-format json` is accumulated from process start: the TUI title bar shows `tokens 31.2k in / 1.4k out · $0.052 · 7 calls`, `$` shows a per-phase table; the CLI prints one line on stderr at exit. Most of "in" is cached system prompt, which is cheap.

## TUI

Starts on a **home** screen of sections (Enter/Space on a header folds it, `-`/`+` all, PgDn/PgUp jump between sections). "Me" = your gh accounts, or `me` / `-u`.

| section | content |
|---|---|
| my turn | items I am in (author, commenter or mentioned) where someone else spoke last → `← @who +Nd » summary` |
| mentioning @me | items that mention me, newest mention first |
| opened in the last N days | `--days N` (default 7) |
| active in the last N days | updated in that window but not newly opened |
| waiting on others | I spoke last (or I opened it and nobody commented) |
| opened by me / open PRs by others / stale (30 days) / all open | as named |

Enter opens a tree around the item; Tab switches to the full overview (`--no-home` starts there). Trees start folded to `--depth N` (default 1); nodes carry `▾` open / `▸` folded (`[+N]` hidden lines) / `·` leaf. Translation and summaries run in the background for the rows on screen first (`batch` per call), then the rest; folded nodes are processed when unfolded. The preview pane at the bottom shows the full text of the row under the cursor; a question's answer appears in a panel on the right half. The legend sits in the top-right box (`L` hides it). Open items only.

| key | action |
|---|---|
| `↑`/`k` `↓`/`j` PgUp PgDn `g`/`G` | move (home: PgDn/PgUp = next/previous section) |
| Space, `←`/`→` · `1`~`9` · `-`/`+` | fold/unfold · unfold to that depth · depth 1 / all |
| Enter | node: tree around it (re-root) · `⇢`/`mentions` line: jump to the linked node |
| Tab | home ↔ overview |
| Backspace / `b` / `h` / Esc · `f` | back (restores view, root, folds, cursor and perspective; with the preview or answer panel focused: just leave it) · forward |
| `v` · `J`/`K` · `w` · `{`/`}` | preview pane: hide/show · scroll · focus (pane grows; ↑↓ PgUp PgDn g G scroll, `w`/Esc back; `w` cycles list → preview → answer panel) · height |
| `a` · `A` | ask claude about the row · hide/show the answer panel |
| `d` · `o` | details pager · open in the browser |
| `u` | view home as another person (the previous one goes on the back stack) |
| `l` `c` `p` `t` `s` `H` `r` | tree/log · comments mode · people nodes · translation · summaries · hops 1/2/3 · refetch |
| `/` `n` `N` · `<` `>` · `L` · `$` · `?` · `q` | search · horizontal scroll · legend · token usage · help · quit |
| mouse | click = cursor; click `▾/▸` = fold; click a section header = fold; double-click = Enter (on a `@login`: view as that person); right click = open in browser; back/forward buttons = back/forward; click preview/answer panel = focus; wheel = scroll that area without moving the cursor |

Under tmux enable mouse reporting (`set -g mouse on`).

## GitHub Enterprise

Repos on an Enterprise host are written `host/owner/name` (`-r ghe.example.com/team/proj`, or found from a remote such as `git@ghe.example.com:team/proj.git`). API calls use `gh api --hostname` with the accounts/tokens gh holds for that host; references in bodies resolve to the item's own host. Not yet verified against a real Enterprise instance — please report what breaks.

## Accounts, network, cache

- If a private repo answers `NOT_FOUND`, the other accounts registered in `gh auth status` for that host are tried automatically (no global account switch); the error names the host and the accounts tried.
- Transient network errors (`TLS handshake timeout`, connection reset, 5xx) are retried `retries` times with backoff; then `gg` prints what to check (`gh api user`, `HTTPS_PROXY`, VPN/DNS).
- Cache: `~/.cache/gitgraph/` — items per repo and state (`--max-age`, `--refresh`), translations, summaries. TUI logs (and stderr) go to `~/.cache/gitgraph/tui.log`.

## License

MIT
