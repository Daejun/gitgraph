# gg 테스트 강화 계획 (회귀 방어)

상태: **Phase 0~5 구현 완료** (2026-08-28). `python3 tests/run.py` = 단위 346개 + golden + pty smoke 55개 체크,
전부 오프라인. 아래 본문은 구현된 구조를 그대로 설명한다. 구현 중 드러난 gitgraph.py 의심 버그는 문서 끝
"발견한 회귀" 절에 모았다.

## 왜

지금 테스트는 `tests/tui_smoke.py` 하나(153줄, 체크 18개)뿐이고 **실제 `gh` 로그인 + 사용자의 실제 repo 캐시**를
전제한다. 그래서 (1) 그날 GitHub 데이터에 따라 결과가 달라지고, (2) 화면 문자열을 golden으로 고정할 수 없고,
(3) parse / graph / width / markdown / todo / cache / AI 계층은 커버가 0이며, (4) 단일 테스트 선택자가 없다.

## 전제 (실측으로 확인함)

`HOME`만 임시 디렉터리로 바꾸면 `CACHE_DIR`(`~/.cache/gitgraph`)과 `CONFIG_PATH`(`~/.config/gitgraph`)가 모두
따라온다. fixture 캐시 파일(`items__<repo>__open.json`, `stubs__<repo>.json`)을 그 안에 써 두고 `--max-age`를
크게 주면 `load_items()`가 캐시를 그대로 반환하므로 **네트워크·`gh`·AI CLI 없이** 전 파이프라인이 돈다.

```
env -i HOME=$T PATH=$T/bin:/usr/bin:/bin TZ=UTC TERM=xterm-256color \
    GITGRAPH_REPOS=test/repo GITGRAPH_ME=alice GITGRAPH_AUTO_TRANSLATE=false \
    python3 gitgraph.py graph --max-age 100000 -t none --color never
```

이 명령으로 fixture 3건에서 tree 출력이 정상적으로 나왔고, `$T/bin/gh`(호출되면 exit 42)는 한 번도 불리지
않았다. 같은 env로 `tests/tui_smoke.py`도 완주했다.

**절대 규칙**: 테스트는 사용자의 실제 `~/.cache/gitgraph`·`~/.config/gitgraph`·`~/gitgraph-todo.md`를 읽거나
쓰지 않는다. 모든 테스트는 `tests/env.py`가 만든 임시 HOME 안에서만 동작한다.

## Phase 0 — 러너

`tests/run.py` (~80줄)

```
python3 tests/run.py              # py_compile -> unit -> golden -> smoke, 요약 한 줄, 실패 시 exit 1
python3 tests/run.py unit         # 단위 테스트만
python3 tests/run.py smoke        # pty smoke만
python3 tests/run.py -k parse     # 이름으로 고르기
python3 -m unittest tests.test_parse.TestRefs.test_fence   # 단일 테스트 (stdlib unittest)
```

- 단위 계층은 stdlib `unittest`로 쓴다. 의존성 추가 없음(pytest 안 씀 — 무의존성 규칙 유지).
- 첫 단계는 항상 `python3 -m py_compile gitgraph.py` (0.10.3에서 문법 오류가 배포된 적 있음).

## Phase 1 — 고정 fixture + 격리 환경

### `tests/fixtures/repo.json`

fixture repo는 `test/repo` 하나. 회귀 방어에 필요한 표본을 한 파일에 모은다:

- issue / PR / draft / merged / closed, `closes`(PR→issue), crossref만 있는 stub 항목
- CJK·한글 제목, 영문 제목
- 본문: code fence 안의 `#N`, kernel log 줄(`[  123.4]`, `Tainted:`), `overwrite #5`(참조 아님) vs `see PR #5`(참조),
  `owner/repo#N`, 전체 URL, Enterprise 호스트 URL, markdown 표, 아주 긴 문단
- 코멘트: 일반 comment / review / review_comment, `@mention`, 서로 다른 작성자와 날짜(+0d/+1d/+14d)
- "my turn"(남이 마지막 발언) / "waiting"(내가 마지막) / "mentions" / "stale"(30일 이상)이 모두 비지 않도록 배치

