# gg에 PR 리뷰 모드를 넣는 계획

상태: 1~4단계 구현됨 — worktree·3열 diff·내장 프로토콜 pass 1·검증 pass 2·주관적 억제까지. 5~7단계(게시, 증분 재리뷰, review_cmd)가 남았다.

결정된 것(인터뷰):

- 코드 출처 — **로컬 체크아웃 필수**. clone이 없으면 리뷰 모드에 들어가지 않는다.
- 리뷰 엔진 — **gg가 리뷰 프로토콜을 내장한다.** `review_cmd`는 도메인 전용 스킬로 갈아끼우는 탈출구.
- 레이아웃 — **Files / Diff / Findings 3열**.
- findings 보관 — **head SHA 기준 캐시 + 게시 이력**.
- 진입 — **TUI `v` 키 + `gg review <PR>` CLI**.
- 결과 처리 — **PR에 인라인 코멘트 게시**.

---

## 1. 목표 화면

```
┌─1 Files ───────────┐┌─2 Diff  fs/f2fs/data.c ────────────┐┌─3 Findings [open 3] ─┐
│ #123 zone alloc    ││ @@ -220,7 +220,9 @@ f2fs_write_page ││ 🔴 bug               │
│ open · abc1234     ││    220     if (!page)              ││▸#1 ✓ lock leak       │
│ 3 files  +42 -2    ││    221 -       return -ENOMEM;     ││   data.c:222         │
│                    ││ ⚠  222 +       goto out_unlock;    ││ 🟡 logic             │
│▸data.c     +9  -2 ⚠││    223                             ││ #2 ? off-by-one      │
│ gc.c      +31  -0 ℹ││    224     out_unlock:             ││   gc.c:88            │
│ f2fs.h     +2  -0  ││    225         spin_unlock(&sb->l) ││ 🔵 style  (보류)     │
│                    ││                                    ││ #3 · naming          │
│ worktree 1.4G      ││                                    ││   f2fs.h:12          │
└────────────────────┘└────────────────────────────────────┘└──────────────────────┘
 1-3 panel  ⏎ 이동  space 선택  x 무시  P 게시  R 재리뷰  v 브라우저  ? keys  q quit
```

- 왼쪽 Files(`review_files_width`, 기본 0.22): PR 헤더 3줄 + 변경 파일 목록. 파일마다 `+A -D`와 그 파일에 걸린 최고 severity 기호.
- 가운데 Diff(나머지): 선택 파일의 unified diff. 커서가 줄 단위로 선다. findings가 걸린 줄은 gutter에 `⚠`.
- 오른쪽 Findings(`review_findings_width`, 기본 0.30): severity 그룹 + 지적 목록. 지적 앞의 `✓ ? ·`는 검증 판정(3절)이다. 탭은 `open / posted / ignored / dropped / changes / github`.

### 좁은 화면 규칙

`layout()`의 리뷰 분기는 이 순서로 판단한다. 브라우저 모드와 달리 3열은 폭을 많이 먹으므로 수치를 못박는다.

```
H = h - 1                                  # 맨 아래 한 줄
screen == "full"   → 포커스 패널이 화면 전체
screen == "half"   → 2열: 포커스된 목록 패널(없으면 Files) | Diff
                     side_w = clamp(int(w * review_files_width), 24, w - 40)
screen == "normal":
  files_w = clamp(int(w * review_files_width),   22, 40)
  find_w  = clamp(int(w * review_findings_width), 26, 52)
  diff_w  = w - files_w - find_w

  w <= 84                  → 세로 stack(portrait)
                             Files  h = max(5, H // 5)
                             Diff   h = 나머지
                             Findings h = max(6, H // 4)
                             expand_focused면 포커스 패널이 H // 6을 더 가져간다
  diff_w >= 56             → 3열 그대로
  w >= 100                 → find_w = 26으로 다시 계산. diff_w >= 56이면 3열
  그 외                     → 2열 + 스트립: Files(files_w) | Diff(나머지),
                             Findings는 Diff 아래 가로 스트립 h = max(6, H // 4)
  H < 16                   → Findings 스트립을 제목줄만 남기고 접는다(Diff가 그 높이를 가져감)
```

`place()`가 이미 `hh < 3` 또는 `ww < 4`인 패널을 거부하므로 극단적으로 작은 터미널에서도 죽지 않는다.

### 브라우저 모드와의 관계

`Tui.mode` = `browse` | `review` 하나만 늘린다. 패널 집합·키 테이블·`layout()` 분기가 mode에 따라 갈리고, 나머지(`Panel`, `draw_box`, `segments`, `run_bg`, `enrich`, 팝업, 마우스, 테마)는 전부 공유한다.

```python
MODES = {"browse": ["repo", "item", "home", "comments", "links", "people"],
         "review": ["rfiles", "rdiff", "rfind"]}
```

