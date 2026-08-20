AI Part · Interface Contract

# AI 투자 비서 API 명세

AI 파트가 프론트엔드에 제공하는 6개 기능의 인터페이스, 그리고 그 기능들이 성립하기 위해 백엔드에 요구하는 데이터 계약을 함께 정의한다. 국내 주식 전용, 앱 내 가상 포트폴리오를 전제로 한다.

**Base** /api/ai/v1 · **Auth** Bearer JWT · **Format** JSON · UTF-8 · **Runtime** FastAPI (별도 서비스)

## §1 개요와 전제

AI 서비스는 백엔드와 분리된 독립 서버로 동작한다. 포트폴리오·거래 원장은 백엔드가 소유하고 AI 서비스는 이를 읽기만 하며, 계산·검색·생성·검증은 전부 AI 파트 안에서 끝난다.

> **분리 원칙**
> **AI 기능의 구현은 AI 파트가 전부 소유한다.** 다른 파트에 계산이나 조립 로직을 요구하지 않고, 신규 화면 개발도 요청하지 않는다. 프론트는 완성된 응답을 그대로 출력하고, 백엔드는 기존 원장을 읽게 해줄 뿐이다. 이 원칙은 편의가 아니라 **책임 소재**의 문제다 — 숫자가 틀리거나 근거가 어긋났을 때 원인이 AI 파트 밖에 있으면 품질을 통제할 수 없다.

#### 확정 전제

| 항목 | 확정 내용 |
| --- | --- |
| 시장 범위 | 국내 주식 전용 (KOSPI · KOSDAQ). 해외 종목 요청은 `UNSUPPORTED_MARKET` |
| 포트폴리오 | 앱 내 가상 포트폴리오. 실계좌·마이데이터 연동 없음 |
| 계산 주체 | Attribution · Risk 엔진 모두 AI 파트가 구현 |
| 통화 | KRW 단일. 환율·국가 노출 분석 없음 |
| 데이터 원천 | KIS OpenAPI(시세) · pykrx(히스토리) · DART(공시·재무) · ECOS(거시) |
| 제외 항목 | 애널리스트 컨센서스, 목표주가 — 무료 소스 부재 및 규제 리스크 |

