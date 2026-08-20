# 작업 규칙

AI 파트 저장소의 브랜치 · 커밋 · 이관 규칙입니다.

---

## 1. 브랜치 전략

이 저장소는 **팀 서비스 저장소가 만들어지기 전의 임시 개인 저장소**입니다.
따라서 지금은 가볍게 가되, **이관 시점에 마찰이 없도록** 커밋 이력과 규칙을 미리 지켜둡니다.

### 현재 — 단독 작업 단계

```
main                    항상 동작하는 상태 유지
 ├── feat/engine-portfolio
 ├── feat/rag-dart-ingest
 └── fix/hhi-cash-normalize
```

혼자 작업하므로 `develop`을 두지 않습니다. 통합 지점이 없는 상태에서 중간 브랜치를 만들면
머지 단계만 하나 늘어날 뿐입니다.

### 팀 저장소 이관 후

```
main                    배포 가능 상태. 태그로 마일스톤 표시
 └── develop            통합 브랜치. 기본 브랜치로 설정
      ├── feat/ai-engine-portfolio
      ├── feat/be-order-api
      └── feat/fe-portfolio-screen
```

모노레포가 되면 브랜치명에 파트 구분자(`ai-` / `be-` / `fe-`)를 붙입니다.
지금은 AI 전용이므로 붙이지 않습니다.

### 브랜치 이름

```
<type>/<대상>-<내용>
```

| type | 용도 |
|---|---|
| `feat` | 기능 추가 |
| `fix` | 버그 수정 |
| `docs` | 문서만 변경 |
| `refactor` | 동작 변경 없는 구조 개선 |
| `test` | 테스트 추가 · 수정 |
| `chore` | 빌드 · 설정 · 의존성 |

예시

```
feat/engine-risk-score
feat/rag-dart-ingest
feat/wiki-thesis-collect
fix/attribution-carino-linking
docs/prompt-policy
chore/ci-pytest
```

---

## 2. 커밋 규칙

Conventional Commits를 따르고 **제목은 한글**로 씁니다.

```
<type>(<scope>): <제목>

<본문 — 왜 이렇게 했는지. 무엇을 했는지는 diff가 말해준다>
```

### scope

| scope | 범위 |
|---|---|
| `engine` | Portfolio · Risk · Attribution 계산 |
| `rag` | 공시 · 뉴스 수집, 임베딩, 검색 |
| `llm` | 프롬프트 조립, 생성 |
| `guard` | Guardrail 입력단 · 출력단 |
| `api` | FastAPI 라우터, 스키마 |
| `wiki` | 사용자 논지 · 성향 |
| `ingest` | 배치 적재 |
| `eval` | 평가셋, 골든 케이스 |
| `infra` | 설정, CI, 의존성 |

### 예시

```
feat(engine): 포트폴리오 비중·평가손익 계산 추가

이동평균법으로 평균매입가를 관리한다. 국내 증권사 관례를 따라야
앱의 다른 화면과 손익이 어긋나지 않는다.
```

```
fix(engine): HHI 계산에서 현금 제외 누락 수정

현금을 분모에 포함하면 현금만 늘려도 분산된 것처럼 보인다.
주식 비중으로 재정규화한 뒤 계산하도록 바꿨다.
```

```
docs(api): 응답 구조를 narrative/metrics에서 text/segments로 변경

숫자 치환을 프론트에 떠넘기고 있었다. 분리 원칙에 어긋나고,
프론트가 실수하면 정확도 책임을 AI 파트가 질 수 없다.
```

### 지키는 것

- 제목은 50자 이내, 마침표 없음
- 명령형 대신 **완료형**(`추가`, `수정`, `변경`)
- 본문은 **왜**를 쓴다. 무엇을 했는지는 diff에 있다
- 한 커밋에 한 가지 변경. 리팩터링과 기능 추가를 섞지 않는다

---

## 3. PR 규칙

혼자 작업하더라도 **작업 단위는 PR로 남깁니다.** 이력이 읽히고, 팀 저장소로 옮긴 뒤에도
어떤 판단으로 무엇을 만들었는지 추적됩니다.

- 대상 브랜치는 `main` (이관 후에는 `develop`)
- **Squash merge** — 작업 브랜치의 중간 커밋은 이력에 남기지 않는다
- 머지 후 원격 브랜치 삭제
- PR 본문에 관련 설계 문서 절 번호를 적는다 (예: `엔진 산식 §3.6`)

### PR 템플릿

```markdown
## 무엇을
## 왜
## 설계 문서
- 엔진 산식 §
## 확인
- [ ] 검증 항등식 통과
- [ ] 골든 케이스 통과
- [ ] 키·자격증명 미포함
```

---

## 4. 태그

Phase 완료 시점에 태그를 남깁니다.

```
v0.1.0    Phase 1 — Portfolio Context · Event/RAG · Wiki
v0.2.0    Phase 2 — Risk Engine
v0.3.0    Phase 3 — Attribution Engine
```

---

## 5. 절대 하지 않는 것

| 금지 | 이유 |
|---|---|
| `.env` 커밋 | KIS · DART · Anthropic 키. **히스토리에 한 번 들어가면 이관해도 따라간다** |
| 적재 데이터 커밋 | 시세 · 공시 원문은 용량이 크고 재생성 가능하다 |
| `main` force push | 이관 시 히스토리가 깨진다 |
| 파생 지표를 백엔드에서 받아 쓰기 | 화면마다 값이 어긋난다. 원장만 읽고 직접 계산한다 |

