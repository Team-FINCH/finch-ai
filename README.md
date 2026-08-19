# AI 투자 비서 — AI 파트

국내 주식 투자 앱에 얹는 Personal Investment Copilot의 **AI 파트 저장소**입니다.
사용자의 포트폴리오와 신뢰할 수 있는 금융 데이터를 연결해, 투자 상황을 이해하고 설명합니다.

> **Calculation은 Engine이 하고, Explanation은 AI가 한다.**
> LLM은 어떤 수치도 스스로 만들지 않습니다.

---

## 설계 문서

작업 전에 해당 문서를 먼저 확인하세요. 세 문서는 서로 물려 있습니다.

| 문서 | 내용 | 소스 |
|---|---|---|
| [API 명세](https://claude.ai/code/artifact/84ddb8b2-2e3b-4d90-90bf-e56af4459aad) | 프론트 계약, 외부 의존과 자체 조달 범위 | `docs/api-spec.html` |
| [엔진 산식](https://claude.ai/code/artifact/d03728c6-e9a9-470b-b6a4-93ec26fa71c5) | Portfolio · Risk · Attribution 계산식, 검증 항등식 | `docs/engine-formulas.html` |
| [응답 정책](https://claude.ai/code/artifact/8e53d1f6-4067-49f8-8304-d2bbd6368036) | 프롬프트 계층, 답변 구조, Guardrail, 모델 라우팅 | `docs/prompt-policy.html` |

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
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # 키 채우기
uvicorn app.api.main:app --reload
```

필요한 외부 키는 `.env.example`를 참고하세요. **키는 절대 커밋하지 않습니다.**

---

## 브랜치 전략

현재는 AI 파트 전용 임시 저장소이므로 `main` + 작업 브랜치로 운영합니다.
팀 저장소로 이관하면 `develop`을 추가합니다. 자세한 규칙은 [CONTRIBUTING.md](CONTRIBUTING.md)를 참고하세요.

```
main
 ├── feat/engine-portfolio
 ├── feat/rag-dart-ingest
 └── fix/hhi-cash-normalize
```
