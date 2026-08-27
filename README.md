# gg (gitgraph)

**EN** — Draws how GitHub issues, PRs, comments and `@mentions` link to each other as an ASCII graph, on the command line or in a curses TUI. No Python package dependencies.

**KO** — GitHub issue · PR · 코멘트 · `@mention`이 서로 어떻게 연결돼 있는지 ASCII 그래프로 그린다. 터미널 출력과 curses TUI 두 가지. python 패키지 의존성 없음.

## Install / 설치

**EN** — Needs python3 ≥ 3.9 and the `gh` CLI (`gh auth login` done). The `claude` CLI is optional: it is used only for translation, comment summaries and questions; without it those features are silently off and the graph still works.

**KO** — python3 ≥ 3.9, `gh` CLI(`gh auth login` 완료)가 필요하다. `claude` CLI는 선택: 번역 · 코멘트 요약 · 질문에만 쓰이고, 없으면 그 기능만 조용히 꺼진다.

```
pipx install git+https://github.com/Daejun/gitgraph        # recommended / 권장 (no pipx: pip install --user git+…)
# single file / 파일 하나만:
curl -fsSL https://raw.githubusercontent.com/Daejun/gitgraph/main/gitgraph.py -o ~/.local/bin/gg && chmod +x ~/.local/bin/gg
gg update                                                    # later: refresh from GitHub / 나중에 GitHub에서 갱신
```

**EN** — Run it inside a git repo (or a directory that contains repos): the GitHub repo is taken from `origin`; if several are found you are asked which. Or pass `-r owner/name`. For a fixed repo put `export GITGRAPH_REPOS=owner/name` in your shell rc.

**KO** — git repo 안(또는 repo들이 모여 있는 상위 dir)에서 실행하면 `origin`에서 repo를 찾고, 여러 개면 번호로 물어본다. 아니면 `-r owner/name`. 늘 같은 repo를 보면 셸 rc에 `export GITGRAPH_REPOS=owner/name`.

Environment / 환경변수: `GITGRAPH_REPOS`, `GITGRAPH_ME`(logins that count as "me" / "나"로 볼 login), `GITGRAPH_LANG`(default Korean), `GITGRAPH_TRANSLATE`(zh|all|none), `GITGRAPH_TR_MODEL`(haiku), `GITGRAPH_ASK_MODEL`(sonnet), `GITGRAPH_BATCH`(10).

## Usage / 사용법

```
gg                        # overview of everything open, as a tree / open 항목 전체 개요 (tree)
gg -l log                 # git log --graph style timeline / git log --graph 식 시간축
gg 768 --hops 1           # neighbourhood of #768 (also #768, owner/repo#768) / #768 주변 1-hop
gg @someone               # around a person / 사람 기준
gg show 748               # one node in detail: every edge, comments, body / 노드 상세
gg ask 4563 "why does it mention #3859?"   # one-shot question to claude with the item as context / 그 항목을 context로 단발 질문
gg tui [768]              # interactive TUI; a number starts on that tree / 대화형 화면, 숫자를 주면 그 tree에서 시작
gg update                 # update this installation / 설치 갱신
```

Options / 옵션: `-r owner/name` (repeatable, first is primary / 반복 가능, 첫 번째가 primary), `-u LOGIN` (view as that person; only the perspective changes, not the gh login / home의 "나"를 그 사람으로, 로그인은 그대로), `--state open|all`, `--comments linked|all|none` (linked = only comments that reference `#N`/`@someone`, default for graph/show; all = default for tui / graph·show 기본 linked, tui 기본 all), `--no-people`, `--no-closed-neighbors`, `--max-age MIN` (cache TTL, default 15 / 캐시 TTL), `--refresh`, `-w N` (title width / 제목 폭), `-t zh|all|none` (translation, default zh / 번역), `-S` (comment summaries / 코멘트 요약), `--color auto|always|never`.

## Line format / 줄 형식

