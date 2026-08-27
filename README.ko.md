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

옵션: `-r owner/name` · `-u LOGIN`(TUI home의 "나"를 그 사람으로 — 관점만 바뀌고 로그인은 그대로) · `--state open|all`(all은 모든 issue/PR을 받아 느림) · `--comments linked|all|none`(linked = `#N`/`@`가 있는 코멘트만, graph·show 기본; all = tui 기본) · `--no-people` · `--no-closed-neighbors` · `--max-age MIN`(캐시 TTL, 기본 15) · `--refresh` · `-w N`(제목 폭) · `-t zh|all|none`(번역, 기본 zh) · `-S`(코멘트 요약) · `--color auto|always|never` · TUI 전용: `--depth N`(시작 펼침 깊이, 1), `--days N`(home 기간, 7), `--no-home`, `--no-summary`.

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

시작 화면은 섹션별 **home** 목록(헤더에서 Enter/Space로 접고 펼침, `-`/`+` 전부, PgDn/PgUp으로 섹션 이동). "나" = gh 계정들, 또는 `me` / `-u`.

| 섹션 | 내용 |
|---|---|
| my turn | 내가 관여(작성·코멘트·mention)한 항목 중 마지막 코멘트가 남의 것 → `← @누가 +Nd » 요약` |
| mentioning @me | 나를 mention한 항목 (최근 mention 순) |
| opened in the last N days | `--days N`(기본 7) 안에 열린 것 |
| active in the last N days | 그 기간에 갱신됐지만 새로 열린 건 아닌 것 |
| waiting on others | 내가 마지막으로 말한 것(또는 내가 열고 코멘트 없는 것) |
| opened by me / open PRs by others / stale(30일) / all open | 그대로 |

Enter로 그 항목 중심 tree, Tab으로 전체 overview(`--no-home`이면 overview로 시작). tree는 `--depth N`(기본 1)까지만 펼친 상태로 시작하고 노드 앞에 `▾` 펼침 / `▸` 접힘(`[+N]` 숨은 줄 수) / `·` leaf 표시. 번역·요약은 **화면에 보이는 줄부터** `batch`개씩 백그라운드로 처리하고, 접힌 노드는 펼칠 때 처리. 화면 아래 preview pane에 커서 줄의 전체 내용, 질문의 답은 오른쪽 절반 panel. 범례는 오른쪽 위 상자(`L`로 숨김). open 항목만 대상.

| 키 | 동작 |
|---|---|
| `↑`/`k` `↓`/`j` PgUp PgDn `g`/`G` | 커서 이동 (home: PgDn/PgUp = 다음/이전 섹션) |
| Space, `←`/`→` · `1`~`9` · `-`/`+` | 접기/펼치기 · 그 깊이까지 펼침 · depth 1로 접기 / 전부 펼침 |
| Enter | 노드: 그 노드로 재루팅 · `⇢`/`mentions` 줄: 가리키는 노드로 점프 |
| Tab | home ↔ 전체 overview |
| Backspace / `b` / `h` / Esc · `f` | 뒤로(화면·root·접힘·커서·관점 복원; preview·답 panel 포커스 중이면 포커스 해제만) · 앞으로 |
| `v` · `J`/`K` · `w` · `{`/`}` | preview pane: 숨김/표시 · 스크롤 · 포커스(pane 확대; ↑↓ PgUp PgDn g G 스크롤, `w`/Esc 복귀; `w`는 목록 → preview → 답 panel 순환) · 높이 |
| `a` · `A` | 커서 항목에 대해 claude에게 질문 · 답 panel 숨김/표시 |
| `d` · `o` | 상세 pager · 브라우저로 열기 |
| `u` | home의 "나"를 다른 사람으로(이전 관점은 뒤로가기 스택에) |
| `l` `c` `p` `t` `s` `H` `r` | tree/log · comments 모드 · 사람 노드 · 번역 · 요약 · hops 1/2/3 · 재조회 |
| `/` `n` `N` · `<` `>` · `L` · `$` · `?` · `q` | 검색 · 가로 스크롤 · 범례 · 토큰 사용량 · 도움말 · 종료 |
| 마우스 | 클릭 = 커서 · `▾/▸` 클릭 = 접기 · 섹션 헤더 클릭 = 접기 · 더블클릭 = Enter(`@login` 위면 그 사람 관점) · 오른쪽 클릭 = 브라우저 · 뒤로/앞으로 버튼 · preview/답 panel 클릭 = 포커스 · 휠 = 커서는 두고 그 영역만 스크롤 |

tmux 안이면 마우스 보고를 켜야 한다(`set -g mouse on`).

## GitHub Enterprise

Enterprise host의 repo는 `host/owner/name`으로 쓴다(`-r ghe.example.com/team/proj`, 또는 `git@ghe.example.com:team/proj.git` 같은 remote에서 자동 인식). API 호출은 `gh api --hostname`과 그 host에 등록된 계정·토큰을 쓰고, 본문의 참조는 그 항목의 host 기준으로 해석한다. 실제 Enterprise 인스턴스에서는 아직 검증하지 못했으니 안 되는 부분이 있으면 알려 달라.

## 계정, 네트워크, 캐시

- private repo가 `NOT_FOUND`면 그 host에 등록된 다른 `gh` 계정 토큰으로 자동 재시도한다(전역 계정 전환 없음). 실패 시 host와 시도한 계정을 에러에 적는다.
- 일시적 네트워크 오류(`TLS handshake timeout`, connection reset, 5xx)는 `retries`회 backoff 재시도 후, 확인할 것(`gh api user`, `HTTPS_PROXY`, VPN/DNS)을 안내한다.
- 캐시: `~/.cache/gitgraph/` — repo·state별 항목(`--max-age`, `--refresh`), 번역, 요약. TUI 로그(stderr)는 `~/.cache/gitgraph/tui.log`.

## 라이선스

MIT