`self.SIDE` 대신 `self.side_keys()`를 쓰고, mode를 바꿀 때 직전 mode의 `focus`를 기억해 되돌린다.

---

## 2. 리뷰 프로토콜을 어디까지 내재화하는가

`~/.claude/review-prompts/`의 커널 세트는 30개 파일 9,226줄이고 `gitgraph.py`는 5,849줄이다. 통째로 넣을 수 없고, 넣어서도 안 된다 — 그 안의 8할은 lore·Fixes: tag·Kconfig·subsystem guide·`review-inline.txt` 메일 형식처럼 커널에만 쓸모 있는 지식이다.

가져오는 것은 **지식이 아니라 규율**이다. kreview가 좋은 이유는 커널을 알아서가 아니라 절차가 엄해서다.

### 가져오는 것 (gg 안으로)

| 출처 | 규율 | gg에서의 형태 |
|---|---|---|
| review-core.md TASK 1 | "diff 조각으로 판단하지 말고 함수 전체를 먼저 찾아라" | 프롬프트 규칙 + **worktree 필수**라는 설계 근거 |
| review-core.md TASK 1B | 변경을 CHANGE-N으로 잘게 분류(루프마다, 반환값마다, 자원·초기화·락마다)한 뒤 분류별로 분석 | pass 1a 산출물 `changes[]`, Findings의 `changes` 탭 |
| review-core.md TASK 2.0 | **Reachability gate** — 바뀐 경로가 커밋이 말하는 용례로 실제 실행되는가. 아니면 그 자체가 최우선 결함 | 프롬프트의 첫 필수 단계, `severity: reach` |
| review-core.md 분석 철학 | "저자가 틀렸다고 가정하고 옳다는 증거를 요구한다" | 프롬프트 문구 그대로 |
| false-positive-guide.md | **거짓 양성 제거를 별도의 강제 단계로**. 방어적 코딩 요구·이론적 API 오용·락 오판·UAF 혼동·주석 근거 기각 금지 | **pass 2 검증 호출**(코드로 강제) + `VERIFY_PROMPT` |
| slop-indicators.md | 주관적 지적은 correctness와 분리, 최대 3개, **확정 결함이 있으면 통째로 억제** | `review_subjective` 규칙(코드로 강제) |
| report.md / review-metadata.json | 고정 필드 JSON, "다른 필드를 만들지 말 것" | `REVIEW_CONTRACT` |
| sashiko review-pr | 🔴🟠🟡🔵🟢 severity 5분류, 지적마다 적용 가능한 unified diff, 프로젝트 표준 문서 자동 탐지 | `severity` 값, `Finding.diff`, worktree의 `CLAUDE.md`/`AGENTS.md`/`GEMINI.md`/`CONTRIBUTING.md` 읽기 |

### 가져오지 않는 것 (밖에 둔다)

lore/메일링리스트 스레드, Fixes: tag 판정, Kconfig 검사, subsystem guide 표, semcode MCP 사용법, `review-inline.txt`의 커널 메일 형식, 봇 피드백 격리 규칙. 전부 커널 전용이고 gg가 흉내 내면 다른 저장소에서 소음이 된다.

이것들이 필요하면 `review_cmd`로 `/kreview`를 가리키면 된다. **`claude -p`는 사용자의 MCP 서버와 설정을 그대로 물려받으므로**(`--bare`를 주지 않는 한) 커널 트리에서는 semcode까지 그대로 붙는다.

### 결과: 프롬프트 2개, 약 180줄

`gitgraph.py`에 `REVIEW_PROMPT`(~130줄)와 `VERIFY_PROMPT`(~50줄)를 각자의 호출부 옆에 둔다. 파일의 3%다. `TR_PROMPT`/`SUM_PROMPT`/`ASK_PROMPT`가 이미 그렇게 있다.

---

## 3. 리뷰 엔진

### 2-pass 파이프라인 (코드로 강제)

```
pass 1a  파악    변경을 CHANGE-N으로 분류 + reachability 판정      → changes[]
pass 1b  탐색    분류별로 결함 후보를 찾는다 (증거 필드 필수)       → candidates[]
pass 2   검증    후보마다 별도 호출로 반증을 시도한다              → verdict
         └ CONFIRMED  증거가 코드 경로로 확인됨      ✓  게시 후보로 미리 체크됨
         └ PLAUSIBLE  그럴듯하나 확정 못 함          ?  체크하려면 space를 눌러야 함
         └ FALSE      반증됨                        ·  dropped 탭으로, 캐시에 남아 재발견 안 됨
```

pass 1a·1b는 한 번의 호출로 묶는다(모델이 분류한 직후 그 맥락에서 찾는 게 낫다). pass 2는 후보마다 한 번씩, `ai_parallel`만큼 병렬. 후보 8개면 총 9회 호출이다.

