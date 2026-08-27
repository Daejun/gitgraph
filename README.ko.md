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

git repo 안이나 repo들이 모여 있는 dir(2단계 아래까지)에서 `gg`를 실행하면 URL이 GitHub host(`github.com`, `github.*` host, 또는 `gh`에 로그인된 host)를 가리키는 모든 remote가 후보가 된다 — `origin` 우선, 다음은 이름이 `github*`인 remote(그래서 `origin`은 GitLab이고 `github` remote가 따로 있는 repo도 됨). 그리고 여러 개면 번호로 물어본다(`a` = 전부, 첫 번째가 *primary*라 그 항목은 `#N`으로만 표시). 아니면 `-r owner/name`(반복 가능)을 주거나 `gg config repos owner/name`으로 기본값을 저장한다. GitHub Enterprise repo는 `host/owner/name`.

## 사용법

```
gg                        # open 항목 전체 개요 (tree)
gg -l log                 # git log --graph 식 시간축
gg 768 --hops 1           # #768 주변 1-hop (#768, owner/repo#768 도 됨)
gg @someone               # 사람 기준
gg show 748               # 노드 상세: edge 전부, 코멘트, 본문
gg ask 4563 "왜 #3859를 언급해?"   # 그 항목(본문+코멘트 전체)을 context로 claude에게 단발 질문
gg tui [768]              # 대화형 화면; 숫자를 주면 그 tree에서 시작
gg config [KEY [VALUE]]   # 설정 저장
gg update                 # 설치 갱신
```

옵션: `-r owner/name` · `-u LOGIN`(TUI home의 "나"를 그 사람으로 — 관점만 바뀌고 로그인은 그대로) · `--state open|all`(all은 모든 issue/PR을 받아 느림) · `--comments linked|all|none`(linked = `#N`/`@`가 있는 코멘트만, graph·show 기본; all = tui 기본) · `--no-people` · `--no-closed-neighbors` · `--max-age MIN`(캐시 TTL, 기본 15) · `--refresh` · `-w N`(제목 폭) · `-t zh|all|none`(번역, 기본 zh) · `-S`(코멘트 요약) · `--color auto|always|never` · `--theme dark|light|basic` · TUI 전용: `--depth N`(시작 펼침 깊이, 1), `--days N`(home 기간, 7), `--no-summary`.

## 설정 (`gg config`)

`gg config`는 모든 설정과 출처를 보여주고, `gg config KEY VALUE`는 `~/.config/gitgraph/config.json`에 저장, `gg config unset KEY`는 삭제. 우선순위: CLI 옵션 > `GITGRAPH_*` 환경변수 > config 파일 > 기본값.

| 키 | 환경변수 | 기본값 | 의미 |
|---|---|---|---|
| `claude_bin` | `GITGRAPH_CLAUDE` | `claude` | 번역·요약·질문에 쓰는 바이너리. Claude 호환 변종(예: `cla`)은 같은 인자(`-p --no-session-persistence --output-format json <prompt>`)를 받되 **`--model`은 붙이지 않음** — 변종 자체의 기본 모델을 쓴다 |
| `repos` | `GITGRAPH_REPOS` | | 기본 repo, 콤마 구분 |
| `me` | `GITGRAPH_ME` | gh 계정들 | TUI home에서 "나"로 볼 login |
| `lang` | `GITGRAPH_LANG` | `Korean` | 번역·요약·답변 언어 |
| `translate` | `GITGRAPH_TRANSLATE` | `zh` | `zh`(한자만) / `all` / `none` |
| `tr_model` · `ask_model` | `GITGRAPH_TR_MODEL` · `GITGRAPH_ASK_MODEL` | `haiku` · `sonnet` | 모델(진짜 `claude`에만 적용) |
| `batch` | `GITGRAPH_BATCH` | `10` | TUI: 번역/요약 호출당 노드 수 |
| `retries` | `GITGRAPH_RETRIES` | `3` | `gh api` 일시 네트워크 오류 재시도 횟수 |
| `side_width` · `expand_focused` · `expanded_weight` · `screen_mode` · `border` | `GITGRAPH_SIDE_WIDTH` … | `0.33` · `true` · `2` · `normal` · `rounded` | TUI layout (see below) |
| `theme` | `GITGRAPH_THEME` | `dark` | 색 테마, vim의 `bg=`처럼: `dark`(256색), `light`(밝은 배경용 진한 색), `basic`(8색, dim·진한 파랑 없음 — PuTTY 등). 한 번만 바꾸려면 `--theme`, TUI에서는 `T`로 순환 |

