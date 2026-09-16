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
| 기본 모델 | GMS `gpt-5-nano` |

### 분리 원칙

AI 기능의 구현은 AI 파트가 전부 소유합니다. 다른 파트에 계산이나 조립 로직을 요구하지 않고,
신규 화면 개발도 요청하지 않습니다. 타 파트 의존은 **기존 원장 조회 API의 읽기 권한** 하나뿐이며,
그마저 시드 데이터 어댑터로 대체해 병렬 진행합니다.

### 원장을 어디서 읽는가 — `LEDGER_SOURCE`

거래·보유는 백엔드가 소유하고 AI 는 읽기만 합니다. 어디서 읽을지는 설정 하나로 정하며,
선택은 `app/core/adapters.py` 의 `ledger_source()` 한 곳에서만 합니다.

| 값 | 동작 | 언제 |
|---|---|---|
| `backend` (기본) | 보유·거래는 백엔드 `/internal/v1`, 시계열·섹터는 우리 DB | 실사용 기본값 |
| `seed` | `tests/fixtures/seed_portfolio.json` | 테스트 전용 |
| `none` | 원장 없음 | 원장 없이 종목 분석만 돌릴 때 |

`none` 이면 개인화 섹션(`my_impact` · `thesis_check`)이 조용히 비활성되고, 포트폴리오
진단은 `INSUFFICIENT_DATA` 로 나갑니다. 에러가 아니라 설계된 동작입니다.

**`backend` 는 백엔드가 `/internal/v1` 을 구현하기 전에는 쓸 수 없습니다.** 지금 켜면
원장을 못 읽어 위와 같은 비활성 경로를 탑니다. 시계열과 섹터는 백엔드가 주지 않고
우리가 `price_daily` · `instruments` 에 적재한 것을 씁니다 — 자세한 분담은
[`docs/api/aiApiSpec.md`](../docs/api/aiApiSpec.md) §4.1 에 있습니다.

### 배포 조건 — AI 서버는 외부에 노출하지 않습니다

호출 경로는 **프런트 → 백엔드 → AI** 입니다. JWT 검증은 백엔드가 끝내고, AI 는 백엔드가 넘긴
`X-Internal-Token` 으로 호출자가 백엔드인지만 확인한 뒤 `X-User-Id` 헤더를 **검증 없이 그대로
신뢰**합니다(`app/api/deps.py`, 백엔드 명세 §9).

따라서 **AI 서버는 내부 네트워크에서만 접근 가능해야 합니다.** 외부에서 직접 닿을 수 있으면
누구나 헤더를 위조해 남의 위키·포트폴리오를 읽습니다. 공인 IP·인그레스·포트 포워딩을 열지 말고,
백엔드가 클라이언트발 `X-User-Id` 를 자기가 검증한 값으로 덮어쓰게 하세요.
자세한 계약은 [`docs/api/aiApiSpec.md`](../docs/api/aiApiSpec.md) §4 에 있습니다.

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

운영·스테이징의 호출 횟수와 GMS 토큰 예산은 PostgreSQL에 저장되어 여러 Pod가 같은
한도를 공유한다. 요청 창은 고정 구간 두 칸(현재·직전)을 겹치는 비율로 가중 합산해,
창이 바뀌는 순간 한도가 초기화되며 두 배가 통과하는 구멍을 막는다. 진행 중인 GMS
예약은 기본 5분 뒤 만료되므로 Pod 장애가 사용자의 하루 예산을 영구적으로 점유하지
않는다. `APP_ENV=local`은 DB 없이도 개발할 수 있게 프로세스 메모리 장부를 사용하며,
이쪽은 요청 시각을 그대로 들고 있어 근사 없이 직전 60초를 센다.

공시와 뉴스 근거는 종목 마스터를 만든 뒤 증분 적재합니다. 뉴스는 NAVER API HUB가
제공하는 제목·요약·원문 링크만 저장하며 언론사 본문을 별도로 크롤링하지 않습니다.

```bash
# 종목 마스터 — DART 고유번호·시장·섹터를 먼저 적재
python -m ingest.instruments
python -m ingest.prices
python -m app.rag.dart --tickers 005930,000660 --days 30
python -m ingest.news --tickers 005930,000660 --days 7
python -m app.rag.search --backfill
```

마지막 임베딩 백필을 빼먹으면 `documents`는 차도 검색 결과는 0건이다.

```bash
# 사용자별 브리핑 생성
python -m ingest.briefings
# 특정 사용자 또는 재생성이 필요할 때
python -m ingest.briefings --users user_a,user_b --force
```

브리핑 배치는 같은 사용자의 포트폴리오 진단과 수익률 원인 분석(1d)도 함께 미리 만든다. 사용자별 실패를 기본 3회까지 지수 백오프로 재시도한다. 종료 시 대상,
생성, 캐시, 빈 결과, 실패, 건너뜀, 재시도 횟수와 소요 시간을 JSON 한 줄로 출력하므로
배치 로그 수집기가 그대로 운영 지표로 읽을 수 있다. 최종 실패 사용자는 `failed_users`
에서 확인한다.