**이것이 kreview에서 가져오는 것 중 가장 값이 큰 하나다.** false-positive-guide.md는 "같은 세션에서 스스로를 검토하라"고 시키지만, 별도 호출로 쪼개면 앞선 추론에 끌려가지 않는다. `review_verify` 기본 `on`, 끄면 pass 1 결과가 전부 `PLAUSIBLE`로 남는다.

주관적 지적(`style`, `design`)은 slop-indicators.md 규율을 코드로 옮긴다: 최대 3개, 그리고 `review_subjective = auto`(기본)에서는 **같은 PR에 CONFIRMED된 `bug`/`regress`/`reach`가 하나라도 있으면 통째로 감춘다**(Findings 헤더에 `🔵 style (보류 2건)`).

### 설정

| 키 | 기본값 | 뜻 |
|---|---|---|
| `review_cmd` | (빈 값 = 내장 프로토콜) | 도메인 스킬로 갈아끼우기. `repo=cmd` 쌍을 콤마로 나열하면 저장소별 지정(`torvalds/linux=/kreview`), 맨 값 하나면 전역 |
| `review_model` | `sonnet` | pass 1 모델(claude 전용) |
| `review_verify` | `on` | pass 2 검증 호출 |
| `review_verify_model` | `sonnet` | pass 2 모델 |
| `review_subjective` | `auto` | `auto`(확정 결함 있으면 억제) / `always` / `never` |
| `review_timeout` | `900` | 한 번의 호출 상한(초) |
| `review_max_bytes` | `400000` | diff가 이보다 크면 파일 단위로 쪼개 `ai_parallel`만큼 병렬 호출 후 병합 |
| `review_files_width` | `0.22` | Files 열 폭 비율 |
| `review_findings_width` | `0.30` | Findings 열 폭 비율 |
| `worktree_keep_days` | `7` | 이 기간 손대지 않은 리뷰 worktree 삭제 |
| `worktree_max` | `5` | 남겨두는 worktree 개수 상한(오래된 것부터) |
| `review_signature` | (빈 값) | 게시 코멘트 꼬리말. 기본은 서명 없음 |

### 호출 형태

claude 백엔드:

```
cwd = <worktree>
claude -p --output-format json --model <review_model>
       --allowedTools Read Grep Glob "Bash(git *)"
       "<REVIEW_PROMPT 또는 review_cmd> \n\n<REVIEW_CONTRACT>"
```

`_ai_call(prompt, model, phase, timeout)`에 `cwd=None, extra_args=()`를 더한다. 이 두 인자가 이번 기능이 AI 계층에 넣는 변경의 전부다.

`review_cmd`가 슬래시 커맨드일 때도 `REVIEW_CONTRACT`는 언제나 뒤에 붙는다 — 그래야 `/kreview`든 sashiko `/review-pr`이든 같은 패널에 그려진다. 스킬이 없는 백엔드(codex/gemini/grok)는 `review_cmd`를 무시하고 항상 `REVIEW_PROMPT`를 쓴다. codex는 `exec -s read-only`를 worktree에서 돌려 파일을 읽고, gemini/grok은 diff를 프롬프트에 싣는다.

**AI CLI가 없으면** 리뷰 모드는 그대로 열리고 Findings만 `AI CLI 없음 — diff만 봅니다`가 된다. worktree·diff·기존 GitHub 리뷰 스레드 열람은 전부 동작한다.

### 출력 계약

pass 1이 낼 것:

```
<<<GG_REVIEW
{"reachability": {"verdict":"confirmed|blocked","reason":"…"},
 "changes":[{"cid":"CHANGE-1","kind":"control-flow|return|resource|init|locking|api|data|build|doc",
             "path":"fs/f2fs/data.c","symbol":"f2fs_write_page","summary":"한 줄"}],
 "findings":[{"cid":"CHANGE-1","severity":"reach|bug|regress|logic|style|design",
              "path":"fs/f2fs/data.c","line":222,"end_line":222,"side":"RIGHT",
              "title":"70자 이내 한 줄",
              "body":"무엇이 틀렸고 어떻게 고치는지 1-4문장",
              "evidence":"이것이 참임을 보이는 구체 경로. 파일:줄로 지목할 것",
              "diff":"선택. path에 적용되는 unified diff"}]}
GG_REVIEW>>>
```

pass 2가 후보 하나마다 낼 것:

```
<<<GG_VERDICT
{"verdict":"CONFIRMED|PLAUSIBLE|FALSE","reason":"한두 문장","line":222}
GG_VERDICT>>>
```

계약 문구에 report.md의 태도를 그대로 옮긴다 — *"이 필드 말고 다른 필드를 만들지 말 것"*, *"칭찬 항목·요약 항목 금지"*, *"이 diff에 없는 path를 지어내지 말 것"*, *"`evidence` 없이 findings를 내지 말 것"*.

