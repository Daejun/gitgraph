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
gg                        # the TUI (default)
gg 768                    # the TUI starting on #768 (also owner/repo#768, @someone)
gg tutorial               # the TUI with a guided tour of the screen (F2 inside)
gg graph                  # text graph: overview of everything open, as a tree
gg graph -l log           # git log --graph style timeline
gg graph 768 --hops 1     # text graph around #768
gg show 748               # one node in detail: every edge, comments, body
gg review 779             # review a PR: its diff from a worktree of your own checkout (v in the TUI)
gg ask 4563 "why does it mention #3859?"   # one-shot question to claude, with the item as context
gg ai [NAME]              # list / pick the AI CLI: claude, codex, gemini, grok, … (installed ones are shown)
gg config [KEY [VALUE]]   # persistent settings
gg todo                   # print the markdown of everything marked with m in the tui
gg todo done|remove 750   # tick off / delete a mark (also: clear-done); Claude does the same through gg_todo_done
gg mcp                    # MCP server for Claude Code in another window (see the C key)
gg cache [clear …]        # what is stored locally and how to remove it
gg check [-r owner/name]  # diagnose: gh accounts for the host, access per account, open counts, GraphQL fields
gg update                 # update this installation
```

Options: `-r owner/name` · `-u LOGIN` (view as that person in the TUI home; only the perspective changes, not the gh login) · `--state open|all` (all fetches every issue/PR — slow) · `--comments linked|all|none` (linked = only comments that reference `#N`/`@someone`, default for graph/show; all = default for tui) · `--no-people` · `--no-closed-neighbors` · `--max-age MIN` (cache TTL, default 15) · `--refresh` · `-w N` (title width) · `-t zh|all|none` (translation, default zh) · `-S` (comment summaries) · `--color auto|always|never` · `--theme dark|light|basic` · TUI only: `--days N` (home window, 7), `--no-summary`.

## Settings (`gg config`)

`gg config` shows every setting and where it comes from; `gg config KEY VALUE` stores it in `~/.config/gitgraph/config.json`; `gg config unset KEY` removes it. Precedence: CLI option > `GITGRAPH_*` environment variable > config file > default.