## 줄 형식

```
2026-08-13 #750 [PR] 제목  @author           # 날짜 = issue/PR이 열린 날; [PR] 파랑, [I] 초록
  +5d o @author » 요약                        # 코멘트: 열린 날 기준 +N일; » = 한 줄 요약
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
- **요약** (`-S`, TUI 기본) — 코멘트 줄이 첫 줄 발췌 대신 한 줄 요약(`» …`)이 된다. 본문 sha1 기준 `summaries.json` 캐시(본문 4,000자, 호출당 40건 / 40,000자 상한). 생성 중에는 `» 요약 중…`, 실패하면 첫 줄 발췌.
- **질문** (`a`, `gg ask`) — 그 항목의 본문과 코멘트 전체(코멘트당 6,000자, 총 60,000자)를 질문과 함께 `ask_model`에 보낸다. 단발, 캐시 없음.
- Claude Code 로그인만 있으면 되고 API key는 필요 없다. `--output-format json`이 알려주는 usage를 프로세스 시작부터 누적: TUI 제목줄에 `tokens 31.2k in / 1.4k out · $0.052 · 7 calls`, `$` 키로 phase별 표, CLI는 종료 시 stderr 한 줄. "in"의 대부분은 캐시된 system prompt라 비용은 작다.

## TUI

lazygit 식 layout: 왼쪽 side column의 panel들과, 선택한 것을 보여주는 오른쪽 main panel.

```
╭─1 Repo──────────────────────╮╭─0 Main [content] tree log answer──────────────────────╮
│ owner/name  57 open  15:44  ││ #750 [PR] mtfs: running out of space must not …       │
│ item: 2026-08-13 #750 [PR] …││ @Daejun7Park  2026-08-13  updated 2026-08-19          │
╰─────────────────────────────╯│                                                       │
╭─2 Home ‹ my turn 2 › 1/9────╮│ Running the device out of space under concurrent …   │
│ 2026-08-17 #763 [I] xfstest…││                                                       │
╭─3 Links─────────────────────╮│                                                       │
│ → refs 2026-08-13 #748 [I] …││                                                       │
╭─4 Comments [all] linked─────╮│                                                       │
│ +0d o @Daejun7Park » …      ││                                                       │
╭─5 People────────────────────╮│                                                       │
│ @Daejun7Park  author        ││                                                       │
╰─────────────────────────────╯╰───────────────────────────────────────────────────────╯
 ⏎ open item  [ ] section  / search  a ask  o browser   1-5 0 Tab panels  + _ screen  b f back/fwd  ? keys  q quit
```

| panel | 내용 | Enter |
|---|---|---|
| 1 Repo | repo, open 수, fetch 시각, "나", 현재 item, 토글 상태, 토큰 사용량, 백그라운드 진행 | – |
| 2 Home | 한 번에 한 섹션(`[` `]`): my turn · mentions · opened · active · waiting · mine · PRs by others · stale · all — 규칙은 전과 같음("my turn" = 내가 관여한 항목 중 남이 마지막으로 말한 것, `--days N`, "나" = gh 계정 / `-u` / `u`) | **현재 item**으로 설정 — Links, Comments, People, tree가 따라옴 |
| 3 Links | 현재 item과 그 코멘트의 모든 edge: `→ refs`, `← cited-by`, `→ closes`, `← closed-by`(어느 코멘트 경유인지) | 그 item으로 이동(`b`/Esc로 복귀) |
| 4 Comments | 현재 item의 코멘트 `+Nd o @who » 요약`(`[` `]` all / linked) | main에서 읽기 |
| 5 People | 현재 item의 작성자·코멘트 작성자·mention된 사람 | 그 사람 관점으로 Home 보기 |
| 0 Main | 탭(`[` `]`): **content** = 포커스된 side panel의 커서 줄 전문(본문+메타 또는 코멘트) — Enter로 정한 item/코멘트는 `x`로 풀 때까지 **고정(hold)**(제목에 `⊙ hold #750` / `~ follows cursor`); **tree** / **log** = 현재 item 주변 그래프(`--hops`, Space/←/→·`-`/`=` 접기, Enter 재루팅, `⇢` 줄 점프, `▾ ▸ ·` 표시); **answer** = 마지막 `a` 질문의 답 | tree: 그 노드로 재루팅 |