> 이 저장소는 현재 **public**입니다. 금융 API 키를 다루는 동안에는 private 전환을 권합니다.
> `gh repo edit --visibility private`

---

## 6. 설계 문서

문서는 두 형식으로 둔다.

| 형식 | 대상 | 이유 |
|---|---|---|
| `docs/*.md` | **에이전트·워커** | HTML은 절반 이상이 CSS·태그라 컨텍스트를 낭비한다 |
| `docs/*.html` | 사람, 팀 공유 | 아티팩트로 배포된 읽기 좋은 형태 |

HTML이 원본이고 Markdown은 파생물이다. 문서를 고치면 반드시 다시 생성한다.

단, `.html` 짝이 없는 `.md`는 손으로 쓴 문서다(예: `worker-task-template.md`).
변환 대상이 아니므로 직접 고친다.

```bash
python scripts/html2md.py          # 재생성
python scripts/html2md.py --check  # 최신 여부 확인 (CI용)
```

워커 지시문에서는 `.md` 경로를 가리킨다. 전체가 아니라 필요한 절만 지정하면
더 줄어든다.

---

## 7. 적재 순서

`price_daily.ticker`는 `instruments`를 참조한다. 따라서 **시세 적재는 종목 마스터에
의존한다.**

```
ingest/instruments.py   ← 먼저
ingest/prices.py        ← 그다음
```

제약을 완화하지 않는다. 마스터에 없는 종목의 시세는 섹터도 DART 고유번호도 없어
해석할 수 없고, 그런 행이 쌓이면 나중에 정합성을 되돌리기 어렵다.

시세 적재는 사람이 붙어 있을 필요가 없는 기계 시간 작업이다. 마스터가 준비되면
백그라운드로 걸어두고 다른 트랙을 진행한다.

```bash
python -m ingest.prices --days 120 --limit 300 &
```

전종목을 처음부터 받지 않는다. 시드 포트폴리오에 들어갈 종목만 먼저 확보하면
후속 트랙이 며칠 빨리 시작된다.

---

## 8. 병렬 작업 — worktree

여러 트랙을 동시에 진행할 때는 브랜치를 갈아타지 않고 **worktree**를 쓴다.
트랙마다 자기 디렉토리에서 자기 브랜치를 체크아웃하므로 파일이 충돌하지 않는다.

```bash
git worktree add ../wt-ingest -b feat/ingest-instruments main
cd ../wt-ingest && ./scripts/bootstrap.sh
```

### 부트스트랩이 필요한 이유

새 worktree에는 **추적 파일만** 복제된다. `.env`와 `.venv`는 git에 없으므로
비어 있고, 그대로 두면 작업 시작과 동시에 "키가 없다"로 멈춘다.

`scripts/bootstrap.sh`가 이를 처리한다. 여러 번 실행해도 안전하다.

| 항목 | 처리 |
|---|---|
| `.env` | 메인 워크트리에서 **심볼릭 링크**. 키를 한 번만 갱신하면 전체에 반영된다 |
| `.venv` | worktree마다 **별도 생성**. 공유하면 한쪽 `pip install`이 다른 쪽을 오염시킨다 |
| DB | 띄우지 않고 확인만. 컨테이너는 **호스트에 하나**만 둔다 — worktree마다 띄우면 5432가 충돌한다 |
| 마이그레이션 | DB가 떠 있으면 `alembic upgrade head` |

### Orca를 쓰는 경우

Orca의 repo 설정에서 setup 훅 스크립트를 아래로 지정한다.
CLI로는 바꿀 수 없고 앱 UI에서 설정해야 한다.

```
./scripts/bootstrap.sh
```

기본값이 `pnpm install`로 남아 있으면 이 저장소에서는 매번 실패한다.
훅을 고치지 못하는 상황이라면 워커의 첫 지시에 부트스트랩 실행을 넣는다.

### 정리

```bash
git worktree remove ../wt-ingest
git worktree prune
```

---

## 9. 팀 저장소 이관 절차

서비스 저장소가 만들어지면 아래 중 하나를 선택합니다.

### A. AI 서비스를 별도 저장소로 유지 (권장)

분리 원칙과 가장 잘 맞습니다. 리모트만 교체하면 히스토리가 그대로 따라갑니다.

```bash
git remote set-url origin https://github.com/<org>/<ai-repo>.git
git push -u origin main
```

### B. 모노레포에 서브디렉토리로 병합

히스토리를 보존하며 하위 경로로 들어갑니다.

```bash
# 모노레포에서 실행
git subtree add --prefix=ai-service https://github.com/tpals0409/ai-invest-service.git main
```

### 이관 전 반드시 확인

```bash
# 히스토리 전체에 자격증명이 없는지 검사
git log -p --all | grep -iE '(api[_-]?key|secret|password|token)\s*=\s*[A-Za-z0-9]' | head

# 추적 중인 파일 목록에 .env 계열이 없는지
git ls-files | grep -E '\.env'
```

둘 다 결과가 비어 있어야 합니다. 걸리는 것이 있으면 이관 전에 히스토리를 정리하고,
노출된 키는 **전부 재발급**합니다.