> **설계 원칙**
> LLM이 생성한 문장에는 수치가 직접 들어가지 않는다. 엔진이 계산한 값을 **서버에서 치환하고 대조 검증까지 마친 뒤** 완성된 문장을 반환한다. 숫자 정확도를 프롬프트가 아니라 파이프라인 수준에서 보장하며, 클라이언트는 어떤 조립도 하지 않는다. 상세는 [§2.3](#narrative).

## §1.1 기능 · 엔드포인트 대응

사용자 대면 기능은 6개이고, 그 아래에 계산 엔진 3종과 단일 설명 레이어가 있다. 기능은 엔진의 조합을 특정 화면에서 특정 프롬프트로 호출한 결과다.

```
                    ┌──────────────────────┐
                    │  Explanation Layer   │   프롬프트만 다른 단일 생성기
                    └──────────┬───────────┘
           ┌───────────────────┼───────────────────┐
    ┌──────┴──────┐    ┌───────┴──────┐    ┌───────┴───────┐
    │ Attribution │    │ Risk Engine  │    │  Event / RAG  │
    │   Engine    │    │              │    │    Engine     │
    └─────────────┘    └──────────────┘    └───────────────┘
           └───────────────────┼───────────────────┘
                    ┌──────────┴───────────┐
                    │   User Context Layer │   Portfolio · Trades · Wiki
                    └──────────────────────┘
```

| 기능 | 화면 | 엔드포인트 | 호출 엔진 |
| --- | --- | --- | --- |
| AI 종목 분석 | 종목 상세 | `POST /stocks/{ticker}/analysis` | Event/RAG + Wiki |
| Ask My Portfolio | 채팅 | `POST /chat` | 전부 (Tool) |
| 포트폴리오 진단 | 포트폴리오 | `POST /portfolio/diagnosis` | Risk |
| 수익률 원인 분석 | 홈 · 포트폴리오 | `POST /portfolio/attribution` | Attribution + Event |
| 주문 전 점검 | 주문 | `POST /orders/preview` | Risk × 2 diff |
| 데일리 브리핑 | 홈 | `GET /briefing` | 전부 |

**투자 논지 점검(Thesis Check)은 독립 엔드포인트가 아니다.** 동일 종목·동일 RAG를 쓰므로 종목 분석의 `thesis_check` 섹션으로 흡수했다. 마찬가지로 주문 전 점검은 진단 엔진을 두 번 호출한 차분이므로 별도 시뮬레이션 엔진을 두지 않는다.

## §2 공통 규약

### 2.1 기본 규약

| 항목 | 규약 |
| --- | --- |
| Base URL | `/api/ai/v1` |
| 인증 | `Authorization: Bearer <JWT>` — 사용자 식별자는 토큰에서 추출하며 **경로·본문에 넣지 않는다** |
| 시각 | ISO 8601, KST 오프셋 명시 (`2026-08-19T14:32:09+09:00`) |
| 비율 | **0~1 사이 소수**로 전달 (`0.4168`). 백분율 변환은 `metrics.display`가 담당 |
| 금액 | 원 단위 정수. 소수점 없음 |
| 종목코드 | 6자리 문자열 (`"005930"`). 선행 0 유실 방지를 위해 정수 금지 |
| 언어 | `Accept-Language: ko-KR` 고정. 다국어 미지원 |
| 멱등성 | 생성형 POST는 멱등하지 않음. 재시도 시 `Idempotency-Key` 헤더 권장 |

### 2.2 공통 응답 봉투

모든 성공 응답은 아래 봉투를 공유한다. `content`만 엔드포인트별로 달라진다.

```
{
  "request_id": "req_01JQZ8M3T7K2",
  "generated_at": "2026-08-19T14:32:11+09:00",
  "data_as_of": {
    "price":     "2026-08-19T14:30:00+09:00",
    "portfolio": "2026-08-19T14:32:09+09:00",
    "filings":   "2026-08-19T09:00:00+09:00",
    "news":      "2026-08-19T13:50:00+09:00"
  },
  "model": "claude-opus-5",
  "cached": false,
  "content": { },
  "citations": [ ],
  "disclaimer": "본 정보는 투자 판단을 돕기 위한 참고 자료이며 투자 권유가 아닙니다."
}
```

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `request_id` | string | 피드백·로그 추적 키. 프론트는 이 값을 `POST /feedback`에 그대로 전달 |
| `data_as_of` | object | **데이터 원천별 기준 시각.** UI에 반드시 노출한다. 시세는 지연될 수 있으므로 생성 시각과 별도로 관리 |
| `cached` | boolean | 응답 전체 또는 일부가 캐시에서 왔는지 |
| `disclaimer` | string | 고지 문구. 하드코딩하지 말고 응답 값을 표시할 것 (규제 문구 변경 대응) |

### 2.3 서술과 수치의 분리 — 핵심 규약

LLM은 계산을 시키지 않아도 *주어진 숫자를 반올림하거나 바꾼다.* `18.3%`를 "약 18%"로, `+0.87%p`를 "+0.9%p"로 바꾸는 일은 흔하게 발생한다. AI 서비스는 내부적으로 LLM에게 자리표시자만 쓰게 한 뒤 엔진 계산값으로 치환하고, 원본과 대조 검증한 문장을 반환한다. **치환과 검증이 서버에서 끝나므로 클라이언트는 받은 문자열을 그대로 출력하면 된다.**

```
{
  "text": "반도체 관련 자산이 포트폴리오의 42.3%를 차지합니다. 종목은 3개로 나뉘어 있으나 동일한 업황 사이클에 함께 노출되어 있어 분산 효과는 제한적입니다.",
  "segments": [
    { "type": "text",   "value": "반도체 관련 자산이 포트폴리오의 " },
    { "type": "metric", "value": "42.3%", "raw": 0.423, "unit": "ratio",
      "source": "risk_engine", "direction": null },
    { "type": "text",   "value": "를 차지합니다. 종목은 " },
    { "type": "metric", "value": "3개", "raw": 3, "unit": "count",
      "source": "portfolio_engine", "direction": null },
    { "type": "text",   "value": "로 나뉘어 있으나 동일한 업황 사이클에 …" }
  ]
}
```

| 필드 | 용도 |
| --- | --- |
| `text` | **완성된 문장.** 이것만 출력해도 정상 동작한다. 기본 렌더링 경로 |
| `segments` | 선택 사항. 등락 색·강조·툴팁 등 수치 스타일링이 필요할 때만 순회한다 |
| `segments[].raw` | 원시 값. 차트·정렬 등 2차 가공이 필요할 때 사용 |
| `segments[].direction` | `up` / `down`. 국내 관례에 따라 각각 적색·청색 |

`segments`는 `text`를 잘라 놓은 것일 뿐 다른 정보가 아니다. 이어 붙이면 `text`와 정확히 일치한다. 프론트가 `segments`를 무시해도 표시 내용은 동일하며, **숫자 정확도는 어느 쪽을 쓰든 서버가 보장한다.**

#### 서버 내부 검증 — 클라이언트와 무관

치환 직후 AI 서비스가 자체 검사한다. 위반 시 응답을 내보내지 않고 재생성하며, 재시도 후에도 실패하면 `GUARDRAIL_BLOCKED`로 처리한다.

| 검사 | 기준 |
| --- | --- |
| 미치환 자리표시자 | `{{ }}`가 남아 있으면 차단 |
| 비율 · 금액 · 수량 · 배수 | 엔진이 내보내지 않은 수치가 문장에 있으면 차단 (`42.3%`, `1,200만 원`, `2배`) |
| 연도 · 분기 · 서수 · 종목코드 | 허용 (`2026년`, `3분기`, `첫 번째`, `005930`) |
| 엔진 값 대조 | `segments[].raw`가 엔진 출력과 부동소수점 오차 내에서 일치하는지 확인 |

### 2.4 citations

`narrative` 안에서 `[^cit_2]` 형태로 참조한다. 사실 주장에는 근거를 붙이고, 엔진 계산값은 `type: "engine"`으로 표기한다.

```
"citations": [
  {
    "id": "cit_2",
    "type": "filing",
    "title": "단일판매·공급계약 체결",
    "source": "DART",
    "publisher": "삼성전자",
    "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260814000342",
    "published_at": "2026-08-14T16:12:00+09:00",
    "snippet": "계약금액은 최근 매출액 대비 …",
    "relevance": 0.87
  }
]
```

| `type` | 의미 |
| --- | --- |
| `filing` | DART 공시 원문 |
| `financial` | 재무제표 항목 |
| `news` | 언론 기사 |
| `price` | 시세 · 거래 데이터 |
| `macro` | ECOS 거시지표 |
| `engine` | 자체 계산 결과 (외부 URL 없음) |
| `wiki` | 사용자가 기록한 투자 논지 |

### 2.5 SSE 스트리밍

`Accept: text/event-stream`을 보내면 스트리밍으로 응답한다. 대화와 종목 분석이 지원한다.

```
event: meta
data: {"request_id":"req_01JQZ8M3T7K2","data_as_of":{...}}

event: tool_call
data: {"tool":"calc_risk_metrics","status":"running","label":"포트폴리오 위험도 계산 중"}

event: tool_call
data: {"tool":"calc_risk_metrics","status":"done","duration_ms":412}

event: delta
data: {"text":"반도체 관련 자산이 포트폴리오의 "}

event: delta
data: {"text":"42.3%","segment":{"type":"metric","raw":0.423,"unit":"ratio",
       "source":"risk_engine","direction":null}}

event: delta
data: {"text":"를 차지합니다."}

event: citations
data: [{"id":"cit_1","type":"engine","title":"섹터 집중도 계산"}]

event: done
data: {"finish_reason":"stop","tokens":{"input":8412,"output":623}}
```

`tool_call`을 노출하는 이유는 대기 구간의 UX 때문이다. 도구 호출에 수 초가 걸리므로 `label`을 그대로 로딩 문구로 표시한다. **`delta`는 이미 치환이 끝난 문자열**이므로 클라이언트는 이어 붙이기만 하면 된다. 수치가 포함된 조각에는 `segment`가 함께 실려 오며 스타일링이 필요할 때만 쓴다. 서버는 자리표시자가 열려 있는 동안 출력을 보류했다가 치환 후 내보내므로 `{{ }}`가 클라이언트에 노출되지 않는다.

### 2.6 에러 · 캐시 · 호출 한도

```
{
  "error": {
    "code": "INSUFFICIENT_DATA",
    "message": "위험 지표를 계산하기에 가격 히스토리가 부족합니다.",
    "detail": { "required_days": 60, "available_days": 23, "ticker": "462870" }
  },
  "request_id": "req_01JQZ8M3T7K2"
}
```

| HTTP | code | 발생 조건 | 프론트 처리 |
| --- | --- | --- | --- |
| 400 | `INVALID_REQUEST` | 스키마 위반 | 개발 오류. 사용자 노출 금지 |
| 400 | `UNSUPPORTED_MARKET` | 해외 종목 요청 | "현재 국내 종목만 지원합니다" |
| 401 | `UNAUTHORIZED` | 토큰 만료·누락 | 재로그인 |
| 404 | `INSTRUMENT_NOT_FOUND` | 미상장·폐지 종목 | 종목 페이지 자체 처리 |
| 409 | `INSUFFICIENT_DATA` | 보유 종목 0개, 가격 히스토리 60거래일 미만 | **AI 영역만 대체 문구로 숨김.** 화면 전체 실패 아님 |
| 422 | `GUARDRAIL_BLOCKED` | 투자 권유·가격 예측 요구, 수치 검증 실패 | 차단 사유 문구 표시 |
| 429 | `RATE_LIMITED` | 호출 한도 초과 | `Retry-After` 헤더만큼 대기 |
| 502 | `RETRIEVAL_FAILED` | DART·뉴스 검색 실패 | 재시도 버튼 |
| 504 | `LLM_TIMEOUT` | 생성 시간 초과 (30초) | 재시도 버튼 |

> **중요**
> `INSUFFICIENT_DATA`는 정상 상황이다. 신규 가입자는 보유 종목이 없고, 신규 상장 종목은 히스토리가 짧다. **AI 영역만 비활성 상태로 표시하고 기존 화면 기능은 그대로 동작해야 한다.**

#### 캐시 및 호출 한도

| 엔드포인트 | 캐시 | 한도 (사용자당) |
| --- | --- | --- |
| `/stocks/{ticker}/analysis` | 공통 섹션 종목 단위 6시간 · 개인화 섹션 없음 | 10회 / 분 |
| `/chat` | 없음 | 20회 / 분 |
| `/portfolio/diagnosis` | 사용자 단위 15분 | 10회 / 분 |
| `/portfolio/attribution` | 사용자 · 기간 단위 15분 | 10회 / 분 |
| `/orders/preview` | 없음 | 30회 / 분 |
| `/briefing` | 일 1회 배치 생성 | 60회 / 분 |

## §3 종목 AI 분석

**POST** `/api/ai/v1/stocks/{ticker}/analysis`  —  Phase 1

종목 상세 화면의 AI 분석. 투자 논지 점검을 섹션으로 포함한다. SSE 지원.

#### Request

```
{
  "sections": ["current", "changes", "attention", "risks",
               "my_impact", "thesis_check", "next_events"],
  "personalize": true
}
```

`sections`를 생략하면 전체를 반환한다. 비보유 종목에서 `my_impact`·`thesis_check`를 요청하면 해당 섹션은 `null`로 반환되며 에러가 아니다.

#### Response — content

```
{
  "ticker": "005930",
  "name": "삼성전자",
  "sections": {
    "current": {
      "title": "현재 상황",
      "text": "메모리 업황 회복 국면에서 …",
      "segments": [ ],
      "cached": true,
      "cached_at": "2026-08-19T09:00:00+09:00"
    },
    "changes":   { "title": "최근 변화" },
    "attention": { "title": "시장이 주목하는 요인" },
    "risks":     { "title": "확인된 위험 요인" },
    "my_impact": {
      "title": "내 포트폴리오 영향",
      "text": "이 종목은 포트폴리오의 41.7%를 차지하며 평균 매입가 대비 +10.1% 상태입니다.",
      "segments": [
        { "type": "text",   "value": "이 종목은 포트폴리오의 " },
        { "type": "metric", "value": "41.7%", "raw": 0.4168, "unit": "ratio",
          "source": "portfolio_engine", "direction": null },
        { "type": "text",   "value": "를 차지하며 평균 매입가 대비 " },
        { "type": "metric", "value": "+10.1%", "raw": 0.1011, "unit": "ratio",
          "source": "portfolio_engine", "direction": "up" },
        { "type": "text",   "value": " 상태입니다." }
      ],
      "cached": false
    },
    "thesis_check": {
      "title": "투자 논지 점검",
      "thesis": {
        "text": "HBM 구조적 성장에 베팅",
        "recorded_at": "2026-03-11T10:22:00+09:00",
        "source": "user_stated"
      },
      "supporting": [
        { "summary": "3분기 HBM 매출 비중 확대", "citation_id": "cit_1" }
      ],
      "challenging": [
        { "summary": "파운드리 부문 적자 지속", "citation_id": "cit_3" }
      ],
      "text": "매수 시점에 언급하신 근거와 관련해 …",
      "segments": [ ]
    },
    "next_events": {
      "title": "다음에 확인할 일정",
      "events": [
        { "type": "earnings", "label": "3분기 실적 발표", "date": "2026-10-08", "confirmed": false },
        { "type": "dividend", "label": "분기 배당 기준일", "date": "2026-09-30", "confirmed": true }
      ]
    }
  }
}
```

> **비용 설계**
> `current`·`changes`·`attention`·`risks`·`next_events`는 사용자와 무관하므로 **종목 단위로 캐시**한다(TTL 6시간). `my_impact`·`thesis_check`만 사용자별로 생성한다. 이 분리가 없으면 인기 종목에서 동일 분석을 수천 번 재생성한다.

`attention`·`risks`의 명칭은 규제 대응이다. "긍정 요인 / 부정 요인"은 의견 제시로 읽힐 수 있어 **시장이 주목하는 요인 / 공시·실적에서 확인된 위험 요인**이라는 출처 귀속형으로 고정한다. 목표주가와 투자의견은 어떤 섹션에서도 생성하지 않는다.

## §4 AI 대화

**POST** `/api/ai/v1/chat`  —  SSE · Phase 1

유일한 도구 호출 에이전트. 나머지 기능의 파이프라인을 Tool로 노출해 재사용한다.

#### Request

```
{
  "conversation_id": "conv_01JQZ7X4M9",
  "message": "엔비디아 말고 삼성전자 비중이 너무 높은 거 아냐?",
  "context": {
    "screen": "stock_detail",
    "ticker": "005930"
  }
}
```

`conversation_id`를 생략하면 새 대화를 시작하고 응답 `meta` 이벤트로 발급한다. `context`는 화면 맥락으로, 사용자가 "이거 어때?"처럼 대명사로 물을 때 지시 대상을 해소한다. `screen`은 `home · portfolio · stock_detail · order · chat` 중 하나.

#### 에이전트 Tool 목록

여섯 기능이 공유하는 도구다. 각 기능의 파이프라인을 그대로 Tool 시그니처로 노출했다.

| Tool | 반환 | Phase |
| --- | --- | --- |
| `get_portfolio` | 보유 종목·비중·현금·평가손익 | 1 |
| `get_price_history` | 일별 종가·거래량 시계열 | 1 |
| `search_filings` | DART 공시 검색 결과 | 1 |
| `search_news` | 뉴스 검색 결과 | 1 |
| `get_financials` | 재무제표 항목 | 1 |
| `get_wiki` | 투자 성향·논지 | 1 |
| `calc_risk_metrics` | 집중도·변동성·상관관계 | 2 |
| `simulate_order` | 주문 전후 지표 차분 | 2 |
| `calc_attribution` | 수익 기여도 분해 | 3 |

> **구현 순서**
> 각 기능을 만들 때 **내부 로직을 처음부터 Tool 시그니처로 작성**하면 대화 기능은 별도 개발 없이 완성된다. 기능을 먼저 만들고 나중에 Tool로 감싸면 같은 로직을 두 번 만들게 된다.

## §5 포트폴리오 진단

**POST** `/api/ai/v1/portfolio/diagnosis`  —  Phase 2

위험 지표를 계산하고 상위 항목을 설명한다. 지표 계산은 Risk Engine이, 문장은 LLM이 담당한다.

#### Response — content

```
{
  "risk_level": "high",
  "risk_score": 72,
  "summary": {
    "text": "가장 큰 위험은 반도체 업종 집중도입니다. …",
    "segments": [ ]
  },
  "findings": [
    {
      "id": "sector_concentration",
      "category": "concentration",
      "severity": "high",
      "title": "반도체 업종 집중",
      "text": "삼성전자, SK하이닉스, 한미반도체를 합산하면 42.3%입니다. …",
      "segments": [
        { "type": "text",   "value": "삼성전자, SK하이닉스, 한미반도체를 합산하면 " },
        { "type": "metric", "value": "42.3%", "raw": 0.423, "unit": "ratio",
          "source": "risk_engine", "direction": null },
        { "type": "text",   "value": "입니다. …" }
      ],
      "evidence": {
        "tickers": ["005930", "000660", "042700"],
        "hhi": 0.2841,
        "avg_pairwise_corr": 0.78
      }
    }
  ],
  "indicators": {
    "hhi": 0.2841,
    "top1_weight": 0.4168,
    "top3_weight": 0.7204,
    "annualized_volatility": 0.2837,
    "max_drawdown_1y": -0.2214,
    "cash_ratio": 0.081,
    "rate_sensitivity": "high"
  }
}
```

| 필드 | 값 | 설명 |
| --- | --- | --- |
| `risk_level` | `low · moderate · high` | 규칙 엔진 판정. LLM이 정하지 않는다 |
| `risk_score` | 0–100 정수 | 구성 지표 가중합. 산식은 별도 문서 |
| `findings[].severity` | `info · medium · high` | 정렬 순서가 곧 중요도 순위 |
| `evidence` | object | LLM 입력으로 쓰인 원시 지표. 디버깅·평가용으로 응답에 포함 |

`findings[].category`는 `concentration · volatility · correlation · style_tilt · macro_exposure · liquidity` 중 하나다. 국가 집중도와 통화 노출은 국내 단일 시장이므로 정의하지 않는다.

## §6 수익률 원인 분석

**POST** `/api/ai/v1/portfolio/attribution`  —  Phase 3

수익률을 시장·섹터·종목 선택으로 분해하고 주요 기여 종목에 이벤트를 연결한다.

#### Request

```
{ "period": "1d", "benchmark": "KOSPI" }
```

`period`는 `1d · 1w · 1m · 3m · ytd`. `benchmark`는 `KOSPI · KOSDAQ`이며 생략 시 보유 비중 가중으로 자동 결정한다.

#### Response — content

```
{
  "period": "1d",
  "total_return": 0.0213,
  "benchmark_return": 0.0140,
  "excess_return": 0.0073,
  "breakdown": {
    "market":    0.0140,
    "sector":    0.0031,
    "selection": 0.0042
  },
  "contributors": [
    {
      "ticker": "000660",
      "name": "SK하이닉스",
      "weight": 0.1820,
      "return": 0.0512,
      "contribution": 0.0093,
      "events": [
        { "citation_id": "cit_1", "type": "news",
          "summary": "HBM 공급 계약 관련 보도", "matched_confidence": 0.72 }
      ]
    }
  ],
  "detractors": [ ],
  "text": "오늘 상승분의 상당 부분은 시장 전체 상승에서 왔습니다. …",
  "segments": [ ]
}
```

> **기획서 대비 변경**
> 원안의 **환율(FX) 기여도 항목을 제거**했다. 국내 단일 시장에서는 성립하지 않는다. 대신 `market` / `selection` 분해를 넣어 "장이 좋았던 것인지, 종목 선택이 좋았던 것인지"에 답한다. 국내 사용자에게 체감 가치가 더 크다.

`matched_confidence`는 가격 변동과 이벤트의 연결 강도다. **0.6 미만이면 인과 표현을 쓰지 않는다.** LLM 프롬프트에서 "때문입니다" 대신 "같은 날 다음 소식이 있었습니다" 형태로 강제한다.

## §7 주문 전 점검

**POST** `/api/ai/v1/orders/preview`  —  Phase 2

주문 체결을 가정한 포트폴리오에 진단 엔진을 재실행하고 차분을 반환한다. 승인·거절 판단은 하지 않는다.

#### Request

```
{
  "orders": [
    { "ticker": "000660", "side": "buy", "quantity": 40, "price": 214000 }
  ]
}
```

배열로 받아 리밸런싱 시나리오(동시 매수·매도)를 지원한다. `price`를 생략하면 현재가로 계산한다.

#### Response — content

```
{
  "orders_value": 8560000,
  "feasible": true,
  "changes": [
    {
      "key": "ticker_weight",
      "label": "SK하이닉스 비중",
      "before": 0.1820, "after": 0.2634, "delta": 0.0814,
      "significance": "high"
    },
    {
      "key": "sector_weight_semiconductor",
      "label": "반도체 업종 비중",
      "before": 0.4230, "after": 0.5044, "delta": 0.0814,
      "significance": "high"
    },
    { "key": "cash_ratio", "label": "현금 비중",
      "before": 0.0810, "after": 0.0593, "delta": -0.0217, "significance": "medium" },
    { "key": "hhi", "label": "집중도",
      "before": 0.2841, "after": 0.3392, "delta": 0.0551, "significance": "high" },
    { "key": "annualized_volatility", "label": "예상 변동성",
      "before": 0.2837, "after": 0.3105, "delta": 0.0268, "significance": "medium" }
  ],
  "wiki_conflicts": [
    {
      "fact": "분산 투자를 중시하며 단일 업종 40% 이상을 피하고 싶다고 하셨습니다",
      "source": "user_stated",
      "recorded_at": "2026-04-02T20:11:00+09:00",
      "conflict": "이 주문은 반도체 업종 비중을 해당 기준 위로 올립니다"
    }
  ],
  "text": "이 주문을 실행하면 …",
  "segments": [ ]
}
```

`feasible`이 `false`면 현금 부족이며 `detail.shortfall`에 부족액이 담긴다. `wiki_conflicts`는 **사용자가 직접 진술한 항목(`user_stated`)만** 사용한다. AI가 추론한 성향으로 주문에 이의를 제기하면 근거 없는 참견이 된다.

## §8 데일리 브리핑

**GET** `/api/ai/v1/briefing?date=2026-08-19`  —  Phase 3

배치 생성 결과 조회. 생성 트리거가 아니므로 GET이다. `date` 생략 시 당일.

#### Response — content

```
{
  "date": "2026-08-19",
  "status": "ready",
  "generated_at": "2026-08-19T07:30:00+09:00",
  "items": [
    {
      "rank": 1,
      "category": "holding_move",
      "relevance_score": 0.91,
      "title": "SK하이닉스 강세",
      "text": "포트폴리오에서 18.2%를 차지하는 SK하이닉스가 +5.12% 상승했습니다. …",
      "segments": [
        { "type": "text",   "value": "포트폴리오에서 " },
        { "type": "metric", "value": "18.2%", "raw": 0.1820, "unit": "ratio",
          "source": "portfolio_engine", "direction": null },
        { "type": "text",   "value": "를 차지하는 SK하이닉스가 " },
        { "type": "metric", "value": "+5.12%", "raw": 0.0512, "unit": "ratio",
          "source": "price", "direction": "up" },
        { "type": "text",   "value": " 상승했습니다. …" }
      ],
      "related_tickers": ["000660"],
      "deeplink": "/stocks/000660?tab=ai",
      "citations": ["cit_1"]
    }
  ]
}
```

| `status` | 의미 | 프론트 처리 |
| --- | --- | --- |
| `ready` | 생성 완료 | 정상 표시 |
| `generating` | 배치 진행 중 | 스켈레톤 표시 후 30초 뒤 재조회 |
| `empty` | 보유 종목 없음 또는 유의미한 이벤트 없음 | 영역 숨김 |

`category`는 `holding_move · earnings · filing · macro_event · portfolio_shift` 중 하나이며 최대 4건을 반환한다.

> **비용 설계**
> `relevance_score` 산출은 **LLM이 아니라 규칙 엔진**이 한다(보유 여부 × 비중 × 이벤트 중요도). LLM은 상위 4건의 문장만 생성한다. 랭킹까지 LLM에 맡기면 사용자 수에 비례해 비용이 선형 증가한다.

## §9 투자 논지 · Wiki

계산으로 얻을 수 없는 사용자 맥락을 누적한다. **비중·수익률 같은 수치는 절대 저장하지 않는다.** 즉시 낡기 때문이다.

#### 수집 경로 — AI가 직접 묻는다

논지 입력 화면을 다른 파트에 요청하지 않는다. AI 서비스가 거래 이력을 폴링하다 신규 매수를 발견하면 **대화 안에서 AI가 먼저 질문해** 수집한다. 별도 UI 없이 기존 AI 표면만으로 완결된다.

```
신규 매수 감지 (거래 이력 폴링, 5분 주기)
   │
   ├─ 대화 진입 시  →  "SK하이닉스를 새로 담으셨네요. 어떤 점을 보고 결정하셨나요?"
   │                    사용자 답변 → 논지 기록 (source: user_stated)
   │
   └─ 미응답 시     →  종목 분석의 thesis_check 섹션에 입력 유도 카드 노출
                        건너뛰어도 나머지 기능은 그대로 동작
```

논지가 없는 종목은 `thesis_check`가 `null`로 반환될 뿐 오류가 아니다. 수집률이 낮아도 다른 기능이 무너지지 않도록 **선택 정보로만 취급한다.**

**POST** `/api/ai/v1/wiki/theses`  —  Phase 1

논지 기록. 위 대화 흐름에서 AI 서비스가 스스로 호출하며, 클라이언트가 직접 부를 일은 없다.

```
{
  "ticker": "000660",
  "text": "HBM 구조적 성장에 베팅",
  "horizon": "long",
  "linked_trade_id": "trd_01JQZ6P2K8"
}
```

**GET** `/api/ai/v1/wiki`  —  Phase 1

"AI가 이해한 나" 화면. 사용자가 자신의 맥락을 열람하고 수정한다.

```
{
  "profile": [
    {
      "fact_id": "fct_01JQ...",
      "text": "분산 투자를 중시하며 단일 업종 40% 이상을 피하고자 함",
      "source": "user_stated",
      "confidence": "high",
      "as_of": "2026-04-02T20:11:00+09:00",
      "evidence": { "type": "conversation", "ref": "conv_01JQZ3M1" },
      "editable": true
    }
  ],
  "theses": [
    { "ticker": "000660", "name": "SK하이닉스",
      "text": "HBM 구조적 성장에 베팅", "horizon": "long",
      "source": "user_stated", "recorded_at": "2026-03-11T10:22:00+09:00",
      "status": "active" }
  ]
}
```

**PUT** `/api/ai/v1/wiki/theses/{ticker}`  —  Phase 1

논지 수정. 수정 시 `source`는 자동으로 `user_stated`가 된다.

**DELETE** `/api/ai/v1/wiki/facts/{factId}`  —  Phase 1

항목 삭제. 즉시 이후 모든 응답에서 제외된다.

| `source` | 의미 | 응답에서의 취급 |
| --- | --- | --- |
| `user_stated` | 사용자가 직접 진술 | 사실로 인용 가능 |
| `derived_from_trades` | 거래 이력에서 도출 | 사실로 인용 가능 |
| `ai_inferred` | AI가 대화에서 추론 | **단정 금지.** "~하신 것으로 보이는데 맞나요?" 형태만 허용 |

Wiki 갱신은 **대화 종료 후 비동기 배치**로만 수행한다. 응답 경로에서 동기적으로 쓰면 지연이 그대로 사용자에게 전가된다. 동시 세션 충돌은 사용자당 단일 writer 큐로 직렬화한다.

## §10 응답 피드백

**POST** `/api/ai/v1/feedback`  —  Phase 1

AI 품질 지표 수집. 모든 AI 응답 영역에 노출한다.

```
{
  "request_id": "req_01JQZ8M3T7K2",
  "rating": "down",
  "reasons": ["wrong_number", "not_relevant"],
  "comment": "비중이 실제와 다릅니다"
}
```

`reasons`는 `wrong_number · not_relevant · outdated · too_generic · unclear · wrong_citation` 중 복수 선택. 이 분포가 곧 평가 체계의 우선순위가 된다.

## §11 외부 의존과 자체 조달

분리 원칙에 따라 **다른 파트에 신규 개발을 요청하지 않는다.** AI 기능이 필요로 하는 데이터는 이미 존재하는 것을 읽거나, AI 파트가 직접 조달한다.

| 필요 데이터 | 조달 방식 | 타 파트 작업 |
| --- | --- | --- |
| 보유 종목 · 현금 · 평균매입가 | 기존 포트폴리오 조회 API를 서버 간 호출로 소비 | 없음 — 읽기 권한만 |
| 거래 이력 | 기존 거래 조회 API 소비 + 주기 폴링 | 없음 — 읽기 권한만 |
| 종목 마스터 · 섹터 | **자체 구축** — pykrx + KRX 업종분류, 일 1회 동기화 | 없음 |
| DART 고유번호 | **자체 구축** — DART `corpCode.xml` 직접 수집 | 없음 |
| 시세 · 가격 히스토리 | **자체 적재** — KIS OpenAPI · pykrx | 없음 |
| 공시 · 뉴스 · 거시지표 | **자체 적재** — DART · 네이버 · ECOS | 없음 |
| 투자 논지 | **자체 수집** — 대화로 직접 질문 ([§9](#ep-wiki)) | 없음 |

> **타 파트에 요청하는 것**
> 딱 두 가지다. **①** 기존 포트폴리오·거래 조회 API에 대한 서버 간 인증 수단 — 서비스 토큰 또는 읽기 전용 DB 계정. **②** 화면에서 AI 응답이 들어갈 **빈 슬롯**. 슬롯 내부의 로직·상태·에러 처리는 전부 AI 파트가 책임진다.

#### AI 파트가 소유하는 저장소

원장은 백엔드가 소유하고 AI는 읽기만 한다. 반대로 아래는 AI 파트가 온전히 소유하며 다른 파트가 알 필요가 없다.

| 저장소 | 내용 |
| --- | --- |
| `instruments` | 종목 마스터 — 종목코드, 종목명, 시장, 섹터, DART 고유번호 |
| `price_daily` | 일별 시세 — 변동성·상관관계 계산 기반 (최소 60거래일) |
| `documents` · `embeddings` | 공시·뉴스 원문과 벡터 (pgvector) |
| `events` | 실적·공시·거시 일정과 중요도 점수 |
| `wiki` | 사용자 투자 논지·성향 |
| `ai_responses` | 응답 로그 · 피드백 · 평가 데이터셋 |

> **경계선**
> 백엔드가 **파생 지표를 계산해 주겠다고 해도 받지 않는다.** 비중·수익률·집중도를 양쪽이 각각 계산하면 화면마다 값이 어긋나고 정확도의 책임 소재가 사라진다. AI 응답에 등장하는 모든 수치는 AI 파트 엔진에서만 나온다. 원장 조회 응답에 `weight`·`pnl_pct` 같은 파생 필드가 있어도 **무시하고 다시 계산한다.**

#### 거래 감지 — 폴링으로 자체 해결

논지 수집과 브리핑에는 신규 거래 시점을 알아야 한다. 웹훅 개발을 요청하는 대신 거래 조회 API를 주기 폴링하고 `trade_id` 워터마크로 신규 건을 판별한다. 가상 포트폴리오라 거래 빈도가 낮아 5분 주기로 충분하며, **백엔드는 폴링당하는 사실조차 알 필요가 없다.**

#### 병렬 진행 장치

읽기 권한이 늦어져도 AI 파트가 멈추지 않도록, 포트폴리오 조회를 어댑터 한 겹 뒤에 둔다. 초기에는 시드 데이터 어댑터로 개발하고 권한이 열리면 실제 어댑터로 교체한다. **다른 파트의 일정이 AI 파트의 임계 경로에 들어오지 않게 하는 것이 분리 원칙의 실질적 목적이다.**

## §12 공통 타입

#### Segment

```
{
  "type":      "text | metric",
  "value":     "41.7%",
  "raw":       0.4168,
  "unit":      "ratio | krw | count | days | score",
  "source":    "portfolio_engine | risk_engine | attribution_engine | price | filing",
  "direction": "up | down | null"
}
```

`type: "text"`인 조각은 `value`만 가진다. 나머지 필드는 `metric`에서만 채워진다.

#### Section

```
{
  "title":     "string",
  "text":      "string",
  "segments":  [ ],
  "cached":    false,
  "cached_at": "string | null"
}
```

#### Citation

```
{
  "id":           "cit_1",
  "type":         "filing | financial | news | price | macro | engine | wiki",
  "title":        "string",
  "source":       "string",
  "url":          "string | null",
  "published_at": "string | null",
  "snippet":      "string | null",
  "relevance":    0.87
}
```

## §13 단계별 인도 범위

엔진 하나가 완성될 때마다 기능이 쌍으로 나온다. 따라서 단계는 기능이 아니라 엔진 단위로 끊는다.

| 단계 | 완성 엔진 | 인도 엔드포인트 |
| --- | --- | --- |
| **Phase 1** 
Event/RAG + Wiki | Portfolio Engine 
Event/RAG Engine 
User Wiki | `/stocks/{ticker}/analysis` 
`/chat` (Tool 6종) 
`/wiki/*` 
`/feedback` |
| **Phase 2** 
Risk | Risk Engine | `/portfolio/diagnosis` 
`/orders/preview` 
`/chat` Tool 2종 추가 |
| **Phase 3** 
Attribution | Attribution Engine 
Event Ranking | `/portfolio/attribution` 
`/briefing` 
`/chat` Tool 1종 추가 |

주문 전 점검이 Phase 2에 있는 것은 진단 엔진의 차분 계산이라 추가 비용이 거의 없기 때문이다. 브리핑은 세 엔진을 모두 소비하므로 마지막이지만, 필요하면 Phase 1에서 공시·실적 일정만 다루는 축소판을 먼저 낼 수 있다.

> **착수 조건**
> 타 파트 의존은 [기존 조회 API의 서버 간 접근 권한](#internal) 하나뿐이고, 그마저 시드 데이터 어댑터로 대체해 병렬 진행할 수 있다. 종목 마스터·시세·공시·뉴스는 전부 AI 파트가 직접 적재하므로 **다른 파트의 일정에 막히는 지점이 없다.** 오늘 바로 착수 가능하다.

---

AI 투자 비서 · AI 파트 인터페이스 계약 v0.1 — 국내 주식 전용, 앱 내 가상 포트폴리오 기준. 엔진 산식과 프롬프트 정책은 별도 문서로 분리한다.