파싱 실패 시: 마커가 없으면 마지막 `{...}` 블록을 한 번 더 시도하고, 그것도 실패하면 Findings가 원문을 그대로 보여주며 `파싱 실패 — 원문` 헤더를 단다(`AI_FAILURES`에는 넣지 않는다. CLI가 죽은 게 아니라 형식이 어긋난 것이다).

### findings의 언어 — 원문이 정본, 번역은 보기용

`lang`은 Korean인데 이 글은 GitHub에 나간다. 저장소의 언어를 gg는 알 수 없고, 영어 저장소에 한국어 인라인 코멘트가 나가면 되돌리기 어렵다. 그래서:

- `REVIEW_CONTRACT`가 `title`과 `body`를 **영어로 쓰라고 못박는다.** 모델이 낸 원문이 정본이다.
- Findings 패널에서 `i`를 누르면 선택한 지적의 `title`/`body`를 `lang`으로 번역해 **패널에만** 보여준다. 기존 `TR_FULL_PROMPT` + `translations_full.json`을 그대로 쓴다(캐시 열쇠가 텍스트 해시라 그대로 맞는다).
- **게시(`P`)는 언제나 원문을 보낸다.** 확인 팝업도 원문을 보여준다 — 번역본을 읽고 승인했는데 다른 글이 나가는 일은 없어야 한다.
- 이 규칙은 `review_cmd`로 남의 스킬을 부를 때도 계약이 뒤에 붙으므로 그대로 유지된다.

### `gg mcp`에 리뷰 모드 노출

지금 `state_snapshot()`은 `focus`·`item`·`subject`만 쓴다. 리뷰 모드의 focus는 `rfiles`/`rdiff`/`rfind`이고 `subject`는 그래프 노드라, 손대지 않으면 리뷰 중에 `gg_state`가 직전에 보던 이슈를 보고한다.

- 스냅샷에 `"mode": "browse" | "review"`를 넣고, `review` 모드일 때 키 하나를 더 쓴다:
  ```json
  {"repo":"…","number":123,"url":"…","head_oid":"abc1234","status":"done",
   "file":"fs/f2fs/data.c",
   "counts":{"open":3,"confirmed":1,"plausible":2,"posted":0,"ignored":0,"dropped":1},
   "finding":{"fid":"…","severity":"bug","verdict":"CONFIRMED",
              "path":"fs/f2fs/data.c","line":222,"title":"…","body":"…"}}
  ```
- `item`은 그 PR의 노드를 그대로 유지한다 — 기존 MCP 소비자가 깨지지 않는다. `cursor_row`는 이미 일반적이라 그대로 쓴다.
- `gg_context`가 `finding:<fid>`와 `file:<path>`를 id로 받아 지적 전문 + 그 hunk를 돌려준다.
- `poll_cmd()`의 `{"op":"open","id":"finding:<fid>"}`로 밖에서 특정 지적을 띄울 수 있게 한다.
- `MCP_TOOLS`의 `gg_state`/`gg_context` 설명에 리뷰 모드 한 줄을 더한다.

### 앵커 검증 (게시 전 필수)

GitHub은 diff에 없는 줄의 인라인 코멘트를 거부한다. 파싱 직후:

1. `path`가 이 PR의 파일 목록에 없다 → `anchor = unanchored`. 보여는 주되 게시 대상에서 뺀다.
2. `line`이 그 파일의 어떤 hunk에도 `side`쪽으로 안 걸린다 → 같은 파일의 가장 가까운 변경 줄로 당기고 `anchor = moved`(`⚠ 위치 보정`). 변경 줄이 없으면 `unanchored`.
3. `anchor ∈ {ok, moved}`이고 `verdict ≠ FALSE`인 것만 게시 가능.

---

## 4. 데이터 모델

그래프(item/comment/person)와 별개의 자족적 모델. `Graph`에 섞지 않는다 — 리뷰는 노드도 엣지도 아니다.

```python
class Hunk:      header, old_start, old_lines, new_start, new_lines, lines
                 # lines: [(tag, old_no, new_no, text)]  tag: " " | "+" | "-"
class DiffFile:  path, old_path, status, additions, deletions, hunks, binary
                 # status: added | modified | deleted | renamed
class Change:    cid, kind, path, symbol, summary
class Finding:   fid, cid, severity, path, line, end_line, side, title, body,
                 evidence, diff, verdict, verdict_reason, anchor,
                 state, posted_at, thread_url, digest
                 # severity: reach | bug | regress | logic | style | design
                 # verdict:  CONFIRMED | PLAUSIBLE | FALSE | (검증 끔: None)
                 # anchor:   ok | moved | unanchored
                 # state:    new | posted | ignored | stale
class Review:    repo, number, title, head_oid, base_oid, merge_base, head_ref, base_ref,
                 worktree, files, changes, findings, reachability, threads,
                 engine, model, status, t0, t1, error
                 # status: idle | worktree | running | verifying | done | failed
```

