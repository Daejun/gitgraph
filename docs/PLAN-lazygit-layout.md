# gg TUI를 lazygit layout으로 바꾸는 계획

상태(0.5.0): B안 1~7단계 모두 반영 — Panel 추상화, layout 엔진(accordion·screen mode·portrait·border), Repo/Home/Links/Comments/People + main 탭, lazygit 키, 팝업(입력·메뉴·확인·텍스트, `?` 키 메뉴, `O` 옵션), 경계 드래그, tests/tui_smoke.py.

기준: lazygit `docs/Config.md`(gui.sidePanelWidth 0.3333, sidePanels stack, expandFocusedSidePanel, screenMode normal/half/full,
portraitMode auto ≤84열, border rounded, showBottomLine/showPanelJumps), `docs/keybindings/Keybindings_en.md`
(1-5 panel jump, 0 main, Tab, `[`/`]` tab, `+`/`_` screen mode, `,`/`.` page, `<`/`>` top/bottom, H/L, K/J main scroll, `/` search, `?` menu, Esc).

## 1. 목표 layout

```
┌─1 Repo ─────────────┐┌─ Main ─────────────────────────────────────────────┐
│ vivo-samsung/… 57 ↻ ││ 2026-08-13 #750 [PR] mtfs: running out of space … │
├─2 Home [my turn]────┤│ @Daejun7Park · updated 08-19 · labels …            │
│ > #768 …            ││                                                    │
│   #693 …            ││ Running the device out of space under concurrent…  │
├─3 Graph [tree]──────┤│ (본문 / 코멘트 전문 / tree / 답변 — 탭)              │
│ ▾ #750              ││                                                    │
│ ├─ → refs #748      ││                                                    │
├─4 Comments [all]────┤│                                                    │
│ +0d @Daejun7Park »… ││                                                    │
├─5 People ───────────┤│                                                    │
│ @Daejun7Park  @… … ││                                                    │
└─────────────────────┘└────────────────────────────────────────────────────┘
 1-5 panel  0 main  Tab next  [ ] tab  + _ screen  Enter open  a ask  ? keys  q quit
```

- 왼쪽 side column(폭 1/3, `side_width`)에 panel 5개를 세로로 stack, 오른쪽 main panel은 side에서 선택한 것의 내용.
- 포커스된 side panel: 테두리·제목 강조(theme 색), `expand_focused`면 accordion(높이 2배).
- screen mode `+`/`_`: normal → half(포커스 panel이 왼쪽 전체 높이) → full(포커스 panel이 화면 전체).
- portrait(폭 ≤ 84): side를 위, main을 아래로 stack.
- 맨 아래 한 줄: 현재 panel의 키 힌트 + panel 번호(showPanelJumps).
- 팝업(중앙 box): `a` 질문 입력, `/` 검색, repo 선택, `?` 키 메뉴, 확인.

### panel ↔ gg 데이터

| # | panel | 내용 | 탭(`[`/`]`) | Enter |
|---|---|---|---|---|
| 1 | Repo | repo, fetched 시각, 항목 수, me, theme, token usage, 백그라운드 진행 | – | refetch |
| 2 | Home | 현재 home 섹션 목록 | my turn · mentions · opened · active · waiting · mine · PRs · stale · all | Graph root로 설정 + Graph panel 포커스 |
| 3 | Graph | root 기준 tree/log (접기·`⇢` 점프 그대로) | tree · log | 노드 재루팅(현재 Enter) / `⇢`는 점프 |
| 4 | Comments | 선택 항목의 코멘트 시간순(`+Nd @who » 요약`) | all · linked | main에 코멘트 전문 |
| 5 | People | 선택 항목의 작성자·mention된 사람 | – | 그 사람 관점(`u`)으로 Home 갱신 |
| 0 | Main | 선택된 것의 전문: 본문+메타 / 코멘트 / (질문 답) | content · answer | – |

side 선택이 바뀌면 main이 따라 바뀐다(lazygit의 file → diff 관계). Home/Graph/Comments 어느 panel에서든 "현재 항목"이 하나 있고 Comments/People/Main은 그 항목을 따른다.

### 결정이 필요한 것

- **A안(lazygit 그대로)**: tree를 3번 side panel(폭 1/3)에 둔다. 좁아서 tree 줄이 많이 잘린다(`>`/`<` 가로 스크롤 의존).
- **B안(권장)**: Graph는 main의 탭으로 두고 side에는 목록(Home/Comments/People)만. tree가 넓게 보이고 lazygit의 "side = 목록, main = 내용" 원칙과도 맞는다. 3번 panel 자리는 "Links"(선택 항목의 `→ refs / ← cited-by / closes` 목록, Enter로 이동)로 채운다.
- 80x24 터미널에서 side panel 5개는 각 3~4줄이라 `expand_focused`를 기본 on으로 둘지.