| key | env | default | meaning |
|---|---|---|---|
| `claude_bin` | `GITGRAPH_CLAUDE` | `claude` | the AI CLI for translation / summaries / questions, chosen with `gg ai`: `claude` (`-p --output-format json --model …`), `codex` (`codex exec … -o FILE`), `gemini` / `grok` / anything else (`-p PROMPT`, its own default model). Only claude reports token usage. When the chosen CLI fails (not logged in, expired token, missing) the TUI pops up an offer to switch to an installed alternative, keep trying, or turn AI features off for the session |
| `repos` | `GITGRAPH_REPOS` | | default repos, comma separated |
| `me` | `GITGRAPH_ME` | gh accounts | logins that count as "me" in the TUI home |
| `lang` | `GITGRAPH_LANG` | `Korean` | language of translations, summaries and answers |
| `translate` | `GITGRAPH_TRANSLATE` | `zh` | `zh` (Chinese only) / `all` / `none` |
| `tr_model` · `ask_model` | `GITGRAPH_TR_MODEL` · `GITGRAPH_ASK_MODEL` | `haiku` · `sonnet` | models (real `claude` only) |
| `batch` | `GITGRAPH_BATCH` | `10` | TUI: nodes per translate/summary call (split in halves across jobs) |
| `ai_parallel` | `GITGRAPH_AI_PARALLEL` | `3` | TUI: how many AI CLI calls run at the same time (summaries, title translations, link reasons; long bodies are translated in parallel chunks) |
| `retries` | `GITGRAPH_RETRIES` | `3` | `gh api` retries on transient network errors |
| `fetch_parallel` | `GITGRAPH_FETCH_PARALLEL` | `8` | how many `gh` queries run at the same time while filling the cache (first run, refresh). A round trip to GitHub costs ~0.4s whatever it asks for, so this is what makes a cold start fast; on a repo with hundreds of open items raising it to 12 is measurably quicker still |
| `side_width` · `expand_focused` · `expanded_weight` · `screen_mode` · `border` | `GITGRAPH_SIDE_WIDTH` … | `0.4` · `true` · `2` · `normal` · `rounded` | TUI layout (see below) |
| `review_model` · `review_timeout` · `review_max_bytes` | `GITGRAPH_REVIEW_MODEL` · `GITGRAPH_REVIEW_TIMEOUT` · `GITGRAPH_REVIEW_MAX_BYTES` | `sonnet` · `900` · `400000` | review mode: the model the review runs on (claude only), how long one call may take, and the diff size beyond which it is split by file and run in parallel |
| `review_files_width` · `review_findings_width` | `GITGRAPH_REVIEW_FILES_WIDTH` · `GITGRAPH_REVIEW_FINDINGS_WIDTH` | `0.22` · `0.30` | review mode: width of the Files and Findings columns |
| `worktree_keep_days` · `worktree_max` | `GITGRAPH_WORKTREE_KEEP_DAYS` · `GITGRAPH_WORKTREE_MAX` | `7` · `5` | review mode: how long a PR worktree is kept, and how many. A kernel checkout is well over a gigabyte, so these are not a formality — `gg cache` lists them with their real size |
| `review_subjective` | `GITGRAPH_REVIEW_SUBJECTIVE` | `auto` | review mode: style/design remarks — `auto` hides them while a confirmed defect stands, `always`, `never` |
| `todo_file` | `GITGRAPH_TODO` | `~/gitgraph-todo.md` | markdown written from the marks made with `m` |
| `theme` | `GITGRAPH_THEME` | `dark` | colour theme, like vim's `bg=`: `dark` (256 colours), `light` (darker tones for a light background), `basic` (8 colours, no dim, no dark blue — PuTTY and other plain terminals). `--theme` for one run, `T` in the TUI to cycle |

## Line format

```
2026-08-13 #750 [PR] title  @author        # date = when the issue/PR was opened; [PR] blue, [I] green
  +5d 08-18 o @author » summary            # comment: +N days after opening, and its date; » = one-line summary
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
- **Questions** (`a`, `gg ask`) — every answer is saved with its question, anchored to the issue/PR/comment (`~/.config/gitgraph/qa.json`), and the answer tab shows the earlier Q&A of the selection again in later sessions (the Item panel counts them: `2 Q&A`). The question goes to `ask_model` with the item (or the comment under the cursor, marked) plus its metadata, the whole comment thread in order and the linked issues/PRs with the sentence that made each link (up to 90,000 chars); the answer names the comment or #number it relies on. One-shot, no cache.
- Only the Claude Code login is needed, no API key. Usage reported by `--output-format json` is accumulated from process start: the TUI title bar shows `tokens 31.2k in / 1.4k out · $0.052 · 7 calls`, `$` shows a per-phase table; the CLI prints one line on stderr at exit. Most of "in" is cached system prompt, which is cheap.

## TUI

lazygit-style layout: a side column of panels (Repo · Item · Inbox · Comments · Links · People) and a main panel that shows whatever is selected.

```
╭─1 Repo──────────────────────╮╭─0 Main [content] answer───────────────────────────────╮
│ owner/name  57 open  15:44  ││ #750 [PR] mtfs: running out of space must not …       │
╰─────────────────────────────╯│ @Daejun7Park  2026-08-13  updated 2026-08-19          │
╭─2 Item──────────────────────╮│ https://github.com/owner/name/pull/750                │
│ 2026-08-13 #750 [PR] mtfs: …││                                                       │
│   » one-line summary        ││                                                       │
╰─────────────────────────────╯│                                                       │
╭─3 Inbox ‹ my turn 2 › 1/10──╮│ Running the device out of space under concurrent …   │
│ 2026-08-17 #763 [I] xfstest…││                                                       │
╭─4 Comments──────────────────╮│                                                       │
│ +0d o @Daejun7Park » …      ││                                                       │
╭─5 Links─────────────────────╮│                                                       │
│ → refs 2026-08-13 #748 [I] …││                                                       │
│   ↳ second half of #748: …  ││                                                       │
╭─6 People────────────────────╮│                                                       │
│ @Daejun7Park  author        ││                                                       │
╰─────────────────────────────╯╰───────────────────────────────────────────────────────╯
 ⏎ open item  [ ] section  / search  a ask  o browser   1-5 0 Tab panels  + _ screen  b f back/fwd  ? keys  q quit