`digest = sha1(path + "\n" + normalized_title)[:12]` — head가 바뀐 뒤 같은 지적을 다시 올리지 않기 위한 열쇠.

---

## 5. 로컬 worktree

```
~/.cache/gitgraph/worktrees/<host>__<owner>__<name>/pr-<N>/
```

1. **clone 찾기.** `discover_repos()`가 이미 `(repo, dir)`를 돌려주는데 `resolve_repos()`가 dir를 버린다. 전역 `CHECKOUTS: {repo: dir}`를 두고 `github_remotes()`에서 채운다. fork는 `unfork()`가 부모로 바꾸므로 **fork의 dir를 부모 repo id에도 같이 등록**한다(PR은 부모에 있고 객체는 fork clone에 있다).
2. **fetch.** target repo를 가리키는 remote가 있으면 그 이름으로, 없으면 `https://<host>/<owner>/<name>.git`을 직접 준다(`gh auth setup-git`이 돼 있으면 credential helper가 처리).
   ```
   git -C <clone> fetch --no-tags <remote|url> \
       +refs/pull/<N>/head:refs/gg/pr-<N> \
       +refs/heads/<baseRef>:refs/gg/pr-<N>-base
   merge_base = git -C <clone> merge-base refs/gg/pr-<N>-base refs/gg/pr-<N>
   ```
3. **worktree.** `git -C <clone> worktree add --detach <WT>/pr-<N> refs/gg/pr-<N>`
4. **diff.** `git -C <clone> diff --no-color -U5 --find-renames <merge_base> refs/gg/pr-<N>` → `parse_unified_diff()`.
   API가 아니라 git이 diff를 만든다. `pulls/N/files`의 파일 수·patch 절단이 없고 컨텍스트 폭(`-U5`)도 우리가 정한다.
5. **표준 문서.** worktree 루트의 `CLAUDE.md` / `AGENTS.md` / `GEMINI.md` / `CONTRIBUTING.md` 중 있는 것의 경로를 프롬프트에 알린다(내용은 모델이 읽는다). sashiko 스킬의 첫 단계를 일반화한 것.
6. **재사용.** 같은 head_oid면 그대로. head가 움직였으면 지우고 다시.

clone을 못 찾으면 리뷰 모드에 들어가지 않고 이유를 말한다:
`gg: <repo>의 로컬 clone을 <cwd> 아래에서 못 찾았습니다 — 그 디렉터리에서 gg를 켜거나 clone 하세요.`

**문서에 남길 주의 둘.** `git worktree add`는 사용자 clone의 `.git/worktrees/`에 메타데이터를 쓴다(작업 트리는 안 건드린다). 정리는 `git worktree remove --force` 후 `git worktree prune`이며 `rm -rf`만 하면 stale 메타가 남는다. 커널처럼 큰 트리는 worktree 하나가 1.5G쯤이라 `worktree_max` 기본이 5고 Files 헤더에 실제 용량을 적는다.

---

## 6. 캐시

`~/.cache/gitgraph/reviews__<host>__<owner>__<name>.json`

```json
{"123": {
  "reviews": {"abc1234…": {"base_oid":"…","merge_base":"…",
                           "engine":"builtin|claude:/kreview","model":"sonnet",
                           "verify":true,"created":1756000000,
                           "reachability":{"verdict":"confirmed","reason":"…"},
                           "files":[{"path":"fs/f2fs/data.c","additions":9,"deletions":2}],
                           "changes":[{"cid":"CHANGE-1","kind":"locking","...":"..."}],
                           "findings":[{"fid":"…","digest":"…","verdict":"CONFIRMED","...":"..."}]}},
  "posted":  {"<digest>": {"head_oid":"abc1234…","at":1756000001,"thread_url":"https://…"}},
  "ignored": {"<digest>": {"at":1756000002}},
  "dropped": {"<digest>": {"at":1756000003,"reason":"caller holds i_lock at data.c:198"}}}}
```

- `reviews`는 head_oid별. 같은 head면 LLM을 다시 부르지 않는다(`R`이나 `--refresh`로만 강제).
- `posted`/`ignored`/`dropped`는 head를 넘어 산다. 새 head에서 같은 `digest`가 나오면 그 상태를 물려받는다 — 중복 게시도, 이미 반증된 지적의 재발견도 막는다. **이것이 kreview의 "거짓 양성을 두 번 보고하지 않는다"를 저장소 수준으로 옮긴 것.**
- **증분 재리뷰**: head가 바뀌면 `git diff <old_head> <new_head> --name-only` ∩ PR 파일에 대해서만 다시 부르고, 손대지 않은 파일의 findings는 verdict째로 승계한다. 승계분 중 줄이 옮겨간 것은 앵커 재검증 후 `moved`.

`CACHE_KINDS`에 두 줄 추가:

```python
"reviews__": ("review", "PR별 AI 리뷰 findings와 판정·게시·무시·기각 이력"),
"worktrees": ("review", "리뷰용으로 펼친 PR head의 git worktree"),
```