배치가 쓰는 토큰은 사용자 개인 예산이 아니라 `system:briefing-batch` 장부에서 나간다
(`AI_BATCH_DAILY_TOKEN_BUDGET`). 사용자가 누른 요청이 아니므로 개인 몫을 깎지 않되,
상한 없이 요금이 새지도 않는다. 예산이 소진되면 재시도 없이 그 자리에서 멈추고
`budget_exhausted=true`와 함께 남은 대상 수를 `skipped`로 보고하며 종료 코드 1을
돌려준다.

> 컨테이너 런타임은 **podman**을 씁니다. `compose.yaml`은 표준 Compose 규격이라
> Docker를 쓰는 팀원은 `docker compose up -d`로 그대로 사용할 수 있습니다.

---

## 에이전트로 작업할 때

작업 규율(커밋 단위, 탐침 우선, 읽기 범위, 완료 정의)은 개인 에이전트 설정에 두고
저장소에는 두지 않는다. 팀 저장소로 이관하면서 개인 설정 문서는 제외했다.
AI 파트는 `ai/` 안에서만 작업한다. `backend/`, `frontend/`, `infra/`, `docs/` 와
저장소 루트는 읽기 전용이다.

## 평가

```bash
python -m eval.run --list       # 검색 평가셋 요약
python -m eval.run --metrics    # 지표 자체 점검 (키 불필요, CI에 포함)
python -m eval.run --retrieval  # 검색 정확도 (임베딩 키 필요)
python -m eval.run --feedback   # 최근 30일 프롬프트 버전별 사용자 평가
python -m eval.run --feedback --baseline prompt_old --candidate prompt_new
```

자동 지표는 셋뿐이다 — `Numerical Accuracy` · `Groundedness` ·
`Portfolio Accuracy`. 응답 정책의 목록은 열한 개지만 초기에 다 만들면 어느 것도
제대로 쓰지 못한다. 이 셋은 LLM 판정 없이 계산돼 CI에서 돈다.

검색 평가셋은 **우리 코퍼스에 실제로 답이 있는 질문만** 담는다. 코퍼스를 다시
적재하면 `eval/retrieval.yaml`도 같이 손봐야 한다.

모든 새 AI 응답 로그에는 사용한 프롬프트 파일의 해시가 `prompt_version`으로
기록된다. 피드백 통계는 승인율·피드백률·오류 사유 분포를 이 버전별로 묶으며,
`--baseline`과 `--candidate`를 함께 주면 두 버전의 변화량을 비교한다.

## 브랜치 전략

팀 저장소 기본 브랜치는 `master` 입니다. AI 파트는 **에픽 단위**로 브랜치를 하나 열고,
그 아래 스토리들을 커밋으로 누적한 뒤 에픽 작업이 끝나면 MR 을 한 번 엽니다.

```
master
 └── FINCH-4-ai-service      ← 에픽 FINCH-4 "AI 서비스"
      ├── feat: AI 파트 로컬 검사 스크립트 추가          (FINCH-67)
      └── docs: 이관 이후 낡은 AI 파트 문서 정정         (FINCH-68)
```

스토리마다 Jira 티켓은 만들되 MR 은 열지 않습니다. 브랜치가 오래 사는 만큼
작업 중 주기적으로 `origin/master` 를 반영하고, 스토리별로 커밋을 분리해
리뷰어가 단위로 읽을 수 있게 합니다.

커밋 메시지는 팀 컨벤션(`<타입>: <제목>`)을 따릅니다. **이모지를 붙이지 않습니다**
(2026-08-20 팀 회의 확정) — 저장소 루트의 `docs/convention/gitConvention.md` 를
참고하세요. 이관 전 커밋들이 쓰던 이모지·scope 붙은 형식은 그때의 기록이므로 그대로 둡니다.

자세한 규칙은 [CONTRIBUTING.md](CONTRIBUTING.md) 를 참고하세요.


### 적재 실패 진단

`python -m ingest.financials`는 DART `013`(데이터 없음)을 정상적인 미제출로
집계한다. 다른 비정상 상태는 `failed`로 집계하고, 조회 대상 전부가 같은 비정상
상태로 거부되면 상태 코드와 함께 0이 아닌 종료 코드로 배치를 실패시킨다.
일부 종목만 실패한 경우 나머지 종목은 계속 적재한다.

`GET /health`의 `ingest` 불리언은 기존처럼 행의 존재 여부이며, 조회하지 못한
항목도 `false`다. `ingest_probe_errors`는 DB 조회 실패로 확인하지 못한 항목의
이름을 담는다. 빈 테이블을 정상 조회했다면 이 목록에 포함하지 않는다.
모두 비어 있을 때와 DB 장애일 때 모두 HTTP 200 / `degraded`를 유지한다.
