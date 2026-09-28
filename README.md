# FINCH AI

**숫자를 지어내지 않는 금융 LLM 서버** — [FINCH](https://github.com/Team-FINCH/finch-docs) 의 AI 파트입니다.

![Python](https://img.shields.io/badge/Python_3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![pgvector](https://img.shields.io/badge/pgvector-4169E1?logo=postgresql&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-412991?logo=openai&logoColor=white)
![tests](https://img.shields.io/badge/tests-708-brightgreen)

> 담당: 김세민 [@tpals0409](https://github.com/tpals0409) · 커밋 비중 75%

## 핵심 과제

금융 서비스에서 LLM 의 두 가지 실패 — **숫자 환각**과 **투자 권유** — 를 프롬프트가 아니라 **구조로** 막는 것.

## 어떻게 풀었나

### 1. LLM 은 숫자 대신 자리표시자를 씁니다

```
엔진 계산   return_005930 = +2.48%
LLM 출력    "삼성전자는 {{return_005930}} 올랐습니다."
서버 치환   "삼성전자는 +2.48% 올랐습니다."
```

수익률·비중·기여도는 Python 계산 엔진이 만들고, LLM 은 허용된 key 목록 안에서만 문장을 씁니다.
응답은 텍스트·수치 조각(`segments`)으로 나뉘어 나가서 프론트가 수치만 따로 강조할 수 있습니다.

### 2. 출력 가드레일 10종

| 검사 | 막는 것 |
|---|---|
| 원시 숫자 | 자리표시자 없이 쓴 비율·금액·수량 |
| 자리표시자 | 미치환, 허용 목록 밖 key, 단위 중복(`41%%`) |
| 엔진 값 | 엔진 결과와 다른 수치 |
| 인용 무결성 | 존재하지 않는 근거 각주 |
| 금지 표현 | 인과 단정 · 확률 표현 · 매수/매도 권유 · 종목 우열 |
| 톤 · 길이 · 스키마 | 기능별 문장 수, JSON 스키마 위반 |

위반 사유를 붙여 **재생성**하고, 그래도 실패하면 **차단**합니다.

### 3. 도구를 고르는 채팅 에이전트

질문을 보고 필요한 도구를 **한 턴에 병렬로** 호출한 뒤, 모은 근거만 들고 답변을 생성합니다.

`get_portfolio` · `calc_attribution` (수익률 분해) · `get_price_history` · `search_news` · `search_filings` · `get_financials` · `get_wiki`

### 4. RAG 데이터 파이프라인

| 소스 | 주기 | 처리 |
|---|---|---|
| 네이버 뉴스 | 매일 06시 · 평일 13시 | 중복 제거 → 청크 → 임베딩 → 이벤트 승격 |
| DART 공시 | 매일 | 유형별 범위 분리, 신규 종목 1년 백필 |
| 시세 | 장중 매시 | 일봉 적재 |

`text-embedding-3-small` (1024차원) + pgvector 로 검색합니다.

## 기능

데일리 브리핑 · AI 채팅 · 포트폴리오 진단 · 수익률 원인 분석 · 종목 분석 · 주문 전 점검 · 매수 이유 위키

## 운영하며 해결한 문제

- **답변이 "확인되지 않았습니다"로만 나오던 문제** — 원장 경로에 따라 도구가 수치를 자리표시자로 올리지 않던 근본 원인을 찾아 수정
- **채팅 응답 지연** — 모델 교체(`gpt-6-luna`)와 도구 턴 추론 비활성화, 도구 병렬 호출 유도
- **가드레일 오탐** — 운영 폐기 사유를 집계해 1위 원인(각주 위치로 문장 분리 실패)을 수정
- **측정 오염** — 실패 응답 캐시가 A/B 실험을 가리던 문제를 찾아 캐시 우회 경로 추가

## 구조

```
app/
├── api/routes/   기능별 엔드포인트
├── engines/      수익률·위험 지표 계산 (LLM 미사용)
├── llm/
│   ├── agent.py     도구 선택 → 서술 2단계 에이전트
│   ├── tools.py     에이전트 도구
│   ├── prompts/     기능별 프롬프트
│   └── guard/       출력 가드레일
└── rag/          검색 · 임베딩
ingest/           뉴스 · 공시 · 시세 수집 배치
eval/             검색 품질 평가
```

## 실행

```bash
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn app.api.main:app --reload
```

개발 환경·배치·평가 상세는 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md), 설계 문서는 [docs/](docs/) 에 있습니다.
