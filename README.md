# gg (gitgraph)

GitHub issue / PR / comment / @mention 관계를 ASCII 그래프로 그린다. python 패키지 의존성 없음.

## 설치

필요한 것: python3 ≥ 3.9, `gh` CLI(`gh auth login` 완료), 선택적으로 `claude` CLI(번역·요약·질문에만 사용; 없으면 그 기능만 조용히 꺼짐).

```
pipx install git+https://github.com/Daejun/gitgraph      # 권장 (pipx가 없으면: pip install --user git+…)
# 또는 단일 파일만:
curl -fsSL https://raw.githubusercontent.com/Daejun/gitgraph/main/gitgraph.py -o ~/.local/bin/gg && chmod +x ~/.local/bin/gg
```

첫 실행은 git repo 안(또는 repo들이 모여 있는 상위 dir)에서 하거나 `-r owner/name`을 준다. 항상 같은 repo를 보면
`export GITGRAPH_REPOS=owner/name`을 셸 rc에 넣는다. 환경변수 한눈에: `GITGRAPH_REPOS`, `GITGRAPH_ME`, `GITGRAPH_LANG`(기본 Korean),
`GITGRAPH_TRANSLATE`(zh|all|none), `GITGRAPH_TR_MODEL`(haiku), `GITGRAPH_ASK_MODEL`(sonnet), `GITGRAPH_BATCH`(10).

```
gg                        # open 항목 전체 개요 (tree). repo는 현재 dir 아래 git repo의 origin에서 찾고, 여러 개면 번호로 물어봄
gg -l log                 # git log --graph 식 시간축
gg 768 --hops 1           # #768 주변 1-hop (#768, owner/repo#768 도 됨)
gg @someone               # 사람 기준
gg show 748               # 노드 상세 (edge 전부 + 코멘트 + 본문)
gg ask 4563 "왜 #3859를 언급해?"   # 그 항목(본문+코멘트 전체)을 context로 claude에게 단발 질문
gg update                 # 설치 방식(git checkout / pipx / pip / 단일 파일)에 맞게 GitHub에서 갱신
gg tui [768]              # curses 화면: home 목록(최근 N일 열린 항목 + 나를 mention한 항목) → Enter로 tree. 숫자를 주면 그 tree에서 시작
```

repo 선택 순서: `-r owner/name`(반복 가능, 첫 번째가 primary) > `GITGRAPH_REPOS` > 현재 dir(과 2단계 아래)의 git repo `origin`(여러 개면 질문, `a`=전부). 아무것도 없으면 에러.

옵션: `--state open|all`, `--comments linked|all|none`(graph/show 기본 linked = `#N`/`@` 있는 코멘트만, tui 기본 all),
`--no-people`, `--no-closed-neighbors`, `--max-age MIN`(캐시 TTL, 기본 15), `--refresh`, `-w N`(제목 폭),
`-t zh|all|none`(번역, 기본 zh), `-S`(코멘트 요약), `--color auto|always|never`(tty면 자동 색: open 초록 / merged 자주 / closed 빨강 / 코멘트 청록 / 사람 노랑 / `→` 파랑 / `←` 자주).

## 줄 형식

```
2026-08-13 #750 [PR] 제목  @author                    # 날짜 = issue/PR이 열린 날; [PR] 파랑 / [I] 초록, open이 아닐 때만 [draft]/[merged]/[closed]
  +5d o @author » 요약                                 # 코멘트: 열린 날 기준 +N일
```
`@login`은 사람마다 고유 색(256색 팔레트, 처음 등장 순서로 배정).

## Edge 종류

| edge | 출처 |
|---|---|
| `→ refs` / `← cited-by` | 본문·코멘트의 `#N`, `owner/repo#N`, GitHub URL + timeline CROSS_REFERENCED 이벤트(닫힌 항목에서 오는 참조). `→` = 이 항목이 저것을 참조, `←` = 저것이 이 항목을 참조 |
| `→ closes` / `← closed-by` | PR의 `closingIssuesReferences` |
| `→ mentions` / `← mentioned-by` | `@login` (2자 이상, 숫자만은 제외) |
| `o` (comment) | issue comment, PR review 본문, PR inline review comment |

