# AI 투자 비서 — AI 파트

국내 주식 투자 앱에 얹는 Personal Investment Copilot의 **AI 파트**입니다.
팀 저장소의 `ai/` 디렉터리이며, 아래 명령은 모두 `ai/` 안에서 실행합니다.
사용자의 포트폴리오와 신뢰할 수 있는 금융 데이터를 연결해, 투자 상황을 이해하고 설명합니다.

> **Calculation은 Engine이 하고, Explanation은 AI가 한다.**
> LLM은 어떤 수치도 스스로 만들지 않습니다.

---

## 설계 문서

작업 전에 해당 문서를 먼저 확인하세요. 세 문서는 서로 물려 있습니다.

**에이전트는 `.md`를 읽으세요.** 같은 내용이지만 HTML은 절반 이상이 CSS와 태그라
컨텍스트를 낭비합니다. HTML은 사람이 읽고 공유하는 용도로 유지합니다.

| 문서 | 내용 | 에이전트용 | 사람용 |
|---|---|---|---|
| [API 명세](https://claude.ai/code/artifact/84ddb8b2-2e3b-4d90-90bf-e56af4459aad) | 프론트 계약, 외부 의존과 자체 조달 범위 | `docs/api-spec.md` | `docs/api-spec.html` |
| [엔진 산식](https://claude.ai/code/artifact/d03728c6-e9a9-470b-b6a4-93ec26fa71c5) | Portfolio · Risk · Attribution 계산식, 검증 항등식 | `docs/engine-formulas.md` | `docs/engine-formulas.html` |
| [응답 정책](https://claude.ai/code/artifact/8e53d1f6-4067-49f8-8304-d2bbd6368036) | 프롬프트 계층, 답변 구조, Guardrail, 모델 라우팅 | `docs/prompt-policy.md` | `docs/prompt-policy.html` |

---

## 전제

| 항목 | 내용 |
|---|---|
| 시장 | 국내 주식 전용 (KOSPI · KOSDAQ) |
| 포트폴리오 | 앱 내 가상 포트폴리오. 실계좌 연동 없음 |
| 통화 | KRW 단일. 환율 · 국가 노출 분석 없음 |
| 계산 주체 | Attribution · Risk 엔진 모두 AI 파트가 구현 |
| 기본 모델 | `claude-opus-5` |

### 분리 원칙

AI 기능의 구현은 AI 파트가 전부 소유합니다. 다른 파트에 계산이나 조립 로직을 요구하지 않고,
신규 화면 개발도 요청하지 않습니다. 타 파트 의존은 **기존 원장 조회 API의 읽기 권한** 하나뿐이며,
그마저 시드 데이터 어댑터로 대체해 병렬 진행합니다.

### ⚠️ 배포 조건 — AI 서버는 외부에 노출하지 않습니다

호출 경로는 **프런트 → 백엔드 → AI** 입니다. JWT 검증은 백엔드가 끝내고, AI 는 백엔드가 넘긴
`X-User-Id` 헤더를 **검증 없이 그대로 신뢰**합니다(`app/api/deps.py`).

따라서 **AI 서버는 내부 네트워크에서만 접근 가능해야 합니다.** 외부에서 직접 닿을 수 있으면
누구나 헤더를 위조해 남의 위키·포트폴리오를 읽습니다. 공인 IP·인그레스·포트 포워딩을 열지 말고,
백엔드가 클라이언트발 `X-User-Id` 를 자기가 검증한 값으로 덮어쓰게 하세요.
자세한 계약은 [`docs/api-contract-proposal.md`](docs/api-contract-proposal.md) §4 에 있습니다.

---

## 사용자 대면 기능

| 기능 | 화면 | 구조 | Phase |
|---|---|---|---|
| AI 종목 분석 | 종목 상세 | RAG 파이프라인 | 1 |
| Ask My Portfolio | 채팅 | 툴콜링 에이전트 | 1 |
| AI가 이해한 나 | 마이페이지 | Wiki 조회 · 편집 | 1 |
| 포트폴리오 진단 | 포트폴리오 | 결정론적 파이프라인 | 2 |
| 주문 전 점검 | 주문 | 진단 엔진 차분 | 2 |
| 수익률 원인 분석 | 홈 · 포트폴리오 | 결정론적 파이프라인 | 3 |
| 데일리 브리핑 | 홈 | 배치 파이프라인 | 3 |

툴콜링 에이전트는 `Ask My Portfolio` 하나뿐입니다. 나머지는 엔진 출력 JSON을 받아
문장만 생성하는 단방향 파이프라인입니다.

---

## 구조

```
app/
├── engines/   Portfolio · Risk · Attribution 계산
├── rag/       DART 공시 · 뉴스 수집, 임베딩, 검색
├── llm/       프롬프트 조립, 생성, Guardrail 검사
├── api/       FastAPI 라우터
├── wiki/      사용자 투자 논지 · 성향
└── core/      설정, DB, 공통 유틸
ingest/        시세 · 공시 · 마스터 배치 적재
prompts/       프롬프트 파일 (버전 태그로 관리)
tests/golden/  손으로 검산 가능한 회귀 케이스
eval/          평가셋과 실행 결과
docs/          설계 문서
```

프롬프트는 코드가 아니라 **데이터**로 다룹니다. 소스에 하드코딩하지 않고 `prompts/`에 두고
버전 태그를 응답 로그에 기록합니다.

---

## 시작하기

```bash
# 1. 파이썬 환경
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                 # 키 채우기

# 2. DB — Postgres + pgvector (podman)
podman machine start                 # 최초 1회는 podman machine init 필요
podman-compose up -d

# 3. 스키마 적용
alembic upgrade head

# 4. 실행
uvicorn app.api.main:app --reload

# 5. 검사 — 푸시 전에 돌립니다
./scripts/check.sh
```

`http://localhost:8000/docs`에서 OpenAPI 문서를 확인할 수 있습니다.

`scripts/check.sh`는 린트·마이그레이션 드리프트·테스트·평가 지표·OpenAPI 스키마
최신 여부 등 7종을 순서대로 돌리고, 실패한 항목을 한 번에 모아 보고합니다.
DB 나 의존성이 준비되지 않았으면 무엇을 실행해야 하는지 알려주고 멈춥니다.

필요한 외부 키는 `.env.example`를 참고하세요. **키는 절대 커밋하지 않습니다.**

> 컨테이너 런타임은 **podman**을 씁니다. `compose.yaml`은 표준 Compose 규격이라
> Docker를 쓰는 팀원은 `docker compose up -d`로 그대로 사용할 수 있습니다.

---

## 에이전트로 작업할 때

작업 규율(커밋 단위, 탐침 우선, 읽기 범위, 완료 정의)은 개인 에이전트 설정에 두고
저장소에는 두지 않는다. 팀 저장소로 이관하면서 개인 설정 문서는 제외했다.
태스크 지시문 형식은 [docs/worker-task-template.md](docs/worker-task-template.md) 참고.

AI 파트는 `ai/` 안에서만 작업한다. `backend/`, `frontend/`, `infra/`, `docs/` 와
저장소 루트는 읽기 전용이다.

## 평가

```bash
python -m eval.run --list       # 검색 평가셋 요약
python -m eval.run --metrics    # 지표 자체 점검 (키 불필요, CI에 포함)
python -m eval.run --retrieval  # 검색 정확도 (임베딩 키 필요)
```

자동 지표는 셋뿐이다 — `Numerical Accuracy` · `Groundedness` ·
`Portfolio Accuracy`. 응답 정책의 목록은 열한 개지만 초기에 다 만들면 어느 것도
제대로 쓰지 못한다. 이 셋은 LLM 판정 없이 계산돼 CI에서 돈다.

검색 평가셋은 **우리 코퍼스에 실제로 답이 있는 질문만** 담는다. 코퍼스를 다시
적재하면 `eval/retrieval.yaml`도 같이 손봐야 한다.

## 브랜치 전략

팀 저장소 기본 브랜치는 `master` 입니다. AI 파트는 **에픽 단위**로 브랜치를 하나 열고,
그 아래 스토리들을 커밋으로 누적한 뒤 에픽 작업이 끝나면 MR 을 한 번 엽니다.

```
master
 └── FINCH-4-ai-service      ← 에픽 FINCH-4 "AI 서비스"
      ├── ✨feat: AI 파트 로컬 검사 스크립트 추가        (FINCH-67)
      └── 📝docs: 이관 이후 낡은 AI 파트 문서 정정        (FINCH-68)
```

스토리마다 Jira 티켓은 만들되 MR 은 열지 않습니다. 브랜치가 오래 사는 만큼
작업 중 주기적으로 `origin/master` 를 반영하고, 스토리별로 커밋을 분리해
리뷰어가 단위로 읽을 수 있게 합니다.

커밋 메시지는 팀 컨벤션(`✨feat:`, `📝docs:` 등)을 따릅니다 —
저장소 루트의 `docs/convention/gitConvention.md` 를 참고하세요.
이관 전 커밋들이 쓰던 `feat(scope):` 형식은 그때의 기록이므로 그대로 둡니다.

자세한 규칙은 [CONTRIBUTING.md](CONTRIBUTING.md) 를 참고하세요.