`cache_files()`는 지금 파일만 훑으므로 디렉터리(worktrees) 크기 계산과 `gg cache clear review`가 `git worktree remove`를 부르도록 확장한다. `cache_hygiene()`에 `worktree_keep_days` / `worktree_max` 정리를 넣는다.

---

## 7. GitHub 왕복

새 GraphQL은 둘뿐이고 기존 `graphql()`을 그대로 탄다(계정 fallback·재시도·NOT_FOUND 처리 공짜).

**읽기** — PR 메타 + 기존 리뷰 스레드:

```graphql
repository(owner:$owner,name:$name){ pullRequest(number:$number){
  number title state isDraft headRefName baseRefName headRefOid baseRefOid
  headRepository{ nameWithOwner }
  reviewThreads(first:100){ nodes{ id isResolved isOutdated path line diffSide
    comments(first:10){ nodes{ author{login} body createdAt url } } } } } }
```

**쓰기** — 고른 findings를 한 번의 mutation, 하나의 review로:

```graphql
mutation($pr:ID!,$body:String,$threads:[DraftPullRequestReviewThread!]){
  addPullRequestReview(input:{pullRequestId:$pr, event:COMMENT, body:$body, threads:$threads}){
    pullRequestReview{ url } } }
```

`threads` 원소는 `{path, line, side, body}` (스키마 확인함: `path line side startLine startSide body`). 알림이 한 번만 가고 REST 배관을 새로 만들 필요도 없다.

코멘트 본문:

```
<title>

<body>

```suggestion 또는 diff 블록(있을 때만)```
```

`review_signature`가 비어 있으면 꼬리말을 붙이지 않는다 — 사용자 계정으로 나가는 글에 도구 서명을 넣지 않는 기존 규칙 그대로다. 대신 **게시 전 확인 팝업이 나갈 글 전문을 그대로 보여준다.**

---

## 8. Row / 스타일

새 렌더러는 전부 `Row(text, nid, jump, kind)`를 낸다.

| 렌더러 | nid | kind |
|---|---|---|
| `review_files_rows(rv)` | `file:<path>` | `""`, `head` |
| `diff_rows(rv, path, width, collapsed)` | `line:<path>:<side>:<no>` | `diff_file` `diff_hunk` `diff_add` `diff_del` `diff_ctx` |
| `findings_rows(rv, tab, width)` | `finding:<fid>` | `""`, `sec` |
| `changes_rows(rv, width)` | `change:<cid>` | `""`, `sec` |

`segments()`는 **노드 조회보다 먼저** 이 kind들을 처리하고 early-return 한다(리뷰 nid는 `g.nodes`에 없다). `LIST_KINDS`에 `diff_add/diff_del/diff_ctx/diff_hunk`를 더해 Diff 패널에도 커서가 선다.

`THEMES` 세 벌에 스타일 11개 추가 — `diff_add` `diff_del` `diff_hunk` `diff_file` `diff_ctx`, `sev_reach` `sev_bug` `sev_regress` `sev_logic` `sev_style` `sev_design`. basic(8색)에서는 `diff_add`=2, `diff_del`=1, `sev_*`는 1/1/3/3/6/2로 접는다. 폭 계산은 전부 `dw()/trunc()/slice_cols()` — diff 안의 CJK 주석과 탭(4칸 전개) 때문에 특히 필요하다.

---

## 9. 키

| 키 | 자리 | 동작 |
|---|---|---|
| `v` | 브라우저: PR 위 | 리뷰 모드로. 리뷰 모드에서는 브라우저로 복귀 |
| `1` `2` `3` | 리뷰 | Files / Diff / Findings |
| `⏎` | Files | 그 파일 diff를 Diff에 띄우고 포커스 이동 |
| `⏎` | Findings | Diff를 그 줄로 점프 |
| `⏎` | Diff | hunk 접기/펴기 |
| `space` | Findings | 게시 후보 체크/해제 (CONFIRMED는 처음부터 체크됨) |
| `x` | Findings | 무시 토글(`ignored`) |
| `P` | Findings | 게시 — 체크된 것 전부, 없으면 커서 하나. 확인 팝업 필수 |
| `V` | Findings | 이 지적만 다시 검증(pass 2 재실행) |
| `R` | 리뷰 | 재리뷰(확인 후 캐시 무시) |
| `r` | 리뷰 | PR 메타 새로 읽고 head가 바뀌었으면 증분 재리뷰 |
| `i` | Findings | 선택한 지적의 title/body를 `lang`으로 번역해 패널에만 표시(게시는 늘 원문) |
| `y` | Findings | 지적을 markdown으로 클립보드에 |
| `a` | 리뷰 | 선택한 finding/hunk를 놓고 질문(기존 `ask()` 재사용) |
| `o` | 리뷰 | 브라우저로 PR 또는 그 파일·줄 열기 |
| `[` `]` | Findings | `open / posted / ignored / dropped / changes / github` |
| `+` `_` `Tab` `,` `.` `<` `>` `H` `L` `/` `n` `N` `T` `?` `q` | 리뷰 | 브라우저 모드와 같음 |