```
2026-08-13 #750 [PR] title  @author        # date = when the issue/PR was opened; [PR] blue, [I] green / 날짜 = 열린 날; [PR] 파랑, [I] 초록
  +5d o @author » summary                  # comment: +N days after opening; » = one-line summary / 코멘트: 열린 날 기준 +N일, » = 한 줄 요약
```

**EN** — `[draft]` / `[merged]` / `[closed]` appear only when an item is not simply open. Every `@login` gets a colour of its own (256-colour palette, assigned in order of first appearance).

**KO** — open이 아닐 때만 `[draft]` / `[merged]` / `[closed]`가 붙는다. `@login`은 사람마다 고유 색(256색, 처음 등장 순서로 배정).

## Edges / edge 종류

| edge | EN | KO |
|---|---|---|
| `→ refs` / `← cited-by` | `#N`, `owner/repo#N` or GitHub URLs in bodies and comments, plus GitHub's CROSS_REFERENCED timeline events (references coming from closed items). `→` = this item references that one, `←` = that one references this. | 본문·코멘트의 `#N`, `owner/repo#N`, GitHub URL + timeline CROSS_REFERENCED 이벤트(닫힌 항목에서 오는 참조). `→` = 이 항목이 저것을 참조, `←` = 저것이 이 항목을 참조. |
| `→ closes` / `← closed-by` | the PR's `closingIssuesReferences` | PR의 `closingIssuesReferences` |
| `→ mentions` / `← mentioned-by` | `@login` (2+ chars, not all digits) | `@login` (2자 이상, 숫자만은 제외) |
| `o` | issue comments, PR review bodies, inline review comments | issue comment, PR review 본문, inline review comment |

**EN** — `#N` inside code fences and in pasted kernel logs / stack traces (`[ 123.4]`, `Tainted:`, `PID:`, `PLATFORM --` …) is ignored. Small numbers (≤ 20) count only after a reference word such as `PR`, `issue`, `see`, `fixes` (so `overwrite #5` is not a reference).

**KO** — code fence 안과 kernel log / stack trace 줄(`[ 123.4]`, `Tainted:`, `PID:`, `PLATFORM --` 등)의 `#N`은 무시한다. `#20` 이하 작은 번호는 앞 단어가 `PR`, `issue`, `see`, `fixes` 같은 참조어일 때만 인정한다(`overwrite #5` 같은 서수 배제).

## Translation / 번역

**EN** — Titles and comment first lines containing Chinese characters (`-t zh`, default) are translated into `GITGRAPH_LANG` (default Korean) with `claude -p --model haiku`; `-t all` also translates English. Results are cached in `~/.cache/gitgraph/translations.json`, so each string is asked once. `show` also prints the original title. Only the Claude Code login is needed, no API key.

**KO** — 제목과 코멘트 첫 줄 중 한자가 섞인 것(`-t zh`, 기본)을 `claude -p --model haiku`로 `GITGRAPH_LANG`(기본 Korean)으로 바꾼다. `-t all`이면 영어도. `~/.cache/gitgraph/translations.json`에 캐시되어 같은 문자열은 다시 묻지 않는다. `show`는 원문 제목도 같이 보여준다. Claude Code 로그인만 있으면 되고 API key는 필요 없다.

## Comment summaries / 코멘트 한 줄 요약

**EN** — With `-S` (CLI) or by default in the TUI, each comment line shows a one-line summary (`» …`) made by haiku instead of its first line, cached by the body's sha1 in `~/.cache/gitgraph/summaries.json` (≤ 4,000 chars per body, ≤ 40 comments / 40,000 chars per call). While a summary is being generated the line reads `» 요약 중…`; on failure the first line is shown instead.

**KO** — `-S`(CLI) 또는 TUI 기본으로, 코멘트 줄이 첫 줄 발췌 대신 haiku가 만든 한 줄 요약(`» …`)이 된다. 본문 sha1 기준으로 `~/.cache/gitgraph/summaries.json`에 캐시(본문 4,000자, 호출당 40건 / 40,000자 상한). 생성 중에는 `» 요약 중…`, 실패하면 첫 줄 발췌로 돌아간다.

