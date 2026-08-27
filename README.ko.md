# gg (gitgraph)

[English](README.md)

GitHub issue · PR · 코멘트 · `@mention`이 서로 어떻게 연결돼 있는지 ASCII 그래프로 그린다. 터미널 출력과 curses TUI 두 가지. python 패키지 의존성 없음.

## 설치

python3 ≥ 3.9, `gh` CLI(`gh auth login` 완료)가 필요하다. `claude` CLI는 선택: 번역 · 코멘트 요약 · 질문에만 쓰이고, 없으면 그 기능만 조용히 꺼지고 그래프는 그대로 동작한다.

```
pipx install git+https://github.com/Daejun/gitgraph        # 권장 (pipx가 없으면: pip install --user git+…)
# 또는 파일 하나만:
curl -fsSL https://raw.githubusercontent.com/Daejun/gitgraph/main/gitgraph.py -o ~/.local/bin/gg && chmod +x ~/.local/bin/gg
gg update                                                    # 나중에 GitHub에서 갱신
```

git repo 안(또는 repo들이 모여 있는 상위 dir)에서 실행하면 `origin`에서 repo를 찾고, 여러 개면 번호로 물어본다. 아니면 `-r owner/name`. 늘 같은 repo를 보면 셸 rc에 `export GITGRAPH_REPOS=owner/name`.

환경변수: `GITGRAPH_REPOS`, `GITGRAPH_ME`("나"로 볼 login들), `GITGRAPH_LANG`(기본 Korean), `GITGRAPH_TRANSLATE`(zh|all|none), `GITGRAPH_TR_MODEL`(haiku), `GITGRAPH_ASK_MODEL`(sonnet), `GITGRAPH_BATCH`(10).

## 사용법

```
gg                        # open 항목 전체 개요 (tree)
gg -l log                 # git log --graph 식 시간축
gg 768 --hops 1           # #768 주변 1-hop (#768, owner/repo#768 도 됨)
gg @someone               # 사람 기준
gg show 748               # 노드 상세: edge 전부, 코멘트, 본문
gg ask 4563 "왜 #3859를 언급해?"   # 그 항목(본문+코멘트 전체)을 context로 claude에게 단발 질문
gg tui [768]              # 대화형 화면; 숫자를 주면 그 tree에서 시작
gg update                 # 설치 갱신
```

옵션: `-r owner/name`(반복 가능, 첫 번째가 primary), `-u LOGIN`(home의 "나"를 그 사람으로 — 관점만 바뀌고 로그인은 그대로), `--state open|all`, `--comments linked|all|none`(linked = `#N`/`@` 있는 코멘트만, graph·show 기본; all = tui 기본), `--no-people`, `--no-closed-neighbors`, `--max-age MIN`(캐시 TTL, 기본 15), `--refresh`, `-w N`(제목 폭), `-t zh|all|none`(번역, 기본 zh), `-S`(코멘트 요약), `--color auto|always|never`.

## 줄 형식

```
2026-08-13 #750 [PR] 제목  @author           # 날짜 = issue/PR이 열린 날; [PR] 파랑, [I] 초록
  +5d o @author » 요약                        # 코멘트: 열린 날 기준 +N일; » = 한 줄 요약
```

open이 아닐 때만 `[draft]` / `[merged]` / `[closed]`가 붙는다. `@login`은 사람마다 고유 색(256색, 처음 등장 순서로 배정).

## Edge 종류

| edge | 출처 |
|---|---|
| `→ refs` / `← cited-by` | 본문·코멘트의 `#N`, `owner/repo#N`, GitHub URL + timeline CROSS_REFERENCED 이벤트(닫힌 항목에서 오는 참조). `→` = 이 항목이 저것을 참조, `←` = 저것이 이 항목을 참조 |
| `→ closes` / `← closed-by` | PR의 `closingIssuesReferences` |
| `→ mentions` / `← mentioned-by` | `@login` (2자 이상, 숫자만은 제외) |
| `o` | issue comment, PR review 본문, inline review comment |

code fence 안과 kernel log / stack trace 줄(`[ 123.4]`, `Tainted:`, `PID:`, `PLATFORM --` 등)의 `#N`은 무시한다. `#20` 이하 작은 번호는 앞 단어가 `PR`, `issue`, `see`, `fixes` 같은 참조어일 때만 인정한다(`overwrite #5` 같은 서수 배제).

## 번역

제목과 코멘트 첫 줄 중 한자가 섞인 것(`-t zh`, 기본)을 `claude -p --model haiku`로 `GITGRAPH_LANG`(기본 Korean)으로 바꾼다. `-t all`이면 영어도. `~/.cache/gitgraph/translations.json`에 캐시되어 같은 문자열은 다시 묻지 않는다. `show`는 원문 제목도 같이 보여준다. Claude Code 로그인만 있으면 되고 API key는 필요 없다.

## 코멘트 한 줄 요약

`-S`(CLI) 또는 TUI 기본으로, 코멘트 줄이 첫 줄 발췌 대신 haiku가 만든 한 줄 요약(`» …`)이 된다. 본문 sha1 기준으로 `~/.cache/gitgraph/summaries.json`에 캐시(본문 4,000자, 호출당 40건 / 40,000자 상한). 생성 중에는 `» 요약 중…`, 실패하면 첫 줄 발췌로 돌아간다.