`HINTS`에 세 패널 항목을 더하고 `key_menu()`(`?`)는 mode에 따라 다른 표를 낸다.

---

## 10. CLI

```
gg review <PR>                 리뷰 모드로 TUI 시작
gg review <PR> --print         리뷰 후 findings를 ANSI로 출력(TUI 없음)
gg review <PR> --json          findings를 JSON으로 stdout에
gg review <PR> --no-ai         worktree + diff만
gg review <PR> --no-verify     pass 2를 건너뛴다
gg review <PR> --refresh       이 head의 캐시를 무시하고 다시 리뷰
gg review <PR> --post [--yes]  게시(기본은 터미널에서 확인, --yes면 생략)
gg review <PR> --post --dry-run  보낼 mutation 페이로드만 출력하고 끝
```

`<PR>`은 `123`, `#123`, `owner/name#123`, PR URL을 받는다. `--print`는 `ansi_rows()`를 그대로 쓰므로 CLI/TUI 두 프론트엔드 규칙이 유지된다.

---

## 11. 구현 단계

각 단계가 그 자체로 동작하고 테스트된다.

1. **diff 파이프라인 (AI 없음)** — `CHECKOUTS` 기록, `review_worktree()`, `parse_unified_diff()`, `Review`/`DiffFile`/`Hunk`, `reviews__` 캐시 골격, PR 메타 GraphQL. 검증: `gg review N --no-ai --print`.
2. **리뷰 모드 레이아웃** — `Tui.mode`, `layout()` 리뷰 분기(1절의 좁은 화면 수치 그대로), 패널 3개, Row kind·스타일·`segments()` 분기, 키 테이블, `state_snapshot()`에 `mode`/`review` 추가. 이 시점에서 AI 없이 완결된 diff 뷰어.
3. **pass 1 (내장 프로토콜)** — `REVIEW_PROMPT` + `REVIEW_CONTRACT`, `_ai_call(cwd=, extra_args=)`, 파싱 + 앵커 검증, `changes`/`reachability` 저장, `run_bg()` 백그라운드와 진행 표시, `review_max_bytes` 초과 시 분할 병렬.
4. **pass 2 (검증) + 주관적 억제** — `VERIFY_PROMPT`, 후보별 병렬 검증, `verdict` 렌더링, `dropped` 캐시, `review_subjective=auto` 억제 규칙, `V` 키, findings 번역(`i`), `gg_context`의 `finding:`/`file:` id.
5. **게시** — `addPullRequestReview`, 확인 팝업(전문 표시), `digest` 중복 방지, `posted` 기록, 실패 메시지, `--dry-run`.
6. **증분 재리뷰 + 정리** — head 변경 시 변경 파일만, 상태·verdict 승계와 앵커 재검증, worktree hygiene, `gg cache`의 디렉터리 처리.
7. **`review_cmd` 오버라이드** — 저장소별 매핑 파서, 슬래시 커맨드 미설치 시 내장으로 1회 폴백 + 안내.
8. **문서** — 1·2단계 몫은 0.22.0에 이미 반영됨(리뷰 모드 절, 키 표, worktree 용량, `?` 메뉴, `HINTS`). 남은 것: 설정 키 나머지와 게시가 사용자 계정으로 서명 없이 나간다는 사실, `VERSION`/`pyproject.toml` 0.23.0.

3·4단계는 프롬프트 작업이라 실측이 필요하다 — 실제 PR로 돌려 보고 `REVIEW_PROMPT`를 조인다.

3단계 실측(claude sonnet, 1회 호출):

| PR | 규모 | 시간 | reachability | CHANGE | findings |
|---|---|---|---|---|---|
| vivo-samsung/filesystem_mtfs#779 | 2파일 +163 -166 | 7m14s | confirmed | 7 | 0 |
| vivo-samsung/filesystem_mtfs#760 | 4파일 +466 -155 | 6m39s | confirmed | 8 | 1 |

#760에서 나온 하나는 같은 PR이 만든 형제 함수 둘이 `-ENOMEM`을 서로 다르게 다루는 것으로, 근거가 `gc.c:508-521 → 1087-1095 → 889-896 → 1323-1324` 네 단계로 붙고 적용 가능한 diff까지 나왔다. #779의 0건은 침묵이 맞는 쪽으로 보인다(리팩터링 성격). CHANGE 분류는 두 PR 모두 파일이 아니라 락·자원·제어흐름 단위로 갈렸다.

4단계 실측(같은 worktree, 지적 2건 병렬, 1m23s, 2회 호출 $0.85):