kernel log / stack trace 줄(`[ 123.4]`, `Tainted:`, `PID:`, `PLATFORM --` 등)과 code fence 안의 `#N`은 무시한다.
`#20` 이하 작은 번호는 앞 단어가 `PR`, `issue`, `see`, `fixes` 같은 참조어일 때만 인정한다 (`overwrite #5` 같은 서수 배제).

## 번역

제목과 코멘트 첫 줄 중 한자가 섞인 것(`-t zh`, 기본)을 `claude -p --model haiku`로 한국어로 바꾼다. `-t all`이면 영어도.
결과는 `~/.cache/gitgraph/translations.json`에 캐시되어 같은 문자열은 다시 묻지 않는다. 첫 호출은 1~2분 걸릴 수 있다.
`show`는 원문 제목을 `original title:` 줄로 같이 보여준다. 환경변수: `GITGRAPH_TRANSLATE`(기본 모드),
`GITGRAPH_LANG`(기본 Korean), `GITGRAPH_TR_MODEL`(기본 haiku). Claude Code 로그인만 있으면 되고 API key는 필요 없다.

## 코멘트 한 줄 요약

TUI는 기본으로(CLI/MCP는 `-S` / `summary: true`) 코멘트 한 줄을 첫 줄 발췌 대신 `claude -p --model haiku`가 만든
한국어 한 줄 요약(`» …`)으로 보여준다. 본문 sha1 기준으로 `~/.cache/gitgraph/summaries.json`에 캐시되며,
본문은 4,000자까지만 보내고 한 호출에 40건/40,000자까지 묶는다. 실패하면 조용히 첫 줄 발췌로 돌아간다.

## 질문 (`a` / `gg ask`)

커서 항목의 본문과 코멘트 전체(코멘트당 6,000자, 총 60,000자 상한)를 붙여 `claude -p --model $GITGRAPH_ASK_MODEL`(기본 sonnet)에 한 번 묻는다.
답변 언어는 `GITGRAPH_LANG`(기본 Korean). 캐시 없음, 대화 이어가기 없음(단발).

## TUI 키

시작 화면은 **home** — 섹션별 목록(Enter/Space로 접고 펼침, `-`/`+` 전부). 나 = `gh auth status`의 계정들(`GITGRAPH_ME=login,login`으로 지정).

| 섹션 | 내용 |
|---|---|
| my turn | 내가 관여(작성·코멘트·mention)한 항목 중 마지막 코멘트가 남의 것 → `← @누가 +Nd » 요약` |
| mentioning @me | 나를 mention한 항목 (최근 mention 순) |
| opened in the last N days | `--days N`(기본 7) 안에 열린 것 |
| active in the last N days | 그 기간에 갱신됐지만 새로 열린 건 아닌 것 |
| waiting on others | 내가 마지막으로 말한 것(또는 내가 열고 코멘트 없는 것) |
| opened by me / open PRs by others / stale(30일 무갱신) / all open | 그대로 |

Enter로 그 항목 중심 tree, Tab으로 전체 overview, `--no-home`이면 overview로 시작. open 항목만 대상.
tree는 `--depth N`(기본 1)까지만 펼친 상태로 시작하고 각 노드 앞에 `▾` 펼침 / `▸` 접힘(`[+N]` 숨은 줄 수) / `·` leaf 표시가 붙는다.
번역·요약은 **화면에 보이는 줄부터** `GITGRAPH_BATCH`(기본 10)개씩 백그라운드로 처리하고 처리되는 대로 갱신한다(그다음 같은 화면의 나머지, 접힌 노드는 펼칠 때).
범례는 오른쪽 위 상자(`L`로 숨김).