## 질문 (`a`, `gg ask`)

커서 항목의 본문과 코멘트 전체(코멘트당 6,000자, 총 60,000자 상한)를 질문과 함께 `claude -p --model $GITGRAPH_ASK_MODEL`(기본 sonnet)에 보낸다. 답은 `GITGRAPH_LANG`. 단발: 캐시 없음, 이어가기 없음.

## TUI

시작 화면은 섹션별 **home** 목록(헤더에서 Enter/Space로 접고 펼침, `-`/`+` 전부). "나" = `gh auth status`의 계정들(`GITGRAPH_ME` / `-u`로 지정).

| 섹션 | 내용 |
|---|---|
| my turn | 내가 관여(작성·코멘트·mention)한 항목 중 마지막 코멘트가 남의 것 → `← @누가 +Nd » 요약` |
| mentioning @me | 나를 mention한 항목 (최근 mention 순) |
| opened in the last N days | `--days N`(기본 7) 안에 열린 것 |
| active in the last N days | 그 기간에 갱신됐지만 새로 열린 건 아닌 것 |
| waiting on others | 내가 마지막으로 말한 것(또는 내가 열고 코멘트 없는 것) |
| opened by me / open PRs by others / stale(30일) / all open | 그대로 |

Enter로 그 항목 중심 tree, Tab으로 전체 overview(`--no-home`이면 overview로 시작). tree는 `--depth N`(기본 1)까지만 펼친 상태로 시작하고 노드 앞에 `▾` 펼침 / `▸` 접힘(`[+N]` 숨은 줄 수) / `·` leaf 표시. 번역·요약은 **화면에 보이는 줄부터** `GITGRAPH_BATCH`(기본 10)개씩 백그라운드로 처리하고, 접힌 노드는 펼칠 때 처리. 범례는 오른쪽 위 상자(`L`로 숨김). 진행 상황과 토큰 사용량은 상태줄·제목줄에. open 항목만 대상.

| 키 | 동작 |
|---|---|
| `↑`/`k` `↓`/`j` PgUp PgDn `g`/`G` | 커서 이동 (home에서 PgDn/PgUp은 다음/이전 섹션) |
| Space, `←`/`→` · `1`~`9` · `-`/`+` | 접기/펼치기 · 그 깊이까지 펼침 · depth 1로 접기 / 전부 펼침 |
| Enter | 노드: 그 노드로 재루팅 · `⇢`/`mentions` 줄: 가리키는 노드로 점프 |
| Tab | home ↔ 전체 overview |
| Backspace / `b` / `h` / Esc · `f` | 뒤로(화면·root·접힘·커서·관점 복원; preview·답 panel 포커스 중이면 포커스 해제만) · 앞으로 |
| preview pane | 화면 아래에 커서 줄의 전체 내용: `v` 숨김/표시, `J`/`K` 스크롤, `w` 포커스(pane 확대; ↑↓ PgUp PgDn g G 스크롤, `w`/Esc 복귀), `{`/`}` 높이 |
| `a` · `A` | 커서 항목에 대해 claude에게 질문(답은 오른쪽 절반 panel; `w`로 포커스 순환 목록 → preview → panel) · panel 숨김/표시 |
| `d` · `o` | 상세 pager · 브라우저로 열기 |
| `u` | home의 "나"를 다른 사람으로(이전 관점은 뒤로가기 스택에) |
| `l` `c` `p` `t` `s` `H` `r` | tree/log · comments 모드 · 사람 노드 · 번역 · 요약 · hops 1/2/3 · 재조회 |
| `/` `n` `N` · `<` `>` · `L` · `$` · `?` · `q` | 검색 · 가로 스크롤 · 범례 · 토큰 사용량 · 도움말 · 종료 |
| 마우스 | 클릭 = 커서 · `▾/▸` 클릭 = 접기 · 섹션 헤더 클릭 = 접기 · 더블클릭 = Enter(`@login` 위면 그 사람 관점) · 오른쪽 클릭 = 브라우저 · 뒤로/앞으로 버튼 · preview/답 panel 클릭 = 포커스 · 휠 = 커서는 두고 그 영역만 스크롤 |

## 토큰 사용량

`claude -p --output-format json`의 usage를 프로세스 시작 시점부터 누적한다. TUI 제목줄에 `tokens 31.2k in / 1.4k out · $0.052 · 7 calls`, `$` 키로 phase(translate / summarize / ask)별 표. CLI는 호출이 있었을 때 종료 시 stderr에 한 줄. 캐시 hit은 호출이 없으므로 0.

## 계정, 캐시

private repo가 `NOT_FOUND`면 `gh auth status`에 등록된 다른 계정 토큰으로 자동 재시도한다(전역 계정 전환 없음). 캐시: `~/.cache/gitgraph/`(repo별 항목, 번역, 요약; `--max-age`분, `--refresh`로 강제). TUI 로그는 `~/.cache/gitgraph/tui.log`.
