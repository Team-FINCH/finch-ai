# 워커 태스크 지시문 템플릿

`CLAUDE.md`가 작업 규율을 담당한다. 지시문에는 **무엇을 만들지**와
**그 태스크 고유의 함정**만 쓴다. 규율을 다시 적지 않는다.

에이전트만 읽으므로 **영어로 쓴다.** 25~30줄을 넘기지 않는다.

```text
[GOAL] One sentence. Why this matters to the critical path.

[BUILD]
  path/to/module.py       what it does
  tests/test_module.py

[VERIFIED — already checked, do not re-verify]
  Paste exact API signatures, response field names, formulas, or schemas
  that were confirmed working. This is what keeps the worker off a
  wild goose chase and out of the design docs.

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
