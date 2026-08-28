# gg (gitgraph)

[English](README.md)

GitHub issue · PR · 코멘트 · `@mention`이 서로 어떻게 연결돼 있는지 ASCII 그래프로 그린다 — 터미널 출력, 또는 키보드·마우스로 다니는 curses TUI. github.com과 GitHub Enterprise 지원. python 패키지 의존성 없음.

```
2026-08-13 #750 [PR] mtfs: running out of space must not disable the filesystem  @Daejun7Park  (1 comments, 1 linked)
├─ → refs 2026-08-13 #748 [I] Filling the device leaves the filesystem permanently unmountable  @Daejun7Park
│  ├─ ⇢ ← cited-by #749 #763:o+1d · comment #748:o+0d
│  └─ +0d o @Daejun7Park » #749/#750 제시, checkpoint 재시도 storm 해결책 및 측정 결과
└─ → refs 2026-08-13 #749 [PR][closed] recovery: a full inode zone is the end of the scan, not a bad address  @Daejun7Park
```

## 설치

필요한 것: python3 ≥ 3.9, 로그인된 `gh` CLI(`gh auth login`; GitHub Enterprise는 `gh auth login -h host`). `claude` CLI(또는 호환 변종, *설정* 참조)는 선택 — 번역·코멘트 요약·질문에만 쓰이고, 없으면 그 기능만 꺼지고 그래프는 그대로 동작한다.

```
pipx install git+https://github.com/Daejun/gitgraph        # 권장 (pipx가 없으면: pip install --user git+…)
# 또는 파일 하나만:
curl -fsSL https://raw.githubusercontent.com/Daejun/gitgraph/main/gitgraph.py -o ~/.local/bin/gg && chmod +x ~/.local/bin/gg
gg update                                                    # 나중에 GitHub에서 갱신 (세 설치 방식 모두)
gg --version
```

### repo 선택

git repo 안이나 repo들이 모여 있는 dir(2단계 아래까지)에서 `gg`를 실행하면 URL이 GitHub host(`github.com`, `github.*` host, 또는 `gh`에 로그인된 host)를 가리키는 모든 remote가 후보가 된다 — `origin` 우선, 다음은 이름이 `github*`인 remote(그래서 `origin`은 GitLab이고 `github` remote가 따로 있는 repo도 됨). 그리고 여러 개면 번호로 물어본다(`a` = 전부, 첫 번째가 *primary*라 그 항목은 `#N`으로만 표시). 잡힌 repo가 fork면 원본(parent) repo로 자동 전환한다(issue/PR은 거기 있으므로; fork 자체를 보려면 `-r`로 지정). 아니면 `-r owner/name`(반복 가능)을 주거나 `gg config repos owner/name`으로 기본값을 저장한다. GitHub Enterprise repo는 `host/owner/name`.

## 사용법

```
gg                        # TUI (기본)
gg 768                    # #768에서 시작하는 TUI (owner/repo#768, @someone 도 됨)
gg tutorial               # 화면 안내 투어와 함께 TUI (안에서는 F2)
gg graph                  # 텍스트 그래프: open 항목 전체 개요 (tree)
gg graph -l log           # git log --graph 식 시간축
gg graph 768 --hops 1     # #768 주변 텍스트 그래프
gg show 748               # 노드 상세: edge 전부(참조가 나온 문장 포함), 코멘트, 본문
gg ask 4563 "왜 #3859를 언급해?"   # 그 항목(본문+코멘트 전체)을 context로 claude에게 단발 질문
gg ai [NAME]              # AI CLI 목록/선택: claude, codex, gemini, grok, … (설치된 것 표시)
gg config [KEY [VALUE]]   # 설정 저장
gg todo                   # tui에서 m으로 표시한 것들의 markdown 출력
gg todo done|remove 750   # 마킹 완료 처리 / 삭제 (clear-done: 완료된 것 정리); Claude는 gg_todo_done으로 같은 일을 함
gg mcp                    # 다른 창의 Claude Code용 MCP 서버 (C 키 참조)
gg cache [clear …]        # 로컬에 저장된 것과 지우는 법
gg check [-r owner/name]  # 진단: host별 gh 계정, 계정별 접근 여부, open 수, GraphQL 필드
gg update                 # 설치 갱신
```