```

| panel | shows | Enter |
|---|---|---|
| 1 Repo | repo, open count, fetch time, "me", toggles, token usage | – |
| 2 Item | the current item: title, metadata, `» one-line summary` (or the first line of its body), comment/link counts, URL | read it in main |
| 3 Inbox | one section at a time (`[` `]`): todo · my turn · mentions · opened · active · waiting · mine · PRs by others · stale · all — same rules as before ("my turn" = items I am in where someone else spoke last, `--days N` window, "me" = gh accounts / `-u` / `u`). In my turn / active / waiting / PRs rows the cursor previews the **latest comment** (that is why main shows a comment there); my turn rows end with why it is on me: `⟵ @who mentioned +5d`, `@who on my PR +14d`, `@who replied +2d` | make it the **current item** — Item, Links, Comments and People follow |
| 5 Links | every edge of the current item and its comments: `→ refs`, `← cited-by`, `→ closes`, `← closed-by` (via which comment). Under each link a `↳` note (up to two lines) says **why**: a one-line reason written by the summarizer from the sentence that made the reference (e.g. `충돌 여부를 확인한 관련 PR`; a short quote around `#N` until it arrives, or when summaries are off), or — when there is no such text (references recorded only by GitHub's timeline, closed items) — a one-line summary of that issue/PR (`» …`, made by the same summarizer as comments once its body has been fetched; `» summarizing…` while pending) | go to that item; on a comment row: its item, with the cursor on that comment (back with `b`/Esc) |
| 4 Comments | the current item's comments `+Nd o @who » summary`, newest on top | read it in main |
| 6 People | author, commenters and mentioned people of the current item, most recently active first | view Inbox as that person |
| 0 Main | tabs (`[` `]`): **content** = full text of the row under the cursor in the focused side panel (URL first, underlined; body + metadata, or a comment) — it stays put while main itself is focused; **answer** = the last `a` question. Both render markdown: headings, code blocks and `code`, **bold**, links, quotes, bullets, tables (aligned box tables, scroll sideways with `H`/`L`) | – |

Layout: side column `side_width` (0.4) of the screen; titles are truncated to what fits and re-fitted whenever the widths change (drag, resize, screen mode); the focused side panel is taller (`expand_focused`, `expanded_weight`) and stays that way while main is focused, so the next pick is easy; `+`/`_` cycle screen modes normal → half (the focused panel fills its column) → full (only that panel); narrow terminals (≤ 84 columns) stack the focused side panel above the main panel; borders `border` (rounded · single · double · bold · hidden). Translation and summaries run in the background for the visible rows first (`batch` per call); `» summarizing…` marks a pending one. The first time a repo is fetched the items arrive in batches and the screen is drawn as soon as the first one lands — the Repo panel says `⋯ still loading` until the rest is in, and you can already move around.