Layout: side column 폭 `side_width`(0.33); 포커스된 side panel이 더 높음(`expand_focused`, `expanded_weight`); `+`/`_`로 screen mode normal → half(포커스 panel이 column 전체) → full(그 panel만); 84열 이하 좁은 터미널은 포커스된 side panel을 위, main을 아래에 쌓음; 테두리 `border`(rounded · single · double · bold · hidden). 번역·요약은 보이는 줄부터 백그라운드로(`batch`개씩), 대기 중인 요약은 `» 요약 중…`.

| 키 | 동작 |
|---|---|
| `1`~`5` · `0` · Tab / Shift-Tab | panel 점프 · main · 순환 |
| `[` `]` | 포커스 panel의 이전 / 다음 탭 |
| `+` `_` | screen mode normal → half → full |
| `↑`/`k` `↓`/`j` · PgUp/PgDn `,` `.` · `g`/`G` `<`/`>` · `H`/`L` | 이동 · 페이지 · 처음/끝 · 가로 스크롤 |
| `K` `J` | 어디서든 main panel 스크롤 |
| Enter | 위 표 참조 |
| Space, `←`/`→` · `-` `=` | tree 노드 접기/펼치기 · depth 1로 접기 / 전부 펼침 |
| `x` | main content 고정 / 따라가기 토글 |
| `a` · `d` · `o` | 선택에 대해 claude에게 질문(answer 탭) · 상세 pager · 브라우저로 열기 |
| Esc / `b` · `f` | 뒤로(이전 item·관점) · 앞으로 |
| `u` · `r` | Home을 다른 사람 관점으로 · 재조회 |
| `c` `t` `s` `p` `h` | comments 모드 · 번역 · 요약 · 사람 노드 · hops 1/2/3 |
| `/` `n` `N` · `T` · `$` · `?` · `q` | 포커스 panel 검색 · 색 테마 · 토큰 사용량 · 도움말 · 종료 |
| 마우스 | 클릭 = 포커스 + 선택 · 더블클릭 = Enter(tree의 `▾/▸` 위면 접기) · 오른쪽 클릭 = 브라우저 · 휠 = 커서는 두고 그 panel 스크롤 · 뒤로/앞으로 버튼 |

tmux 안이면 마우스 보고를 켜야 한다(`set -g mouse on`).

## GitHub Enterprise

Enterprise host의 repo는 `host/owner/name`으로 쓴다(`-r ghe.example.com/team/proj`, 또는 `git@ghe.example.com:team/proj.git` 같은 remote에서 자동 인식). API 호출은 `gh api --hostname`과 그 host에 등록된 계정·토큰을 쓰고, 본문의 참조는 그 항목의 host 기준으로 해석한다. 실제 Enterprise 인스턴스에서는 아직 검증하지 못했으니 안 되는 부분이 있으면 알려 달라.

## 계정, 네트워크, 캐시

- private repo가 `NOT_FOUND`면 그 host에 등록된 다른 `gh` 계정 토큰으로 자동 재시도한다(전역 계정 전환 없음). 실패 시 host와 시도한 계정을 에러에 적는다.
- 일시적 네트워크 오류(`TLS handshake timeout`, connection reset, 5xx)는 `retries`회 backoff 재시도 후, 확인할 것(`gh api user`, `HTTPS_PROXY`, VPN/DNS)을 안내한다.
- 캐시: `~/.cache/gitgraph/` — repo·state별 항목(`--max-age`, `--refresh`), 번역, 요약. TUI 로그(stderr)는 `~/.cache/gitgraph/tui.log`.

## 라이선스

MIT