옵션: `-r owner/name` · `-u LOGIN`(TUI home의 "나"를 그 사람으로 — 관점만 바뀌고 로그인은 그대로) · `--state open|all`(all은 모든 issue/PR을 받아 느림) · `--comments linked|all|none`(linked = `#N`/`@`가 있는 코멘트만, graph·show 기본; all = tui 기본) · `--no-people` · `--no-closed-neighbors` · `--max-age MIN`(캐시 TTL, 기본 15) · `--refresh` · `-w N`(제목 폭) · `-t zh|all|none`(번역, 기본 zh) · `-S`(코멘트 요약) · `--color auto|always|never` · `--theme dark|light|basic` · TUI 전용: `--days N`(home 기간, 7), `--no-summary`.

## 설정 (`gg config`)

`gg config`는 모든 설정과 출처를 보여주고, `gg config KEY VALUE`는 `~/.config/gitgraph/config.json`에 저장, `gg config unset KEY`는 삭제. 우선순위: CLI 옵션 > `GITGRAPH_*` 환경변수 > config 파일 > 기본값.

| 키 | 환경변수 | 기본값 | 의미 |
|---|---|---|---|
| `claude_bin` | `GITGRAPH_CLAUDE` | `claude` | 번역·요약·질문에 쓰는 AI CLI, `gg ai`로 선택: `claude`(`-p --output-format json --model …`), `codex`(`codex exec … -o FILE`), `gemini`/`grok`/그 밖(`-p PROMPT`, 자체 기본 모델). 토큰 집계는 claude만. 선택한 CLI가 실패하면(로그인 안 됨·토큰 만료·미설치) TUI가 팝업으로 설치된 대안으로 전환 / 계속 시도 / 이 세션 AI 기능 끄기를 제안 |
| `repos` | `GITGRAPH_REPOS` | | 기본 repo, 콤마 구분 |
| `me` | `GITGRAPH_ME` | gh 계정들 | TUI home에서 "나"로 볼 login |
| `lang` | `GITGRAPH_LANG` | `Korean` | 번역·요약·답변 언어 |
| `translate` | `GITGRAPH_TRANSLATE` | `zh` | `zh`(한자만) / `all` / `none` |
| `tr_model` · `ask_model` | `GITGRAPH_TR_MODEL` · `GITGRAPH_ASK_MODEL` | `haiku` · `sonnet` | 모델(진짜 `claude`에만 적용) |
| `batch` | `GITGRAPH_BATCH` | `10` | TUI: 번역/요약 호출당 노드 수(job마다 절반씩) |
| `ai_parallel` | `GITGRAPH_AI_PARALLEL` | `3` | TUI: 동시에 돌리는 AI CLI 호출 수(요약·제목 번역·링크 이유; 긴 본문 번역은 덩어리로 나눠 병렬) |
| `retries` | `GITGRAPH_RETRIES` | `3` | `gh api` 일시 네트워크 오류 재시도 횟수 |
| `fetch_parallel` | `GITGRAPH_FETCH_PARALLEL` | `6` | 캐시를 채울 때(첫 실행·갱신) 동시에 도는 `gh` 쿼리 수. 쿼리 하나가 `gh` 프로세스 하나(네트워크 전에 이미 ~0.4s)라 첫 실행 속도를 좌우한다 |
| `side_width` · `expand_focused` · `expanded_weight` · `screen_mode` · `border` | `GITGRAPH_SIDE_WIDTH` … | `0.4` · `true` · `2` · `normal` · `rounded` | TUI layout (아래 참조) |
| `todo_file` | `GITGRAPH_TODO` | `~/gitgraph-todo.md` | `m`으로 표시한 것으로 만드는 markdown |
| `theme` | `GITGRAPH_THEME` | `dark` | 색 테마, vim의 `bg=`처럼: `dark`(256색), `light`(밝은 배경용 진한 색), `basic`(8색, dim·진한 파랑 없음 — PuTTY 등). 한 번만 바꾸려면 `--theme`, TUI에서는 `T`로 순환 |

## 줄 형식