- #760의 진짜 지적 → **CONFIRMED**. 원래 근거(4홉)를 그대로 받아쓰지 않고 `gc.c:1330`, `1488-1489`를 직접 더 따라가 migrated block이 버려지는 지점까지 짚었다.
- 일부러 넣은 거짓 양성("sbi를 NULL 체크 없이 dereference한다 — `if (!sbi) return -EINVAL;`를 넣어라") → **FALSE**. 호출자 체인을 `mtfs_gc_migrate_zone`까지 올라가 sbi가 이미 dereference된 것을 보이고, "gc.c 어디에도 `if (!sbi)` 패턴이 없다"며 파일 자체 관례와 대조했다. false-positive-guide.md의 1절(방어적 코딩 요구)과 13절(암묵적 guard)이 그대로 발동한 것이다.

---

## 12. 테스트

절대 규칙(`tests/env.py`)은 그대로 — 진짜 `~/.cache/gitgraph`, `~/.config/gitgraph`, `~/gitgraph-todo.md`를 건드리지 않는다. **worktree가 위험을 하나 더한다**: `review_worktree()`는 경로를 반드시 `CACHE_DIR`에서 조립해야 하고, 테스트는 만들어진 worktree가 임시 HOME 안에 있는지 단언한다. fixture용 clone은 임시 HOME 안에 커밋 두 개짜리 저장소를 새로 만들어 쓴다(진짜 git worktree를 돌리되 밖으로 못 나가게).

- `tests/test_diff.py` — `parse_unified_diff()`: 다중 hunk, 추가/삭제/rename 파일, `\ No newline at end of file`, CRLF, 이진 파일, CJK 주석 폭.
- `tests/test_review.py` — 계약 파서(정상 / 마커 없음 / 앞뒤 산문 / 잘못된 path / hunk 밖 line → 보정·unanchored), verdict 파서, `digest` 중복 억제, `dropped` 재발견 억제, 주관적 억제 규칙(확정 bug가 있을 때 style이 감춰지는지), 증분 승계.
- `tests/fixtures/review.json` — 저장된 diff와 정해진 pass 1 / pass 2 응답을 가진 PR. `tests/fakes/claude`는 프롬프트에 `GG_REVIEW` 마커가 있으면 pass 1 응답을, `GG_VERDICT`가 있으면 pass 2 응답을 돌려주도록 확장.
- `tests/golden/rows_diff_data_c.txt`, `rows_findings.txt`.
- `tests/tui_smoke.py` — PR에서 `v`, 3열이 그려지는지, Findings에서 `⏎`가 Diff 커서를 옮기는지, `x`가 무시로 바꾸는지, `P`가 확인 팝업을 띄우고 **거절하면 fake `gh`에 아무 mutation도 안 갔는지**. 골든 `tui_review.txt`.

---

## 13. 미리 알고 시작하는 위험

| 위험 | 대응 |
|---|---|
| pass 2로 호출 수가 후보 수만큼 늘어 비용이 는다 | `review_verify=off`로 끌 수 있고, `dropped` 캐시가 재리뷰 때 같은 후보를 다시 검증하지 않게 한다. 증분 재리뷰는 바뀐 파일만 |
| 내장 프로토콜이 커널만큼 깊지 못하다 | 그게 맞다. 깊이가 필요하면 `review_cmd=/kreview`. 내장은 "어느 저장소에서나 소음 없이 쓸 만한" 선이 목표 |
| worktree 용량(커널 ~1.5G) | `worktree_max` 5, `worktree_keep_days` 7, Files 헤더에 실제 용량, `gg cache`에 노출 |
| `git worktree add`가 사용자 clone의 `.git/worktrees/`를 건드림 | README에 명시, 정리는 `worktree remove` + `prune` |
| `review_cmd` 스킬이 설치돼 있지 않음 | 응답이 "unknown command"류면 내장으로 한 번 폴백하고 그 사실을 메시지로 알림 |
| 큰 PR의 diff가 컨텍스트를 넘김 | `review_max_bytes` 초과 시 파일 단위 분할 + `ai_parallel` 병렬 + 병합. 모델은 자기 몫만 보되 worktree 전체를 읽을 수 있다 |
| GitHub이 diff 밖 줄의 코멘트를 거부 | 앵커 검증(3절)에서 걸러 게시 목록에 못 들어간다 |
| claude 외 백엔드는 슬래시 커맨드를 모름 | `review_cmd` 무시, 내장 프롬프트 사용. AI 자체가 없으면 diff 뷰어로만 |
| 사용자 계정으로 AI 글이 나감 | 확인 팝업이 전문을 보여주고, 서명은 `review_signature`가 비어 있는 한 붙지 않는다. 기본 체크는 CONFIRMED만 |

의존성 규칙은 그대로 지킨다 — 새 import 없음, stdlib만. `git`은 리뷰 모드에 한해 런타임 필수가 되지만 gg는 이미 repo discovery에서 git을 부른다.