## Questions / 질문 (`a`, `gg ask`)

**EN** — The item's body and all its comments (6,000 chars per comment, 60,000 total) are sent with your question to `claude -p --model $GITGRAPH_ASK_MODEL` (default sonnet); the answer comes back in `GITGRAPH_LANG`. One-shot: no cache, no follow-up.

**KO** — 커서 항목의 본문과 코멘트 전체(코멘트당 6,000자, 총 60,000자 상한)를 질문과 함께 `claude -p --model $GITGRAPH_ASK_MODEL`(기본 sonnet)에 보낸다. 답은 `GITGRAPH_LANG`. 단발: 캐시 없음, 이어가기 없음.

## TUI

**EN** — Starts on a **home** screen of sections (Enter/Space on a header folds it, `-`/`+` all). "Me" = your gh accounts (`GITGRAPH_ME` or `-u` override).

**KO** — 시작 화면은 섹션별 **home** 목록(헤더에서 Enter/Space로 접고 펼침, `-`/`+` 전부). "나" = `gh auth status`의 계정들(`GITGRAPH_ME` / `-u`로 지정).

| section / 섹션 | EN | KO |
|---|---|---|
| my turn | items I am in (author, commenter or mentioned) where someone else spoke last → `← @who +Nd » summary` | 내가 관여(작성·코멘트·mention)한 항목 중 마지막 코멘트가 남의 것 |
| mentioning @me | items that mention me, newest mention first | 나를 mention한 항목 (최근 mention 순) |
| opened in the last N days | `--days N` (default 7) | `--days N`(기본 7) 안에 열린 것 |
| active in the last N days | updated in that window but not newly opened | 그 기간에 갱신됐지만 새로 열린 건 아닌 것 |
| waiting on others | I spoke last (or I opened it and nobody commented) | 내가 마지막으로 말한 것(또는 내가 열고 코멘트 없는 것) |
| opened by me / open PRs by others / stale (30 days) / all open | as named | 그대로 |

**EN** — Enter opens a tree around the item; Tab switches to the full overview (`--no-home` starts there). Trees start folded to `--depth N` (default 1); nodes carry `▾` open / `▸` folded (`[+N]` hidden lines) / `·` leaf. Translation and summaries run in the background for the rows on screen first (`GITGRAPH_BATCH`, default 10, per call), then the rest; folded nodes are processed when unfolded. The legend sits in the top-right box (`L` hides it). Progress and token usage are shown in the status/title bars. Open items only.

**KO** — Enter로 그 항목 중심 tree, Tab으로 전체 overview(`--no-home`이면 overview로 시작). tree는 `--depth N`(기본 1)까지만 펼친 상태로 시작하고 노드 앞에 `▾` 펼침 / `▸` 접힘(`[+N]` 숨은 줄 수) / `·` leaf 표시. 번역·요약은 **화면에 보이는 줄부터** `GITGRAPH_BATCH`(기본 10)개씩 백그라운드로 처리하고, 접힌 노드는 펼칠 때 처리. 범례는 오른쪽 위 상자(`L`로 숨김). 진행 상황과 토큰 사용량은 상태줄·제목줄에. open 항목만 대상.