| 키 | 동작 |
|---|---|
| Tab | home ↔ 전체 overview |
| `1`~`9` | 그 깊이까지 펼침 · `-` depth 1로 접기 · `+` 전부 펼침 |
| PgDn / PgUp | home에서는 다음 / 이전 섹션으로 이동 (tree에서는 페이지 이동) |
| `↑`/`k` `↓`/`j` PgUp PgDn `g`/`G` | 커서 이동 |
| Space, `←`/`→` | 노드 접기/펼치기 (tree) · `-`/`+` 전부 접기/펼치기 |
| Enter | 노드 위: 그 노드로 focus(재루팅) · `⇢`/`mentions` 줄 위: 가리키는 노드로 점프 |
| Backspace / `b` / `h` / Esc | 이전 화면으로 — 화면·root·접힘 상태·커서·관점(`u`)까지 복원 (preview·답 panel 포커스 중이면 포커스 해제만) · `f` 앞으로 |
| (자동) | 화면 아래 preview pane에 커서가 놓인 줄의 전체 내용(제목·메타·본문 또는 코멘트 전문, 사람이면 언급 목록) · `v` 숨기기/보이기 · `J`/`K` 한 줄 스크롤 · `w` preview에 포커스 — pane이 화면 대부분으로 커지고 ↑↓/PgUp/PgDn/g/G로 스크롤, `w`/Esc로 복귀 · `{`/`}` 높이 |
| `a` | 커서 항목에 대해 claude에게 단발 질문(본문+코멘트 전체가 context). 답은 **오른쪽 절반 panel**에 표시(질문 중엔 "asking…") · `A` panel 숨김/표시 · `w` 포커스 순환(목록 → preview → 답 panel)으로 스크롤 |
| `d` | 상세 (edge, 코멘트별 참조 대상, 본문) · `o` 브라우저로 열기 |
| `u` home의 "나"를 다른 사람(@login)으로 바꿔 보기(이전 관점은 뒤로가기 스택에 들어감) · `l` tree/log · `c` comments 모드 순환 · `p` 사람 노드 on/off · `t` 번역 on/off · `s` 코멘트 요약 on/off · `H` hops 1/2/3 · `r` 재조회 |
| `/` `n` `N` | 검색 · `<` `>` 가로 스크롤 · `?` 도움말 · `$` claude 토큰 사용량 · `q` 종료 |
| 마우스 | 클릭 = 커서 이동 · `@login` 더블클릭 = 그 사람 관점으로 home · `▾/▸` 클릭 = 접기/펼치기 · 섹션 헤더 클릭 = 접기 · 더블클릭 = Enter · 오른쪽 클릭 = 브라우저로 열기 · 뒤로/앞으로 버튼 = back/forward(panel 포커스 중이면 포커스 해제) · preview/답 panel 클릭 = 포커스 · 휠 = 커서는 두고 그 영역만 스크롤 |

시작: GitHub fetch만 기다린 뒤 바로 화면을 띄우고, 보이는 노드의 번역·요약은 백그라운드 스레드에서 돌린다. 그동안 상태줄에
`⠧ summarizing comments (claude haiku) [####........] 20/51  batch 2/3  1m03s` 식으로 진행이 보이고 끝나면 줄이 갱신된다.
로딩 중에도 `q`로 나갈 수 있다. stderr(진행 로그)는 `~/.cache/gitgraph/tui.log`로 간다.

## 토큰 사용량

`claude -p --output-format json`의 usage를 프로세스 시작 시점부터 누적한다. TUI 제목줄에 `tokens 31.2k in / 1.4k out · $0.052 · 7 calls`,
`$` 키로 phase(translate / summarize / ask)별 표. CLI는 호출이 있었을 때 종료 시 stderr에 한 줄. 캐시 hit(translations.json 등)는 호출이 없으므로 0.

## 계정

private repo가 `NOT_FOUND`면 `gh auth status`에 등록된 다른 계정 토큰으로 자동 재시도한다 (전역 계정 전환 없음).

캐시: `~/.cache/gitgraph/`.