날짜는 전부 고정 문자열, `fetched_at`도 고정값. `TZ=UTC` 고정으로 출력 바이트가 매 실행 동일해야 한다.

### `tests/env.py` — 다른 모든 테스트가 쓰는 계약

```python
FIXTURE_REPO = "test/repo"
FIXTURE_ME   = "alice"

def make_home(tmpdir=None) -> str
    """임시 HOME을 만들고 그 안에 fixture 캐시(items__/stubs__)를 쓴 뒤 경로를 돌려준다."""

def child_env(home, **extra) -> dict
    """subprocess/pty용 env: HOME, PATH(앞에 tests/fakes), TZ=UTC, TERM, LANG,
       GITGRAPH_REPOS/ME/AUTO_TRANSLATE(false)/THEME(dark). extra로 덮어쓴다."""

def load_module()
    """HOME을 세팅한 뒤 gitgraph를 import 해서 모듈 객체를 돌려준다(in-process 단위 테스트용).
       gitgraph는 import 시점에 CONFIG/CACHE_DIR/ME/THEME를 읽으므로 import 전에 반드시 HOME을 잡아야 한다."""

def fixture_graph(gg, **opts)
    """fixture로 build_graph()를 돌려 Graph를 돌려준다(단위 테스트 공용). max_age는 무한대."""
```

`tests/fakes/gh`는 기본적으로 **호출되면 즉시 실패**한다(네트워크를 타면 hang 대신 FAIL).

## Phase 2 — 순수 계층 단위 테스트 (~700줄)

| 파일 | 대상 |
|---|---|
| `test_width.py` | `dw/trunc/clip/slice_cols/split_width/wrap/char_at` — CJK·조합문자, 불변식 `dw(clip(s,a,b)) <= b` |
| `test_parse.py` | `parse_refs_ctx/_plausible_small_ref/snippet/parse_mentions` — fence·kernel log·작은 번호·URL·Enterprise |
| `test_repo.py` | `split_repo/qualify/repo_host/make_repo`, `_REMOTE_RE`(ssh·https·alias), `unfork` |
| `test_graph.py` | `build_graph` node/edge 집합, closes가 ref를 가리는 규칙, crossref 중복 방지, stub 채우기, `apply_filters/components/focus/subgraph` |
| `test_rows.py` | `tree_rows/log_rows/render_show` golden + `segments()` 스타일(표시 계약) |
| `test_markdown.py` | `render_table`(CJK 정렬)·`render_markdown`·`md_segments` |
| `test_panel.py` | `Panel.move/settle/find/goto_nid/set_rows` — 무효 행 건너뛰기, viewport 클램프, nid로 커서 유지 |
| `test_ime.py` | `hangul_keys` 2벌식 전 키 + 조합 음절 |
| `test_todo_qa.py` | `todo.json` → `render_todo_md` → `todo_find/todo_finish`, `qa.json` 앵커 |
| `test_cache.py` | `cache_kind/cache_files/cache_hygiene` — 30일 삭제, AI 캐시·로그 상한, 0700/0600 |
| `test_config.py` | `cfg` 우선순위 CLI > env > 파일 > 기본값 (subprocess로 `gg config`) |

## Phase 3 — fake CLI (~200줄)

- `tests/fakes/claude` — 프롬프트를 읽어 **길이가 맞는 JSON 배열**을 돌려주는 결정론 backend.
  basename이 `claude`라 `IS_CLAUDE` 경로를 그대로 탄다(`-p --output-format json`). 환경변수로:
  `FAKE_AI_FAIL=1`(실패 → `AI_FAILURES` → TUI 전환 팝업), `FAKE_AI_SLEEP=N`(지연 → `» 요약 중…`, `⟳ 번역 중…` 표시),
  `FAKE_AI_LOG=path`(받은 프롬프트 기록 → 배치 크기·컨텍스트 검증).
- `tests/fakes/gh` — `auth status` / `auth token` / `api graphql`을 fixture로 응답.
  계정 A는 `NOT_FOUND`, 계정 B는 성공 → multi-account fallback 검증. transient 오류 N회 → retry 검증.