| key / 키 | EN | KO |
|---|---|---|
| `↑`/`k` `↓`/`j` PgUp PgDn `g`/`G` | move (in home, PgDn/PgUp jump between sections) | 커서 이동 (home에서 PgDn/PgUp은 다음/이전 섹션) |
| Space, `←`/`→` · `1`~`9` · `-`/`+` | fold/unfold · unfold to that depth · depth 1 / all | 접기/펼치기 · 그 깊이까지 펼침 · depth 1로 접기 / 전부 펼침 |
| Enter | node: tree around it (re-root) · `⇢`/`mentions` line: jump to the linked node | 노드: 그 노드로 재루팅 · `⇢`/`mentions` 줄: 가리키는 노드로 점프 |
| Tab | home ↔ overview | home ↔ 전체 overview |
| Backspace / `b` / `h` / Esc · `f` | back (restores view, root, folds, cursor and perspective; with the preview or answer panel focused: just leave it) · forward | 뒤로(화면·root·접힘·커서·관점 복원; preview·답 panel 포커스 중이면 포커스 해제만) · 앞으로 |
| preview pane | full text of the row under the cursor at the bottom: `v` hide/show, `J`/`K` scroll, `w` focus (pane grows; ↑↓ PgUp PgDn g G scroll, `w`/Esc back), `{`/`}` height | 커서 줄의 전체 내용: `v` 숨김/표시, `J`/`K` 스크롤, `w` 포커스(pane 확대; ↑↓ PgUp PgDn g G 스크롤, `w`/Esc 복귀), `{`/`}` 높이 |
| `a` · `A` | ask claude about the row (answer in a panel on the right half; `w` cycles focus list → preview → panel) · hide/show the panel | 커서 항목에 대해 claude에게 질문(답은 오른쪽 절반 panel; `w`로 포커스 순환) · panel 숨김/표시 |
| `d` · `o` | details pager · open in the browser | 상세 pager · 브라우저로 열기 |
| `u` | view home as another person (previous one goes on the back stack) | home의 "나"를 다른 사람으로(이전 관점은 뒤로가기 스택에) |
| `l` `c` `p` `t` `s` `H` `r` | tree/log · comments mode · people nodes · translation · summaries · hops 1/2/3 · refetch | tree/log · comments 모드 · 사람 노드 · 번역 · 요약 · hops · 재조회 |
| `/` `n` `N` · `<` `>` · `L` · `$` · `?` · `q` | search · horizontal scroll · legend · token usage · help · quit | 검색 · 가로 스크롤 · 범례 · 토큰 사용량 · 도움말 · 종료 |
| mouse / 마우스 | click = cursor; click `▾/▸` = fold; click a section header = fold; double-click = Enter (on a `@login`: view as that person); right click = open in browser; back/forward buttons = back/forward; click preview/answer panel = focus; wheel = scroll that area without moving the cursor | 클릭 = 커서 · `▾/▸` 클릭 = 접기 · 섹션 헤더 클릭 = 접기 · 더블클릭 = Enter(`@login` 위면 그 사람 관점) · 오른쪽 클릭 = 브라우저 · 뒤로/앞으로 버튼 · preview/답 panel 클릭 = 포커스 · 휠 = 커서는 두고 스크롤 |

## Token usage / 토큰 사용량

**EN** — Usage reported by `claude -p --output-format json` is accumulated from process start: the TUI title bar shows `tokens 31.2k in / 1.4k out · $0.052 · 7 calls`, `$` shows a per-phase table (translate / summarize / ask); the CLI prints one line on stderr at exit when any call was made. Cache hits cost nothing.

**KO** — `claude -p --output-format json`의 usage를 프로세스 시작 시점부터 누적한다. TUI 제목줄에 `tokens 31.2k in / 1.4k out · $0.052 · 7 calls`, `$` 키로 phase(translate / summarize / ask)별 표. CLI는 호출이 있었을 때 종료 시 stderr에 한 줄. 캐시 hit은 호출이 없으므로 0.

## Accounts, cache / 계정, 캐시

**EN** — If a private repo answers `NOT_FOUND`, the other accounts registered in `gh auth status` are tried automatically (no global account switch). Cache: `~/.cache/gitgraph/` (items per repo, translations, summaries; `--max-age` minutes, `--refresh` to force). TUI logs go to `~/.cache/gitgraph/tui.log`.

**KO** — private repo가 `NOT_FOUND`면 `gh auth status`에 등록된 다른 계정 토큰으로 자동 재시도한다(전역 계정 전환 없음). 캐시: `~/.cache/gitgraph/`(repo별 항목, 번역, 요약; `--max-age`분, `--refresh`로 강제). TUI 로그는 `~/.cache/gitgraph/tui.log`.
