# 워커 태스크 지시문 템플릿

`CLAUDE.md`가 작업 규율을 담당한다. 지시문에는 **무엇을 만들지**와
**그 태스크 고유의 함정**만 쓴다. 규율을 다시 적지 않는다.

에이전트만 읽으므로 **영어로 쓴다.**

길이는 목표가 아니다. 줄여야 하는 것은 `CLAUDE.md`에 이미 있는 규율의 반복이지,
확인된 사실이 아니다. 오늘 통한 지시문들은 80~120줄이었고 길어진 몫이 전부
`[VERIFIED]`와 `[PATTERN]`이었다 — 그만큼 워커가 읽지 않아도 되는 분량이다.
반대로 W10의 지시문은 짧았고, 짧았기 때문에 실패했다.

```text
[GOAL] One sentence. Why this matters to the critical path.

[BUILD]
  path/to/module.py       what it does
  tests/test_module.py

[VERIFIED — already checked, do not re-verify]
  Paste exact API signatures, response field names, formulas, or schemas
  that were confirmed working. This is what keeps the worker off a
  wild goose chase and out of the design docs.

[PATTERN — the shape, not the file]
  If an existing file solves the same problem, paste its skeleton: call
  order, the names to import and from where, the return type. Then say:
  "You do not need to read it — its shape is here. Read it only if
  something below is genuinely ambiguous, and then read it once, whole."

[TRAPS — specific to this task]
  Only non-obvious failure modes. Two or three at most.
  Skip anything already in CLAUDE.md.

[SCOPE]
  Touch only: <dirs>
  Other tracks own: <dirs>

[DONE]
  Anything beyond the standard definition of done in CLAUDE.md.
```

## 왜 이 형태인가

측정으로 확인한 것:

- W4는 검증된 API 파라미터를 지시문에 받아 **읽기 15회 / 중복 0KB**로 가장 깨끗했다
- W3는 그런 정보가 없어 **읽기 47회 / 중복 40KB**를 썼고 문서를 12조각으로 나눠 읽었다

워커가 문서를 뒤지게 두는 대신 답을 지시문에 넣으면 턴이 줄고, 턴이 줄면
컴팩션이 줄고, 컴팩션이 줄면 요약 래칫이 시작되지 않는다.

## 참고 파일은 가리키지 말고 형태를 붙여 넣는다

같은 태스크를 두 번 돌려 확인했다.

| | 지시문 | 결과 |
|---|---|---|
| W10 | "`stocks.py`를 본으로 삼아라" | 그 파일만 반복해 읽다가 중단. 산출물 0 |
| W10b | 같은 태스크 · 라우트 골격과 import 목록을 붙여 넣음 | 커밋 2건 · PR · 테스트 502건 |

파일을 가리키는 것은 답이 아니라 숙제를 주는 것이다. 워커는 그 안에서 무엇이
본질이고 무엇이 그 파일 사정인지 모르므로, 알아낼 때까지 읽는다.

붙여 넣을 것은 골격이지 전문이 아니다. 함수 호출 순서, import할 이름과 모듈
경로, 반환 타입이면 충분하다. 그리고 **"이 파일은 읽지 않아도 된다. 아래에
재현해 뒀다. 정말 모호할 때만 한 번, 통째로 읽어라"**를 같이 적는다. 이 문장이
없으면 붙여 넣어도 확인하러 간다.

W13에 이 문장을 넣었더니 `portfolio.py`를 한 번만 읽고 끝냈다.

## 병렬로 띄울 때 — 스키마 소유권을 한 워커에만 준다

`app/core/models.py`와 `alembic/`을 둘 이상이 건드리면 마이그레이션 head가
갈라진다. 두 워커를 동시에 띄우기 전에 **어느 쪽이 스키마를 바꿔야 하는지 먼저
판정하고**, 한쪽에는 명시적으로 위임하고 다른 쪽에는 명시적으로 금지한다.

금지하는 쪽에는 이유와 대안을 함께 적는다 — "스키마 변경이 필요하다는 결론이
나면 만들지 말고 보고하라". 그래야 워커가 막혔을 때 몰래 만들지 않는다.

W12(위임)·W13(금지)로 돌린 결과 head는 하나로 유지됐고 두 PR의 변경 파일이
하나도 겹치지 않았다.

## 완료 보고에 숫자를 요구한다

"적재 완료"는 0행 적재와 구분되지 않는다. 이 저장소는 이미 한 번 그렇게 당했다
(pykrx가 빈 결과를 조용히 돌려준 건).

적재·백필 태스크에는 무엇을 세어 보고할지 지시문에 적는다 — 적재 행 수, 기간,
그리고 **채우지 못한 건수**. 마지막 항목이 핵심이다. 성공만 세면 커버리지가
99%인지 5%인지 알 수 없다.

## 컴팩션 이후의 재오리엔테이션

W4를 관찰하니 컴팩션 직후 같은 파일 넷(`instruments.py`, `models.py`,
`api-spec.md`, `CONTRIBUTING.md` 297줄)을 반복해 읽는 루프에 들어갔다.
컴팩션 4회에 트랜스크립트가 932KB까지 불었다.

트랜스크립트는 마지막 창만 보존하므로 이 반복은 사후 측정에 잡히지 않는다.
창 안에서 재독을 세면 0으로 나온다 — 경계를 넘는 반복을 놓치기 때문이다.

`CLAUDE.md`의 진행 메모 규칙이 이 루프를 끊는다. 500바이트짜리 메모 하나를
읽는 것이 소스 넷을 다시 읽는 것보다 싸다.

## 워커를 띄울 때 — 모델·effort를 지정하지 말 것

```bash
orca orchestration worker-start --run <run> --task <task> \
  --worktree new-top-level --agent claude \
  --repo <repo> --base-branch main --name feat/... --setup run
```

`--model`과 `--effort`를 **넘기지 않는다.** 전역 설정(`~/.claude/settings.json`)에
이미 `"model": "opus[1m]"`, `"effortLevel": "xhigh"`가 있고, 인자를 주면 그것을
덮어쓴다.

W1~W5를 `--model claude-opus-5 --effort high`로 띄웠다가 **컨텍스트 창이
1M에서 200K로 떨어졌다.** 워커들이 4~8회씩 컴팩션한 원인이 이것이었다.
요약이 계단식으로 커지는 현상도, 컴팩션 후 같은 파일을 다시 읽는 루프도
전부 여기서 파생된 증상이었다.

실측 비교:

| 워커 | 창 | 컴팩션 | 읽기 |
|---|---|---|---|
| W3 | 200K | 8 | 34 |
| W1 | 200K | 6 | 13 |
| W2b | 200K | 4 | 20 |
| W4 | 200K | 4 | 2 |
| W5b | **1M** | **0** | **1** |

W5는 167,973 토큰에서 컴팩션됐고, W5b는 171,123 토큰에서 컴팩션이 없었다.
같은 구간을 넘긴 것이 창 차이의 직접 증거다.

`--effort`는 `--model` 없이 쓸 수 없으므로, 둘 다 생략하는 것이 유일하게
전역 설정을 온전히 상속하는 방법이다.