- `test_ai.py`: 배치 분할, 응답 길이 불일치 처리, `cache_merge` 동시 기록(스레드), `USAGE` 집계.
- `test_fetch.py`: `load_items` 신선도 분기, `refresh_items` 증분 경로, `GhError` 메시지.

## Phase 4 — smoke test 확장

`tests/tui_smoke.py`를 fixture HOME 위에서 돌게 바꾼다 → **실제 로그인·실제 repo 의존 제거**.

추가 체크: 패널별 Enter, Inbox 10개 탭 순회, `b`/`f` 히스토리, `/`+`n`/`N`, `m` 추가/수정/완료, `y` 복사,
`T` 테마, screen mode 3종, 84↔83열 리사이즈(portrait), `--theme basic`, 마우스(클릭·드래그·휠·더블클릭),
fake AI로 `i` 번역 토글, 팝업별 Esc, `state.json` 내용과 `cmd.json` 왕복(`gg_open`).

화면 스냅샷은 `tests/golden/*.txt`에 저장하고 `GG_UPDATE_GOLDEN=1`로 갱신한다. `tui.log`에 `Traceback`과
오류 줄이 없어야 통과.

## Phase 5 — 과거 버그를 케이스로 고정

| 커밋 | 회귀 테스트 |
|---|---|
| 0.10.3 들여쓰기 오류 배포 | 러너 첫 단계 `py_compile` + import |
| 0.12.1 IME 커밋 키(ㅁ⏎) | `test_ime` + smoke 키 시퀀스 |
| 0.11.2 border drag 범위 | main 가장자리에서 시작한 드래그는 리사이즈가 아니라 텍스트 선택 |
| 0.9.3 URL 클릭 범위 | `click_main` 좌표(보이는 글자 위 / 뒤 빈칸 / 잘린 부분) |
| 0.10.1 URL 앞 들여쓰기 밑줄 | `segments()` golden |
| 0.6.2 ask 크래시(answer 탭 인덱스) | smoke `a` 흐름 |
| 0.17.0 병렬 AI + 잠금 기록 | `cache_merge` 스레드 동시성 |

## 규모

테스트 코드 약 1,500줄. `gitgraph.py` 변경은 없거나 최소로 하고, 테스트를 위해 seam이 꼭 필요하면 그 사실을
따로 보고한다. 완료 후 `CLAUDE.md`의 Commands 절을 새 러너에 맞춰 고친다.


## 발견한 회귀 (테스트로 고정, 코드는 미수정)

테스트를 쓰면서 실제 코드의 문제 4건이 드러났다. 전부 **현재 동작을 그대로 고정**해 뒀고, 고치면 해당
`test_suspected_bug_*`가 실패하도록 이유를 주석에 적어 두었다.

| 위치 | 증상 | 고정한 테스트 |
|---|---|---|
| `gitgraph.py:1285` | 0.17.0에서 `WHY_PROMPT` 정의가 사라지고 사용처만 남았다. `summarize_whys()`가 항상 `NameError`로 실패하고 `except`가 삼켜서 **Links의 `↳` AI 링크 이유가 항상 꺼져 있다**. 0.16.0에는 정의가 있었다(그 판의 1217줄) | `test_ai.TestLinkReasons.test_suspected_bug_why_prompt_is_undefined_in_0_17_0` |
| `gitgraph.py:1710` | `trunc()`이 `dw()`가 아니라 `len()`으로 자른다 — CJK 제목이 열 예산을 넘는다 | `test_width.TestTrunc.test_width_can_exceed_n_for_cjk` |
| `gitgraph.py:443,460` | `graphql()`이 성공한 계정을 앞으로 옮기지만 `gh_accounts()`가 복사본을 주므로 반영되지 않는다 — private repo에서 매 쿼리마다 거부되는 계정을 먼저 시도한다 | `test_fetch.TestGraphqlFallback.test_suspected_bug_the_working_account_is_not_remembered` |
| `gitgraph.py:336` | `unfork()`이 같은 upstream을 가리키는 두 번째 fork를 fork 이름 그대로 남긴다 | `test_repo.TestUnfork.test_suspected_bug_second_fork_of_an_already_seen_parent_survives_under_its_own_name` |