```
2026-08-13 #750 [PR] 제목  @author           # 날짜 = issue/PR이 열린 날; [PR] 파랑, [I] 초록
  +5d 08-18 o @author » 요약                  # 코멘트: 열린 날 기준 +N일과 실제 날짜; » = 한 줄 요약
```

open이 아닐 때만 `[draft]` / `[merged]` / `[closed]`가 붙는다. `@login`은 사람마다 고유 색(256색, 처음 등장 순서로 배정). 색은 stdout이 터미널일 때 켜진다(`--color`).

## Edge 종류

| edge | 출처 |
|---|---|
| `→ refs` / `← cited-by` | 본문·코멘트의 `#N`, `owner/repo#N`, 전체 URL + GitHub timeline의 CROSS_REFERENCED 이벤트(닫힌 항목에서 오는 참조). `→` = 이 항목이 저것을 참조, `←` = 저것이 이 항목을 참조 |
| `→ closes` / `← closed-by` | PR의 `closingIssuesReferences` |
| `→ mentions` / `← mentioned-by` | `@login` (2자 이상, 숫자만은 제외) |
| `o` | issue comment, PR review 본문, inline review comment |
| `⇢ …` | 같은 tree의 다른 곳에 그려진 노드로의 링크(`#N:o+5d` = #N이 열린 지 5일 뒤 코멘트) |

code fence 안과 kernel log / stack trace 줄(`[ 123.4]`, `Tainted:`, `PID:`, `PLATFORM --` 등)의 `#N`은 무시한다. `#20` 이하 작은 번호는 앞 단어가 `PR`, `issue`, `see`, `fixes` 같은 참조어일 때만 인정한다(`overwrite #5` 같은 서수 배제). tree는 각 연결 덩어리에서 연결이 가장 많은 open 항목을 root로 한 shortest-path tree이고, 헤더에 그 이유가 적힌다(`tree rooted at #672 (most linked: 9 items)`).

## 번역·요약·질문 (`claude`)

- **번역** — 제목과 코멘트 첫 줄 중 한자가 섞인 것(`-t zh`)을 `tr_model`로 `lang`으로 바꾼다. `-t all`이면 영어도. `~/.cache/gitgraph/translations.json`에 캐시, `show`는 원문 제목도 같이 보여준다.
- **요약** (`-S`, TUI 기본) — 코멘트 줄이 첫 줄 발췌 대신 한 줄 요약(issue/PR 본문도 요약해 Links panel에 씀)(`» …`)이 된다. 본문 sha1 기준 `summaries.json` 캐시(본문 4,000자, 호출당 40건 / 40,000자 상한). 생성 중에는 `» 요약 중…`, 실패하면 첫 줄 발췌.
- **질문** (`a`, `gg ask`) — 모든 답은 질문과 함께 그 issue/PR/코멘트에 앵커되어 저장되고(`~/.config/gitgraph/qa.json`), 다음 세션에서도 그 대상의 answer 탭에 이전 질문·답이 다시 보인다(Item panel에 `2 Q&A`). 질문과 함께 그 항목(또는 커서의 코멘트, 표시됨)·메타·코멘트 전체 스레드(순서대로)·링크된 issue/PR과 그 참조 문장(총 90,000자까지)을 `ask_model`에 보내고, 답은 근거로 삼은 코멘트나 #번호를 밝힌다. 단발, 캐시 없음.
- Claude Code 로그인만 있으면 되고 API key는 필요 없다. `--output-format json`이 알려주는 usage를 프로세스 시작부터 누적: TUI 제목줄에 `tokens 31.2k in / 1.4k out · $0.052 · 7 calls`, `$` 키로 phase별 표, CLI는 종료 시 stderr 한 줄. "in"의 대부분은 캐시된 system prompt라 비용은 작다.

## TUI

lazygit 식 layout: 왼쪽 side column의 panel들(Repo · Item · Inbox · Comments · Links · People)과, 선택한 것을 보여주는 오른쪽 main panel.

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
│   ↳ #748의 후반부 구현 …     ││                                                       │
╭─6 People────────────────────╮│                                                       │
│ @Daejun7Park  author        ││                                                       │
╰─────────────────────────────╯╰───────────────────────────────────────────────────────╯
 ⏎ open item  [ ] section  / search  a ask  o browser   1-5 0 Tab panels  + _ screen  b f back/fwd  ? keys  q quit
```

| panel | 내용 | Enter |
|---|---|---|
| 1 Repo | repo, open 수, fetch 시각, "나", 토글 상태, 토큰 사용량 | – |
| 2 Item | 현재 item: 제목, 메타, `» 한 줄 요약`(없으면 본문 첫 줄), 코멘트/링크 수, URL | main에서 읽기 |
| 3 Inbox | 한 번에 한 섹션(`[` `]`): todo · my turn · mentions · opened · active · waiting · mine · PRs by others · stale · all — 규칙은 전과 같음("my turn" = 내가 관여한 항목 중 남이 마지막으로 말한 것, `--days N`, "나" = gh 계정 / `-u` / `u`). my turn / active / waiting / PRs 행에서는 커서가 **마지막 코멘트**를 미리 보여줌(main에 코멘트가 뜨는 이유); my turn 행 끝에 왜 내 차례인지 표시: `⟵ @who mentioned +5d`, `@who on my PR +14d`, `@who replied +2d` | **현재 item**으로 설정 — Item, Links, Comments, People이 따라옴 |
| 5 Links | 현재 item과 그 코멘트의 모든 edge: `→ refs`, `← cited-by`, `→ closes`, `← closed-by`(어느 코멘트 경유인지). 각 링크 아래 `↳` 줄(최대 두 줄)에 **이유**: 참조가 나온 문장을 요약기가 40자 이내 한 줄로 바꾼 것(예: `충돌 여부를 확인한 관련 PR`; 오기 전이거나 요약이 꺼져 있으면 `#N` 주변 짧은 인용), 그런 원문이 없으면(GitHub timeline에만 기록된 참조, 닫힌 항목) 그 issue/PR의 한 줄 요약(`» …`, 본문을 받아온 뒤 코멘트와 같은 요약기로; 대기 중엔 `» 요약 중…`) | 그 item으로 이동; 코멘트 행이면 그 item으로 가고 커서는 그 코멘트에(`b`/Esc로 복귀) |
| 4 Comments | 현재 item의 코멘트 `+Nd o @who » 요약`, 최신이 위 | main에서 읽기 |
| 6 People | 현재 item의 작성자·코멘트 작성자·mention된 사람, 최근 활동 순 | 그 사람 관점으로 Inbox 보기 |
| 0 Main | 탭(`[` `]`): **content** = 포커스된 side panel의 커서 줄 전문(URL이 첫 줄에 밑줄로; 본문+메타 또는 코멘트) — main에 포커스가 있는 동안은 그대로 유지; **answer** = 마지막 `a` 질문의 답. 둘 다 markdown 렌더링: 제목, 코드 블록·`code`, **굵게**, 링크, 인용, 글머리표, 표(열 맞춘 box 표, `H`/`L`로 가로 스크롤) | – |

Layout: side column 폭 `side_width`(0.4); 제목은 폭에 맞춰 `…`로 줄이고 폭이 바뀌면(드래그·리사이즈·screen mode) 다시 맞춤; 포커스된 side panel이 더 높고(`expand_focused`, `expanded_weight`) main으로 포커스가 가도 그 크기를 유지해 다음 선택이 쉬움; `+`/`_`로 screen mode normal → half(포커스 panel이 column 전체) → full(그 panel만); 84열 이하 좁은 터미널은 포커스된 side panel을 위, main을 아래에 쌓음; 테두리 `border`(rounded · single · double · bold · hidden). 번역·요약은 보이는 줄부터 백그라운드로(`batch`개씩), 대기 중인 요약은 `» 요약 중…`.

| 키 | 동작 |
|---|---|
| `1`~`6` · `0` · Tab / Shift-Tab | panel 점프 · main · 순환 |
| `[` `]` | 포커스 panel의 이전 / 다음 탭 |
| `+` `_` | screen mode normal → half → full |
| `↑`/`k` `↓`/`j` · PgUp/PgDn `,` `.` · `g`/`G` `<`/`>` · `H`/`L` | 이동 · 페이지 · 처음/끝 · 가로 스크롤 |
| `K` `J` | 어디서든 main panel 스크롤 |
| Enter | 위 표 참조 |
| `i` | main content를 **누를 때만** 전문 번역 — 다시 `i`면 원문. Main 제목줄의 `[i 번역]` 버튼도 같음. 백그라운드 자동 번역은 없다: 요청하기 전까지 본문은 원문 그대로(번역 중에는 `⟳ 번역 중…`; `translations_full.json` 캐시) |
| `m` | 선택한 issue/PR 또는 코멘트를 다음 작업으로 표시하고 메모를 적음; 표시된 행에 `✎`, Inbox의 **todo** 섹션(첫 탭; 시작은 여전히 my turn), 그리고 markdown 파일 `todo_file`(기본 `~/gitgraph-todo.md`; `gg todo`로 출력)이 다시 써져서 다음 세션이나 Claude가 그 문서를 보고 일을 이어갈 수 있음. 표시된 행에서 다시 `m`: 메모 수정 / 완료 / 삭제. answer 탭에서 `m`은 답 내용을 그 마킹의 메모로 저장. 원본은 `~/.config/gitgraph/todo.json` |
| `Del` | 선택 항목의 마킹을 바로 삭제(마킹된 행에서 `m`은 메모 수정 / 완료 / 삭제 메뉴) |
| `y` | 선택 항목의 URL을 클립보드로 |
| `a` · `d` · `o` | 선택에 대해 claude에게 질문(answer 탭) · 상세 pager · 브라우저로 열기(URL은 content 첫 줄에 밑줄로도 표시) |
| Esc / `b` · `f` | 뒤로(이전 item·관점) · 앞으로 |
| `u` | Inbox를 다른 사람 관점으로 |
| `r` · `R` | 백그라운드로 GitHub 갱신: 바뀐 것만(시작할 때마다, 그리고 `--max-age`분마다 자동으로도) · 전체 |
| `c` `t` `s` `p` `h` | comments 모드 · 번역 · 요약 · 사람 노드 · hops 1/2/3 |
| `/` `n` `N` · `T` · `$` · `q` | 포커스 panel 검색 · 색 테마 · 토큰 사용량 · 종료 |
| `C` | gg 옆에 Claude Code를 띄움(tmux면 옆 pane, 아니면 전체 화면). `gg mcp` 서버를 통해 지금 보는 것(`gg_state`, `gg_context`)과 마킹(`gg_todo`)을 읽고 gg를 조작(`gg_open`, `gg_mark`)하고 처리한 마킹을 지울 수 있음(`gg_todo_done`). 한 번 등록: `claude mcp add -s user gg -- gg mcp` |
| `?` · `O` · F1 · F2 | 포커스 panel의 키 메뉴(Enter로 실행) · 옵션 메뉴(comments / 번역 / 요약 / 사람 / hops / 테마 / screen) · 전체 도움말 · 화면 안내 투어(첫 실행 때 한 번 제안; `gg tutorial`도 같음; ⏎/→ 다음, ←/p 이전, Esc 중단) |
| 한글 IME | 한/영 전환을 안 해도 단축키가 먹음: 들어온 자모·음절을 2벌식 자판 키로 되돌림(`ㅓ` = j, `ㅏ` = k, `자` = w k) |
| 마우스 | Main 안에서 드래그 = 텍스트 선택, 놓으면 클립보드로(OSC 52 + 설치돼 있으면 `wl-copy`/`xclip`/`xsel`/`pbcopy`; tmux는 `set -g set-clipboard on`; Shift+드래그는 터미널 자체 선택) · Inbox 제목의 `‹` `›`나 Main 제목의 탭 이름 클릭 = 탭 전환 · 다른 panel 첫 클릭 = 포커스만(커서 유지) · 포커스된 panel 안 클릭 = 그 행 선택 · URL 글자 위 클릭 = 브라우저로 열기 · 더블클릭 = Enter · 휠 = 커서는 두고 그 panel 스크롤 · 뒤로/앞으로 버튼 · side/main 경계 드래그로 폭 조절(`gg config side_width`로 저장) |

입력(`a`, `/`, `u`)·메뉴(`?`, `O`)·확인(`r`)·텍스트(`d`, `$`, F1)는 중앙 팝업으로 뜨고 Esc로 닫는다.

tmux 안이면 마우스 보고를 켜야 한다(`set -g mouse on`).

테스트: `python3 tests/run.py`가 전부 돌린다 — 문법 검사, stdlib `unittest` 묶음, golden 렌더링, 그리고 pty에서 TUI를 돌려 `tests/vt.py`로 화면을 그려 검사하는 `tests/tui_smoke.py`. 전부 임시 `HOME`의 fixture repo를 쓰므로 `gh` 로그인·네트워크·AI CLI가 없어도 되고, 내 캐시는 건드리지 않는다. 한 묶음만: `python3 tests/run.py unit`, 테스트 하나만: `python3 -m unittest tests.test_parse.TestRefs.test_fence`.

## 로컬 데이터

gg가 보관하는 것은 모두 `~/.cache/gitgraph/`(디렉터리 0700, 파일 0600 — private repo의 본문·코멘트가 들어 있음)와 `~/.config/gitgraph/`의 작은 파일 둘입니다:

| 파일 | 내용 | 수명 |
|---|---|---|
| `items__<repo>__open.json` | repo 하나의 issue/PR 원본(본문·코멘트) | `--max-age`분(15) 지나면 open 항목의 번호·updatedAt만 가볍게 받아 **바뀐 것만** 다시 받고 닫힌 것은 뺌; `--refresh`는 전체 재조회; 30일 안 쓰면 시작 시 삭제 |
| `stubs__<repo>.json` | 참조된 항목(닫힌 것·다른 repo)의 제목/본문 | 하루 보관 |
| `translations.json`, `translations_full.json`, `summaries.json`, `whys.json` | AI 결과(텍스트 해시 기준) | 상한(2만 건 넘으면 오래된 것부터 삭제) |
| `tui.log` | tui stderr/진행 로그 | 1 MB 넘으면 잘라냄 |
| `accounts.json` | 어느 gh 계정이 어느 repo를 볼 수 있는지 | 바뀔 때 덮어씀 |
| `state.json`, `cmd*.json` | tui가 보는 것(`gg mcp`용) | 덮어씀 |
| `~/.config/gitgraph/config.json`, `todo.json`(+ `todo_file` markdown) | 설정, 마킹 | 사용자 것 |

`gg cache`로 크기·나이와 함께 목록을 보고, `gg cache clear all|items|ai|logs|owner/name`으로 지운다(지워도 필요할 때 다시 받거나 만든다).

## GitHub Enterprise

Enterprise host의 repo는 `host/owner/name`으로 쓴다(`-r ghe.example.com/team/proj`, 또는 `git@ghe.example.com:team/proj.git` 같은 remote에서 자동 인식). API 호출은 `gh api --hostname`과 그 host에 등록된 계정·토큰을 쓰고, 본문의 참조는 그 항목의 host 기준으로 해석한다. 실제 Enterprise 인스턴스에서는 아직 검증하지 못했으니 안 되는 부분이 있으면 알려 달라.

## 계정, 네트워크, 캐시

- private repo가 `NOT_FOUND`면 그 host에 등록된 다른 `gh` 계정 토큰으로 자동 재시도한다(전역 계정 전환 없음). 실패 시 host와 시도한 계정을 에러에 적는다. 성공한 계정은 repo별로 `accounts.json`에 기억하고, 그 전에는 체크아웃의 git config가 지목하는 계정을 쓴다(`credential.<host>.username`, `gh auth setup-git -u`가 쓰는 helper, remote URL의 `user@`). 그래서 첫 쿼리부터 맞는 계정으로 나가고, 계정을 찾느라 왕복 한 번을 버리지 않는다.
- 일시적 네트워크 오류(`TLS handshake timeout`, connection reset, 5xx)는 `retries`회 backoff 재시도 후, 확인할 것(`gh api user`, `HTTPS_PROXY`, VPN/DNS)을 안내한다.
- 캐시: `~/.cache/gitgraph/` — repo·state별 항목(`--max-age`, `--refresh`), 번역, 요약. TUI 로그(stderr)는 `~/.cache/gitgraph/tui.log`.

## 라이선스

MIT