## 2. 구현 단계 (현재 코드: 단일 rows + preview pane + 답 side panel, `Tui` ~900줄)

1. **Panel 추상화** — `Panel(title, tabs, rows, cur, top, hs, keymap)` + `draw(rect, focused)` + `on_key`. 지금 `Tui`의 `rows/cur/top/hs/pv/side_*`를 panel 인스턴스 상태로 옮긴다. 렌더러(`home_rows`, `tree_rows`, `log_rows`, `preview_lines`, `render_show`, `segments`)는 그대로 재사용, 커서 유지(`find_row(near)`)·접힘(`collapsed`)·history(`snapshot/restore`)는 Graph panel 안으로.
2. **Layout 엔진** — 화면 크기와 (`side_width`, `expand_focused`, `expanded_weight`, `screen_mode`, `portrait`)로 각 panel의 (y, x, h, w)를 계산하고 테두리를 그린다(`border`: rounded · single · hidden, 포커스 색은 theme의 `fold`/`head` 스타일 재사용). 화면 리사이즈(`KEY_RESIZE`) 재계산.
3. **Panel 5개 + main 연결** — 선택 → main 갱신 규칙, Enter 규칙(위 표), Home 섹션을 탭으로, Comments/People panel 신설. 백그라운드 번역·요약은 "보이는 panel의 보이는 줄" 기준으로 유지(`enrich`가 모든 panel의 viewport를 합산).
4. **키 재배치(lazygit 관례)** — `1-5`/`0` 점프, Tab/Shift-Tab, `[`/`]` 탭, `+`/`_` screen mode, `,`/`.` 페이지, `<`/`>` 처음/끝, `H`/`L` 가로, `K`/`J` main 스크롤, `?` 컨텍스트 키 메뉴, Esc 취소→뒤로. 충돌 정리: 지금 `1-9`(펼침 깊이)→`=`+숫자 또는 메뉴, `<`/`>`(가로 스크롤)→`H`/`L`, `J`/`K`(preview)→main 스크롤로 통일, `w`(포커스 순환)→Tab으로 흡수, `A`/`v`(panel 토글)→main 탭·screen mode로 흡수.
5. **하단 키 힌트 줄 + 팝업 위젯** — prompt(`a`, `/`, `u`), 목록 메뉴(`?`, repo 선택, `c`/`t`/`s` 같은 토글 모음), 확인. 기존 `prompt_line`/`pager` 대체.
6. **마우스** — 클릭한 panel 포커스 + 그 줄 선택, 휠은 마우스 아래 panel, side/main 경계 드래그로 `side_width` 조절(선택), 더블클릭 = Enter, 오른쪽 클릭 = 브라우저(유지).
7. **설정·문서·테스트** — `gg config`에 `side_width`, `expand_focused`, `expanded_weight`, `screen_mode`, `border`, `portrait` 추가; README 두 판의 TUI 절 교체; pty 키 로그 테스트를 panel 단위로 갱신.

각 단계 끝에 (1) `python3 -m py_compile`, (2) pty 스크립트로 키 로그 확인, (3) 실제 터미널(PuTTY 포함, `basic` 테마)에서 눈으로 확인.

## 3. 규모·순서

- 1→2→3이 핵심이고 가장 크다(대략 Tui 재작성, 800~1,200줄). 4~6은 그 위에 작은 증분. 7은 마지막.
- 중간 상태를 쓸 수 있게 하려면 2단계까지는 기존 화면을 "main 하나 + side 0개"로 감싸서 동작을 유지한 뒤 3단계에서 panel을 하나씩 추가한다.
- 기존 CLI 출력(`gg`, `gg show`, `gg ask`)은 건드리지 않는다.

## 4. 참고한 lazygit 설정 대응표

| lazygit | gg config |
|---|---|
| gui.sidePanelWidth 0.3333 | side_width |
| gui.expandFocusedSidePanel / expandedSidePanelWeight | expand_focused / expanded_weight |
| gui.screenMode normal·half·full, `+`/`_` | screen_mode |
| gui.portraitMode auto (≤84열, ≥46행) | portrait |
| gui.border rounded·single·double·hidden·bold | border |
| gui.showBottomLine / showPanelJumps | bottom_line (항상 on으로 시작) |
| gui.sidePanels 순서·탭 | side_panels (2단계 이후 고려) |