| key | action |
|---|---|
| `1`-`6` · `0` · Tab / Shift-Tab | jump to a panel · main · cycle |
| `[` `]` | previous / next tab of the focused panel |
| `+` `_` | screen mode normal → half → full |
| `↑`/`k` `↓`/`j` · PgUp/PgDn `,` `.` · `g`/`G` `<`/`>` · `H`/`L` | move · page · top/bottom · scroll sideways |
| `K` `J` | scroll the main panel from anywhere |
| Enter | see the table above |
| `i` | translate the main content in full, **on demand** — `i` again shows the original. Also the `[i 번역]` button in Main's title bar. Nothing is translated in the background: the body stays as written until you ask (`⟳ 번역 중…` while it runs; cached in `translations_full.json`) |
| `m` | mark the selected issue/PR or comment for my next work and write a note; marked rows show `✎`, Inbox gets a **todo** section (second tab, after my turn), and the markdown file `todo_file` (default `~/gitgraph-todo.md`; `gg todo` prints it) is rewritten so the next session — or Claude — can pick the work up. `m` again on a marked row: edit the note / mark done / remove. On the answer tab `m` saves the answer text into the mark's note. Source of truth: `~/.config/gitgraph/todo.json` |
| `Del` | remove the mark on the selection outright (`m` on a marked row offers edit / done / remove instead) |
| `y` | copy the URL of the selection to the clipboard |
| `a` · `d` · `o` | ask claude about the selection (answer tab) · details pager · open in the browser (the URL is also the first, underlined line of the content) |
| Esc / `b` · `f` | back (previous item and perspective) · forward |
| `u` | view Inbox as another person |
| `r` · `R` | refresh from GitHub in the background: only what changed (also on start-up, and automatically every `--max-age` minutes) · everything |
| `c` `t` `s` `p` `h` | comments mode · translation · summaries · people nodes · hops 1/2/3 |
| `/` `n` `N` · `T` · `$` · `q` | search in the focused panel · colour theme · token usage · quit |
| `C` | open Claude Code next to gg (a tmux pane, else full screen). Through the `gg mcp` server it sees what you look at (`gg_state`, `gg_context`), your marks (`gg_todo`) and can drive gg (`gg_open`, `gg_mark`) and tick off marks it has handled (`gg_todo_done`). Register once: `claude mcp add -s user gg -- gg mcp` |
| `?` · `O` · F1 · F2 | key menu for the focused panel (Enter runs the action) · options menu (comments / translation / summaries / people / hops / theme / screen) · full help text · guided tour (offered once on the first run; also `gg tutorial`; ⏎/→ next, ←/p prev, Esc stop) |
| Hangul IME | shortcuts work while the keyboard is in Hangul mode: the jamo/syllable is mapped back to the 2-set layout keys (`ㅓ` = j, `ㅏ` = k, `자` = w k) |
| mouse | drag inside Main = select text, copied to the clipboard on release (OSC 52 plus `wl-copy`/`xclip`/`xsel`/`pbcopy` when installed; tmux needs `set -g set-clipboard on`; Shift+drag keeps the terminal's own selection); click `‹` `›` in the Inbox title or a tab name in Main's title = switch tab; first click on another panel = focus it (its cursor stays); a click inside the focused panel = select that row; click on the URL text = open it in the browser; double-click = Enter; wheel = scroll that panel without moving the cursor; back/forward buttons; drag the border between the side column and main to resize (`gg config side_width` keeps it) |

Prompts (`a`, `/`, `u`), menus (`?`, `O`), confirmations (`r`) and text (`d`, `$`, F1) open as centred popups; Esc closes them.

Under tmux enable mouse reporting (`set -g mouse on`).

Tests: `python3 tests/run.py` runs everything — a syntax check, the stdlib-`unittest` suites, the golden renderings and `tests/tui_smoke.py`, which drives the TUI in a pseudo-terminal and renders it with `tests/vt.py`. It all runs against a fixture repo in a throwaway `HOME`, so no `gh` login, no network and no AI CLI are needed, and your own cache is never touched. One suite: `python3 tests/run.py unit`; one test: `python3 -m unittest tests.test_parse.TestRefs.test_fence`.

## Review mode (`v`, `gg review`)

`v` on a pull request replaces the whole screen with three panels — Files, Diff, Findings — and `v` (or Esc) goes back to the graph. `gg review 779` starts there directly.

The code comes from **your own checkout, not the API**: gg fetches `refs/pull/N/head` and the base branch into `refs/gg/<owner>__<name>/pr-N` in the clone it discovered, checks the head out as a detached `git worktree` under `~/.cache/gitgraph/worktrees/<repo>/pr-N`, and lets `git diff <merge-base> …` produce the patch. Nothing is truncated the way `pulls/N/files` truncates a large PR, and a review can open the whole function a hunk sits in — not just the hunk. Without a local clone of the repo, review mode says so and stops; it never guesses.

```
╭─1 Files────────────────────╮╭─2 Diff  extent.c ──────────────────────╮╭─3 Findings [open] posted─╮
│#779 mtfs: update owner ext…││▾ @@ -220,6 +220,7 @@ mtfs_drop_extent  ││● bug 1                   │
│open · @someone · 0113d1f   ││    220       if (!page)                ││ #1 ✓ lock leak on the ou…│
│2 files +163 -166           ││    221 -         return -ENOMEM;       ││   extent.c:221           │
│                            ││⚠   221 +         goto out_unlock;      ││● logic 1                 │
│▸ extent.c    +163 -156 ⚠   ││    222 +                               ││ #2 ? renaming segno wid… │
│  mtfs.h        +0 -10      ││    223       spin_lock(&sbi->lock);    ││   gc.c:88                │
│                            ││    224   out_unlock:                   ││                          │
│worktree 789.7K             ││    225       spin_unlock(&sbi->lock);  ││                          │
╰────────────────────────────╯╰────────────────────────────────────────╯╰──────────────────────────╯
 ⏎ show this file's diff  r reload  R refetch  o browser  v back to the graph
```

| panel | shows | Enter |
|---|---|---|
| 1 Files | the PR, then one row per changed file: `+added -deleted` and the worst finding on it (`⚠` defect, `ℹ` remark). The last line is what the worktree costs on disk | show that file's diff in Diff |
| 2 Diff | the file's unified diff, five lines of context, hunks foldable. The gutter is the line number in the new file (the old one for removed lines) and a marker when a finding sits there. Tabs expand to four columns | fold / unfold the hunk |
| 3 Findings | tabs (`[` `]`): **open** · **posted** · **ignored** · **dropped** (disproved) · **changes** (how the review split the diff up, and whether the changed code is reachable at all) · **github** (the review threads already on the PR, so you do not repeat a human). Each finding shows its verdict — `✓` confirmed, `?` plausible — its file and line, and `⚠` when gg had to pull it onto the nearest changed line (GitHub refuses a comment anywhere else) | jump the Diff panel to that line |

The screen adapts: three columns while the diff can keep 56 of them, otherwise Findings folds into a strip under the diff, and at ≤ 84 columns the three stack. `+`/`_` and the panel keys work as everywhere else.

| key | action |
|---|---|
| `v` · Esc | into review mode on the PR under the cursor · back to the graph |
| `1` `2` `3` · Tab | Files · Diff · Findings · cycle |
| Enter | see the table above |
| `x` | ignore this finding, or take the ignore back (remembered per PR, across new commits) |
| `R` · `r` | run the review (it asks first) · reload the PR and its diff, keeping the cached findings |
| `o` · `y` | open the PR — or the exact file and line under the cursor — in the browser · copy that URL |
| `/` `n` `N` · `?` | search the focused panel · key menu for it |

```
gg review 779             # the TUI in review mode on PR #779
gg review 779 --print     # run the review and print it (--no-ai: the diff only, no AI call)
gg review 779 --json      # the same as JSON (files, hunks are re-read from the worktree)
gg review 779 --refresh   # ignore what is cached for this head and read it again
```

`R` runs the review itself. gg carries the protocol rather than a prompt, distilled from the kernel review prompt sets: read the whole function a hunk sits in before judging it (that is what the worktree is for), split the diff into CHANGE-N categories — one loop, one lock, one allocation, one changed return value — and analyse them one at a time, gate first on whether the changed code can run at all for the use the description claims, assume the author is wrong and demand proof, and drop any finding you cannot point at with a concrete `file:line -> file:line` path. The reply has to come back in fixed fields, and gg pins every finding onto a line the diff really touches before storing it — GitHub refuses an inline comment anywhere else. A diff over `review_max_bytes` is split by file and reviewed in parallel.

It costs real money and minutes, so it is never started on its own: `R` asks first (with the file and call count), the result is cached against the head SHA, and `r` only reloads the PR. Two measured runs on a private kernel-style repo, claude sonnet, one call each: 2 files / +163 -166 took 7m14s and reported nothing, 4 files / +466 -155 took 6m39s and reported one real inconsistency between two sibling functions the same PR added, with a four-hop evidence chain and an applicable diff.

Everything else in review mode works without an AI CLI at all.

While review mode is open, `gg mcp` reports it: `gg_state` names the PR, its files, the finding counts and the finding under the cursor, and `gg_context` takes `finding:<fid>` for one finding in full or `file:<path>` for the reviewed file straight out of the worktree.

## Local data

Everything gg keeps is under `~/.cache/gitgraph/` (mode 0700, files 0600 — it contains the bodies and comments of private repos) plus two small files under `~/.config/gitgraph/`:

| file | what | lifetime |
|---|---|---|
| `items__<repo>__open.json` | issues/PRs of one repo with bodies and comments, as fetched | after `--max-age` minutes (15) gg asks GitHub only for the numbers + updatedAt of the open items and re-fetches just what changed (closed ones are dropped); `--refresh` fetches everything; deleted at start-up when unused for 30 days |
| `stubs__<repo>.json` | titles/bodies of referenced items (closed, other repos) | kept a day |
| `translations.json`, `translations_full.json`, `summaries.json`, `whys.json` | AI results keyed by text hash | capped (oldest dropped beyond 20k entries) |
| `tui.log` | tui stderr / progress | cut back beyond 1 MB |
| `accounts.json` | which gh account can see which repo | overwritten when it changes |
| `reviews__<repo>.json` | review findings per PR of one repo, and what was posted / ignored / disproved | kept; the per-digest history survives new commits so nothing is re-offered |
| `worktrees/<repo>/pr-N/` | the PR head checked out for review (a real `git worktree` of your clone) | dropped after `worktree_keep_days`, and beyond `worktree_max` oldest first; `gg cache clear review` removes them properly (`git worktree remove` plus the `refs/gg/…` refs) |
| `state.json`, `cmd*.json` | what the tui shows (for `gg mcp`) | overwritten |
| `~/.config/gitgraph/config.json`, `todo.json` (+ the `todo_file` markdown) | settings, your marks | yours |

`gg cache` lists all of it with sizes and ages; `gg cache clear all|items|ai|logs|review|owner/name` removes it (everything is re-fetched or re-generated on demand).

## GitHub Enterprise

Repos on an Enterprise host are written `host/owner/name` (`-r ghe.example.com/team/proj`, or found from a remote such as `git@ghe.example.com:team/proj.git`). API calls use `gh api --hostname` with the accounts/tokens gh holds for that host; references in bodies resolve to the item's own host. Not yet verified against a real Enterprise instance — please report what breaks.

## Accounts, network, cache

- If a private repo answers `NOT_FOUND`, the other accounts registered in `gh auth status` for that host are tried automatically (no global account switch); the error names the host and the accounts tried. The account that worked is remembered per repo in `accounts.json`, and before that gg takes the account named in the git config of that repo's checkout — the one you are in, or one it finds a couple of levels below, the same scan repo discovery does (`credential.<host>.username`, the helper `gh auth setup-git -u` writes, or a `user@` in the remote URL) — so the very first query goes to the right account instead of spending a round trip discovering it.
- Transient network errors (`TLS handshake timeout`, connection reset, 5xx) are retried `retries` times with backoff; then `gg` prints what to check (`gh api user`, `HTTPS_PROXY`, VPN/DNS).
- Cache: `~/.cache/gitgraph/` — items per repo and state (`--max-age`, `--refresh`), translations, summaries. TUI logs (and stderr) go to `~/.cache/gitgraph/tui.log`.

## License

MIT
