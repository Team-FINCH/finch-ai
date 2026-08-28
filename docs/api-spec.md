AI Part · Interface Contract

# AI 투자 비서 API 명세

AI 파트가 프론트엔드에 제공하는 6개 기능의 인터페이스, 그리고 그 기능들이 성립하기 위해 백엔드에 요구하는 데이터 계약을 함께 정의한다. 국내 주식 전용, 앱 내 가상 포트폴리오를 전제로 한다.

**Base** /api/ai/v1 · **Auth** 내부 토큰 + 신뢰 헤더 (내부 전용) · **Format** JSON · UTF-8 · **Runtime** FastAPI (별도 서비스)

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
| 데이터 원천 | KIS OpenAPI(시세) · pykrx(히스토리) · DART(공시·재무) · NAVER API HUB(뉴스 검색 결과) · ECOS(거시) |
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
| 인증 | `X-Internal-Token` + `X-User-Id` 두 개 (백엔드 명세 §9). 앞은 호출자가 백엔드임을 확인하는 서비스 간 공유 토큰이고, 뒤는 백엔드가 JWT를 검증한 뒤 넣어 주는 사용자 식별자다. AI 서버는 JWT를 다시 검증하지 않으며 사용자 식별자를 **경로·본문에 넣지 않는다**. 사용자 식별자를 검증 없이 신뢰하므로 **AI 서버는 내부 네트워크에서만 접근 가능해야 한다** |
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
    "news":      "2026-08-19T13:50:00+09:00",
    "macro":     null
  },
  "model": "gpt-5.4-mini",
  "cached": false,
  "content": { },
  "citations": [ ],
  "freshness_warnings": [
    { "source": "news", "data_as_of": "2026-08-19T06:00:00+09:00",
      "age_seconds": 29531, "threshold_seconds": 21600,
      "message": "뉴스 정보가 평소보다 오래되었습니다." }
  ],
  "disclaimer": "본 정보는 투자 판단을 돕기 위한 참고 자료이며 투자 권유가 아닙니다."
}
```

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `request_id` | string | 피드백·로그 추적 키. 프론트는 이 값을 `POST /feedback`에 그대로 전달 |
| `data_as_of` | object | **데이터 원천별 기준 시각.** UI에 반드시 노출한다. 시세는 지연될 수 있으므로 생성 시각과 별도로 관리 |
| `model` | string | 문장 생성에 쓴 LLM 모델 이름. 서버 설정값을 그대로 싣는다 |
| `cached` | boolean | 응답 전체 또는 일부가 캐시에서 왔는지. 종목 분석의 공통 섹션을 재사용하면 `true` |
| `content` | object | 엔드포인트별 본문. 이 아래 §3–§8이 각각의 모양을 정의한다 |
| `citations` | array | [§2.4](#citations) 근거 목록. 근거를 싣지 않는 엔드포인트에서는 빈 배열 |
| `freshness_warnings` | array | 사용한 원천의 기준 시각이 허용 범위를 넘겼을 때만 표시하는 최신성 경고. 비어 있으면 경고 없음 |
| `disclaimer` | string | 고지 문구. 하드코딩하지 말고 응답 값을 표시할 것 (규제 문구 변경 대응) |

봉투의 아홉 키와 `data_as_of`의 다섯 키(`price · portfolio · filings · news · macro`)는 **항상 모두 실려 나온다.** 그 응답이 어떤 원천을 읽지 않았으면 키가 빠지는 것이 아니라 값이 `null`이다. 프론트는 키 존재 여부가 아니라 `null` 여부로 분기한다.

이 문서의 예시에서 `…`와 `"segments": [ ]`는 **지면상 줄인 자리**다. 키가 없다는 뜻이 아니며, 실제 응답에서 `segments`가 빈 배열인 경우는 그 문장에 수치 조각이 없을 때다.

### 2.3 서술과 수치의 분리 — 핵심 규약

LLM은 계산을 시키지 않아도 *주어진 숫자를 반올림하거나 바꾼다.* `18.3%`를 "약 18%"로, `+0.87%p`를 "+0.9%p"로 바꾸는 일은 흔하게 발생한다. AI 서비스는 내부적으로 LLM에게 자리표시자만 쓰게 한 뒤 엔진 계산값으로 치환하고, 원본과 대조 검증한 문장을 반환한다. **치환과 검증이 서버에서 끝나므로 클라이언트는 받은 문자열을 그대로 출력하면 된다.**

```
{
  "text": "반도체 관련 자산이 포트폴리오의 42.3%를 차지합니다. 종목은 3개로 나뉘어 있으나 동일한 업황 사이클에 함께 노출되어 있어 분산 효과는 제한적입니다.",
  "segments": [
    { "type": "text",   "value": "반도체 관련 자산이 포트폴리오의 ",
      "raw": null, "unit": null, "source": null, "direction": null },
    { "type": "metric", "value": "42.3%", "raw": 0.423, "unit": "ratio",
      "source": "risk_engine", "direction": null },
    { "type": "text",   "value": "를 차지합니다. 종목은 ",
      "raw": null, "unit": null, "source": null, "direction": null },
    { "type": "metric", "value": "3개", "raw": 3, "unit": "count",
      "source": "portfolio_engine", "direction": null },
    { "type": "text",   "value": "로 나뉘어 있으나 동일한 업황 사이클에 함께 노출되어 있어 분산 효과는 제한적입니다.",
      "raw": null, "unit": null, "source": null, "direction": null }
  ]
}
```

| 필드 | 용도 |
| --- | --- |
| `text` | **완성된 문장.** 이것만 출력해도 정상 동작한다. 기본 렌더링 경로 |
| `segments` | 선택 사항. 등락 색·강조·툴팁 등 수치 스타일링이 필요할 때만 순회한다 |
| `segments[].raw` | 원시 값. 차트·정렬 등 2차 가공이 필요할 때 사용 |
| `segments[].direction` | `up` / `down`. 국내 관례에 따라 각각 적색·청색. 방향이 없는 수치는 `null` |
| `segments[].unit` · `source` | `metric` 조각에서만 채워진다. `source`는 `metric`에 필수 |

`segments`는 `text`를 잘라 놓은 것일 뿐 다른 정보가 아니다. 이어 붙이면 `text`와 정확히 일치한다. 프론트가 `segments`를 무시해도 표시 내용은 동일하며, **숫자 정확도는 어느 쪽을 쓰든 서버가 보장한다.**

조각의 여섯 키(`type · value · raw · unit · source · direction`)는 **`text` 조각에도 전부 실려 나온다** — `value`를 뺀 나머지 넷이 `null`일 뿐이다. 위 예시처럼 `text` 조각에서 `null` 넷이 보이는 것이 정상이며, 아래 §3·§5·§8 예시도 같은 모양이다.

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
| `news` | NAVER API HUB 뉴스 검색 결과(제목·요약·원문 링크) |
| `price` | 시세 · 거래 데이터 |
| `macro` | ECOS 거시지표 |
| `engine` | 자체 계산 결과 (외부 URL 없음) |
| `wiki` | 사용자가 기록한 투자 논지 |

### 2.5 스트리밍 — MVP 범위 밖

> **결정**
> **SSE 스트리밍은 MVP 에서 하지 않는다.** 모든 응답은 [§2.2](#envelope)의 JSON 봉투를 한 번에 돌려준다. `Accept` 값은 보지 않는다.
> 이 절에는 `meta`/`tool_call`/`delta`/`citations`/`done` 이벤트 규약이 있었다. 걷어낸 이유는 셋이다. 팀 featureSpec §10.2 의 MVP 3종(종목 분석 · 용어 설명 · 보유 종목 요약) 어디에도 스트리밍 요구가 없고, 백엔드 명세에도 스트리밍 설계가 없다. 그리고 호출 경로가 프론트 → 백엔드 → AI 로 확정되어 **스트리밍을 하려면 백엔드가 스트리밍 프록시를 먼저 만들어야 한다** — 계획에 없는 작업이다. 아무도 만들지 않을 기능을 문서가 약속하고 있었고, 실제로 프론트가 붙였다가 동작하지 않았다.
> 다시 논의한다면 순서는 **백엔드 스트리밍 프록시가 먼저**다. 그 전에는 AI 쪽만 구현해도 프론트까지 닿지 않는다.

### 2.6 에러 · 캐시 · 호출 한도

`AUTH_` 접두사는 서비스의 사용자 인증 에러 전용이다. AI 서버는 내부 인증을 포함해 새 에러 코드를 추가할 때 `AUTH_` 접두사를 사용하지 않는다.

```
{
  "code": "INSUFFICIENT_DATA",
  "message": "위험 지표를 계산하기에 가격 히스토리가 부족합니다.",
  "detail": { "required_days": 60, "available_days": 23, "ticker": "462870" },
  "request_id": "req_01JQZ8M3T7K2"
}
```

> **문구 규약**
> `message`는 **사용자에게 그대로 보여도 되는 한국어 문구**다. 백엔드 명세 §1.3 이 이 값을 화면에 그대로 노출하는 전제이므로, "LLM 키가 없습니다" 같은 내부 사정은 담지 않는다. 기계가 분기할 사유는 `detail.reason`으로 보낸다. `INSUFFICIENT_DATA`에서 현재 정의된 값은 `llm_key_missing`과 `ledger_unavailable` 두 개가 전부다. 단, 보유 종목·거래일·종가가 없는 경우처럼 `reason` 없이 구체적인 `detail`만 제공할 수도 있다. `request_id`는 §1.3 에 없지만 남긴다 — `POST /feedback`이 이 값으로 응답을 찾으므로 에러에서만 빠지면 제보를 응답에 맞출 수 없다.

| HTTP | code | 발생 조건 | 프론트 처리 |
| --- | --- | --- | --- |
| 400 | `INVALID_REQUEST` | 스키마 위반 | 개발 오류. 사용자 노출 금지 |
| 400 | `UNSUPPORTED_MARKET` | 해외 종목 요청 | "현재 국내 종목만 지원합니다" |
| 401 | `UNAUTHORIZED` | 내부 토큰 불일치, 신뢰 헤더 누락·공백 | **사용자 세션 유지.** AI 영역만 오류 표시 |
| 404 | `INSTRUMENT_NOT_FOUND` | 미상장·폐지 종목 | 종목 페이지 자체 처리 |
| 409 | `INSUFFICIENT_DATA` | 보유 종목 0개, 가격 히스토리 60거래일 미만 | **AI 영역만 대체 문구로 숨김.** 화면 전체 실패 아님 |
| 422 | `GUARDRAIL_BLOCKED` | 투자 권유·가격 예측 요구, 수치 검증 실패 | 차단 사유 문구 표시 |
| 429 | `RATE_LIMITED` | 호출 한도 초과 | `Retry-After` 헤더만큼 대기 |
| 502 | `RETRIEVAL_FAILED` | 임베딩 제공자 없음·질의 임베딩 실패·벡터 저장소 오류 | 재시도 버튼 |
| 504 | `LLM_TIMEOUT` | 생성 시간 초과 (30초) | 재시도 버튼 |

> **구분**
> **"근거를 못 찾았다"와 "검색이 고장났다"는 다른 응답이다.** 맞는 자료가 없으면 생성을 계속하고 **200**으로 "관련 자료를 찾지 못했습니다"를 담아 보낸다 (프롬프트 정책 §7). 임베딩 제공자가 없거나 질의 임베딩·벡터 조회가 실패하면 답을 만들지 않고 `RETRIEVAL_FAILED`를 보낸다. 둘을 같은 화면으로 처리하면 검색이 죽은 동안 사용자는 자료가 없는 줄 안다 — 앞은 재시도가 무의미하고 뒤는 재시도가 유효하다.

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

요청 한도는 `X-User-Id`와 엔드포인트별로 독립 집계한다. 운영에서는 PostgreSQL 장부를 사용하므로 Pod가 여러 개이거나 재시작되어도 같은 한도를 공유한다. 초과하면 `detail.reason=request_rate_limit`, `Retry-After` 헤더와 대기 시간을 반환한다. 실제 GMS 호출 직전에는 사용자별 일일 토큰을 예약하고 캐시 응답은 예약하지 않는다. 호출 중 Pod가 종료되어도 예약은 기본 5분 뒤 자동 회수된다. 일일 예산을 넘으면 `detail.reason=daily_token_budget`으로 429를 반환한다.

## §3 종목 AI 분석

**POST** `/api/ai/v1/stocks/{ticker}/analysis`  —  Phase 1

종목 상세 화면의 AI 분석. 투자 논지 점검을 섹션으로 포함한다.

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
      "cached": false,
      "cached_at": null
    },
    "changes":   { "title": "최근 변화",            "text": "…", "segments": [ ], "cached": false, "cached_at": null },
    "attention": { "title": "시장이 주목하는 요인",  "text": "…", "segments": [ ], "cached": false, "cached_at": null },
    "risks":     { "title": "확인된 위험 요인",      "text": "…", "segments": [ ], "cached": false, "cached_at": null },
    "my_impact": {
      "title": "내 포트폴리오 영향",
      "text": "이 종목은 포트폴리오의 41.7%를 차지하며 평균 매입가 대비 +10.1% 상태입니다.",
      "segments": [
        { "type": "text",   "value": "이 종목은 포트폴리오의 ",
          "raw": null, "unit": null, "source": null, "direction": null },
        { "type": "metric", "value": "41.7%", "raw": 0.4168, "unit": "ratio",
          "source": "portfolio_engine", "direction": null },
        { "type": "text",   "value": "를 차지하며 평균 매입가 대비 ",
          "raw": null, "unit": null, "source": null, "direction": null },
        { "type": "metric", "value": "+10.1%", "raw": 0.1011, "unit": "ratio",
          "source": "portfolio_engine", "direction": "up" },
        { "type": "text",   "value": " 상태입니다.",
          "raw": null, "unit": null, "source": null, "direction": null }
      ],
      "cached": false,
      "cached_at": null
    },
    "thesis_check": {
      "title": "투자 논지 점검",
      "text": "매수 시점에 언급하신 근거와 관련해 …",
      "segments": [ ],
      "cached": false,
      "cached_at": null,
      "thesis": {
        "text": "HBM 구조적 성장에 베팅",
        "recorded_at": "2026-03-11T10:22:00+09:00",
        "source": "user_stated"
      },
      "supporting": [
        {
          "citation_id": "cit_1",
          "title": "분기보고서",
          "source": "DART",
          "rationale": "HBM 사업 확대가 기록한 성장 논지를 뒷받침합니다."
        }
      ],
      "challenging": [ ]
    },
      "next_events": {
      "title": "다음에 확인할 일정",
      "text": "…",
      "segments": [ ],
      "cached": false,
      "cached_at": null,
      "events": [
        {
          "id": "evt_01JQ...",
          "type": "earnings",
          "title": "3분기 실적 발표",
          "event_date": "2026-10-30",
          "confirmed": true,
          "days_until": 63
        }
      ]
    }
  }
}
```

| 키 | 실제 출력 |
| --- | --- |
| `sections` | 요청한 `sections`의 키가 **그대로 전부** 들어온다. 값이 없는 섹션은 키가 빠지는 것이 아니라 `null`이다 |
| 섹션 공통 | 모든 섹션은 [§12](#types) Section 다섯 키(`title · text · segments · cached · cached_at`)를 갖는다. `changes`·`attention`·`risks`도 예외가 아니다 |
| `my_impact` | `personalize: true`이고 **보유 중일 때만** 값이 찬다. 아니면 `null` |
| `thesis_check` | 기록된 활성 논지가 있을 때만 값이 차고, 그때 Section 다섯 키에 `thesis`·`supporting`·`challenging`이 더 붙는다 |
| `next_events` | Section 다섯 키에 `events`가 더 붙는다 |
| `cached` · `cached_at` | 6시간 안에 만든 같은 종목·같은 프롬프트 버전의 공통 섹션을 재사용하면 `true` / 원본 생성 시각. 새로 만든 섹션과 개인화 섹션은 `false` / `null` |

> **근거 분류와 일정**
> `thesis_check.supporting`·`challenging`은 검색된 공시·뉴스가 사용자의 논지를 뒷받침하는지 약화하는지 분류한 결과다. 각 항목은 최상위 `citations`의 id와 제목·출처, 논지와 연결되는 이유를 담는다. 중립이거나 관련 없는 자료는 넣지 않으므로 두 배열이 비어 있어도 정상이다. `next_events.events`는 `events` 저장소에서 오늘부터 90일 안의 확정된 종목 일정을 날짜순으로 최대 5건 제공한다. 확정 일정이 없으면 빈 배열이다.

> **비용 설계**
> `current`·`changes`·`attention`·`risks`·`next_events`는 사용자와 무관하므로 **종목 단위로 캐시**한다(TTL 6시간). 프롬프트가 바뀌면 이전 캐시는 쓰지 않는다. `my_impact`·`thesis_check`는 사용자의 최신 원장과 논지를 반영해 요청마다 생성한다. 이 분리가 인기 종목의 같은 분석을 반복 생성하는 비용을 줄인다.

`attention`·`risks`의 명칭은 규제 대응이다. "긍정 요인 / 부정 요인"은 의견 제시로 읽힐 수 있어 **시장이 주목하는 요인 / 공시·실적에서 확인된 위험 요인**이라는 출처 귀속형으로 고정한다. 목표주가와 투자의견은 어떤 섹션에서도 생성하지 않는다.

## §4 AI 대화

**POST** `/api/ai/v1/chat`  —  Phase 1

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

`conversation_id`를 생략하면 새 대화를 시작하고 응답 `content.conversation_id`로 발급한다. `context`는 화면 맥락으로, 사용자가 "이거 어때?"처럼 대명사로 물을 때 지시 대상을 해소한다. `screen`은 `home · portfolio · stock_detail · order · chat` 중 하나. `message`는 공백만으로는 안 되고 2,000자를 넘으면 `INVALID_REQUEST`다 — 그보다 긴 것은 대화가 아니라 문서 붙여넣기다.

#### Response — content

```
{
  "conversation_id": "conv_01JQZ7X4M9",
  "answer": {
    "title": null,
    "text": "삼성전자는 포트폴리오의 41.7%를 차지합니다. …",
    "segments": [ ],
    "cached": false,
    "cached_at": null
  },
  "tools_used": ["get_portfolio", "calc_risk_metrics"]
}
```

`answer`는 [§12](#types) Section 다섯 키를 그대로 쓴다. 다만 대화에는 붙일 제목이 없어 **`answer.title`은 항상 `null`**이고, 캐시 계층이 없어 `cached`/`cached_at`도 `false`/`null`로 고정이다. 말풍선 제목이 필요하면 프론트가 정한다.

`answer`는 [§2.3](#narrative)의 Section과 같은 모양이라 종목 분석의 섹션과 같은 방식으로 렌더링한다. `tools_used`는 이번 답변에서 실제로 호출된 Tool 이름이며, 근거를 되짚을 때 쓴다 — 호출 순서는 보장하지 않는다. 투자 용어 질의(featureSpec §10.2 "용어 설명")도 이 엔드포인트가 담당하고, 도구가 필요 없는 질문은 Tool 없이 답하므로 `tools_used`가 빈 배열이 된다.

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
  "insufficient_history": null,
  "summary": {
    "title": "종합 진단",
    "text": "가장 큰 위험은 반도체 업종 집중도입니다. …",
    "segments": [ ],
    "cached": false,
    "cached_at": null
  },
  "findings": [
    {
      "id": "sector_concentration",
      "category": "concentration",
      "severity": "high",
      "title": "업종 집중",
      "text": "삼성전자, SK하이닉스, 한미반도체를 합산하면 42.3%입니다. …",
      "segments": [
        { "type": "text",   "value": "삼성전자, SK하이닉스, 한미반도체를 합산하면 ",
          "raw": null, "unit": null, "source": null, "direction": null },
        { "type": "metric", "value": "42.3%", "raw": 0.423, "unit": "ratio",
          "source": "risk_engine", "direction": null },
        { "type": "text",   "value": "입니다. …",
          "raw": null, "unit": null, "source": null, "direction": null }
      ],
      "evidence": {
        "tickers": ["005930", "000660", "042700"],
        "metric": "top_sector_weight",
        "value": 0.4230,
        "threshold": 0.3500,
        "hhi": 0.2841,
        "avg_pairwise_corr": 0.78,
        "sector": "반도체"
      }
    }
  ],
  "indicators": {
    "hhi": 0.2841,
    "top1_weight": 0.4168,
    "top3_weight": 0.7204,
    "sector_hhi": 0.3120,
    "annualized_volatility": 0.2837,
    "max_drawdown_1y": -0.2214,
    "cash_ratio": 0.081,
    "rate_sensitivity": "high",
    "beta": 1.14,
    "large_cap_weight": 0.8320,
    "diversification_ratio": 1.18
  }
}
```

| 필드 | 값 | 설명 |
| --- | --- | --- |
| `risk_level` | `low · moderate · high` · `null` | 규칙 엔진 판정. LLM이 정하지 않는다. 판정을 보류하면 `null` |
| `risk_score` | 0–100 정수 · `null` | 구성 지표 가중합. 산식은 별도 문서. 판정 보류 시 `null` |
| `insufficient_history` | string · `null` | 변동성·상관을 계산하지 못한 이유. 정상일 때 `null` |
| `summary` | Section · `null` | [§12](#types) Section 다섯 키. 문장 생성이 막히면 `null`이고 지표는 그대로 나간다 |
| `findings[].severity` | `info · medium · high` | 정렬 순서가 곧 중요도 순위 |
| `findings[].text` · `segments` | string · array · `null` | Section 전체가 아니라 이 두 키만 펼쳐 담는다. 생성이 막히면 둘 다 `null` |
| `evidence` | object | LLM 입력으로 쓰인 원시 지표. 디버깅·평가용으로 응답에 포함 |

`evidence`의 고정 키는 `tickers · metric · value · threshold · hhi` 다섯이다. 나머지는 조건부로 붙는다 — 상관 지표를 계산했으면 `avg_pairwise_corr`, `id`가 `sector_concentration`이면 `sector`, `macro_exposure`면 `rate_sensitivity`가 더 들어온다.

`indicators`의 열한 키는 항상 실려 나오며, **계산되지 않은 지표는 0이 아니라 `null`**이다. 공통 거래일이 60일에 못 미치면 `annualized_volatility`·`diversification_ratio`가 `null`이 되고 `insufficient_history`에 사유 문자열이 담기며, 이때 `risk_level`·`risk_score`도 `null`이 될 수 있다. 집중도·현금 비중은 그대로 유효하므로 **409로 끊지 않는다.**

`findings[].id`는 `ticker_concentration · sector_concentration · volatility · correlation · liquidity · macro_exposure` 여섯 중 하나이며, 걸린 항목만 중요도 순으로 배열에 담긴다.

`findings[].category`는 `concentration · volatility · correlation · style_tilt · macro_exposure · liquidity` 중 하나다. 국가 집중도와 통화 노출은 국내 단일 시장이므로 정의하지 않는다.

## §6 수익률 원인 분석

**POST** `/api/ai/v1/portfolio/attribution`  —  Phase 3

수익률을 시장·섹터·종목 선택으로 분해하고 주요 기여 종목에 이벤트를 연결한다.

#### Request

```
{ "period": "1d", "benchmark": "KOSPI" }
```

`period`는 `1d · 1w · 1m · 3m · ytd`이며 생략 시 `1d`다. 다른 값은 `INVALID_REQUEST`다.

> **구현과 다름**
> `benchmark`는 요청 본문에 **받기만 하고 쓰이지 않는다.** 벤치마크는 항상 보유 종목 유니버스를 시가총액으로 합성한 시장 전체이며, `"KOSPI"`를 보내든 생략하든 결과가 같다. 지수 선택을 지원할 계획이 없다면 이 필드는 요청 스키마에서 빼는 편이 맞다 — 사양이 아니라 **미해결 사항**으로 남긴다.

#### Response — content

```
{
  "period": "1d",
  "start": "2025-09-11",
  "end": "2025-09-12",
  "trading_days": 2,
  "portfolio_return": 0.0213,
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
      "sector": "반도체",
      "weight": 0.1820,
      "return": 0.0512,
      "contribution": 0.0093,
      "held_at_start": true,
      "events": [
        { "citation_id": "cit_1", "type": "news", "title": "HBM 공급 계약 관련 보도",
          "summary": "HBM 공급 계약 관련 보도", "event_date": "2025-09-12",
          "matched_confidence": 0.72 }
      ]
    }
  ],
  "detractors": [ ],
  "sectors": [
    {
      "sector": "반도체",
      "portfolio_weight": 0.4230,
      "benchmark_weight": 0.2810,
      "allocation": 0.0019,
      "selection": 0.0042,
      "proxy": false
    }
  ],
  "notes": [ "…" ],
  "summary": {
    "title": "성과 요인",
    "text": "오늘 상승분의 상당 부분은 시장 전체 상승에서 왔습니다. …",
    "segments": [ ],
    "cached": false,
    "cached_at": null
  },
  "text": "오늘 상승분의 상당 부분은 시장 전체 상승에서 왔습니다. …",
  "segments": [ ]
}
```

| 키 | 실제 출력 |
| --- | --- |
| `start` · `end` · `trading_days` | 실제로 되짚은 구간과 그 안의 거래일 수. 달력 기준으로 자른 뒤 거래일만 남긴 결과라 `period`만으로는 알 수 없다 |
| `portfolio_return` · `total_return` | **같은 값이 두 키로 나간다** (아래 참조) |
| `contributors` · `detractors` | 기여도 부호로 가른 같은 모양의 행. 각 행에 `sector`·`held_at_start`가 함께 온다 |
| `events[].title` · `summary` | **같은 문자열이 두 키로 나간다.** 별도 요약 패스가 없어 제목을 그대로 쓴다 |
| `sectors` | 섹터별 배분·선택 효과. `proxy`는 그 섹터 벤치마크를 대체 지표로 채웠다는 뜻 |
| `notes` | 계산 중 붙은 단서 문자열 배열. 없으면 빈 배열 |
| `summary` · `text` · `segments` | **같은 내용이 두 자리로 나간다.** `summary`가 [§12](#types) Section 전체이고, `text`·`segments`는 그중 두 키를 최상위로 다시 펼친 것이다. 생성이 막히면 셋 다 `null` |

> **중복 키 — 미해결**
> `portfolio_return`/`total_return`, `summary`/`text`+`segments`, `events[].title`/`summary` 세 쌍은 **같은 값을 두 이름으로 내보낸다.** 지금은 구현이 그렇게 동작하므로 그대로 적었다. 소비자가 붙기 전에 한쪽으로 줄이는 것이 맞고, 그 결정은 이 문서 밖이다.

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
  "order_summary": [
    { "ticker": "000660", "side": "buy", "quantity": 40, "price": 214000, "amount": 8560000 }
  ],
  "orders_value": 8560000,
  "feasible": true,
  "shortfall": null,
  "before": {
    "hhi": 0.2841, "top1_weight": 0.4168, "top3_weight": 0.7204, "sector_hhi": 0.3120,
    "annualized_volatility": 0.2837, "max_drawdown_1y": -0.2214, "cash_ratio": 0.0810,
    "rate_sensitivity": "high", "beta": 1.14, "large_cap_weight": 0.8320,
    "diversification_ratio": 1.18, "top_sector_weight": 0.4230
  },
  "after": {
    "hhi": 0.3392, "top1_weight": 0.4168, "top3_weight": 0.7511, "sector_hhi": 0.3680,
    "annualized_volatility": 0.3105, "max_drawdown_1y": -0.2214, "cash_ratio": 0.0593,
    "rate_sensitivity": "high", "beta": 1.19, "large_cap_weight": 0.8460,
    "diversification_ratio": 1.11, "top_sector_weight": 0.5044
  },
  "delta": {
    "hhi": 0.0551, "top1_weight": 0.0, "top3_weight": 0.0307, "sector_hhi": 0.0560,
    "annualized_volatility": 0.0268, "max_drawdown_1y": 0.0, "cash_ratio": -0.0217,
    "beta": 0.05, "large_cap_weight": 0.0140, "diversification_ratio": -0.07,
    "top_sector_weight": 0.0814
  },
  "warnings": [
    {
      "id": "sector_concentration",
      "severity": "high",
      "title": "업종 집중",
      "metric": "top_sector_weight",
      "before": 0.4230,
      "after": 0.5044,
      "threshold": 0.3500,
      "text": "이 주문은 반도체 업종 비중을 50.4%로 올립니다. …",
      "segments": [ ]
    }
  ],
  "thesis_conflicts": [
    {
      "id": "6f1c8a2e-4b90-4d1e-9a3f-2c7d5e081b64",
      "ticker": "000660",
      "fact": "HBM 구조적 성장에 베팅",
      "source": "user_stated",
      "recorded_at": "2026-03-11T10:22:00+09:00",
      "conflict": "기록하신 논지와 이 주문의 방향이 어긋납니다. …",
      "segments": [ ]
    }
  ],
  "summary": {
    "title": "주문 요약",
    "text": "이 주문을 실행하면 …",
    "segments": [ ],
    "cached": false,
    "cached_at": null
  }
}
```

| 키 | 실제 출력 |
| --- | --- |
| `order_summary` | 체결을 가정한 주문 한 줄씩. `price`를 생략했으면 여기 채워진 값이 실제 사용된 단가다 |
| `before` · `after` | [§5](#ep-diagnosis) `indicators` 열한 키에 `top_sector_weight`를 더한 **열두 키**. 양쪽 키 구성은 같다 |
| `delta` | `after − before`. **숫자인 지표만 담긴다** — `rate_sensitivity`처럼 문자열이거나 한쪽이 `null`인 지표는 키째로 빠진다 |
| `warnings` | 이 주문 때문에 **새로 걸렸거나 등급이 올라간** 항목만. 나아진 항목은 요약이 말한다. 첫 발생이면 `before`가 `null` |
| `thesis_conflicts` | 주문에 오른 종목의 `user_stated` 논지 중 어긋난 것만. 충돌이 없으면 빈 배열 |
| `summary` | [§12](#types) Section 다섯 키. 생성이 막히면 `null` |

`feasible`이 `false`면 현금 부족이며 부족액은 **최상위 `shortfall`**에 담긴다 (에러가 아니라 200 응답의 본문이다. 부족하지 않으면 `null`). `thesis_conflicts`는 **사용자가 직접 진술한 항목(`user_stated`)만** 사용한다. AI가 추론한 성향으로 주문에 이의를 제기하면 근거 없는 참견이 된다.

히스토리가 60거래일에 못 미쳐도 409로 끊지 않는다. `annualized_volatility`·`diversification_ratio`가 `before`·`after` 양쪽에서 `null`이 되고 `delta`에서 빠질 뿐, 집중도·현금 차분은 그대로 유효하다. `max_drawdown_1y`의 `delta`가 `0.0`인 것도 정상이다 — 아직 내지 않은 주문이 지난 낙폭을 바꾸지는 않으므로 전·후에 같은 시계열을 넘긴다.

## §8 데일리 브리핑

**GET** `/api/ai/v1/briefing?date=2026-08-19`  —  Phase 3

사용자·거래일별 배치 생성 결과를 우선 조회한다. 배치가 늦거나 대상에서 빠졌으면 기존 화면을 위해 같은 규칙으로 즉시 생성해 저장한다. `date` 생략 시 마지막 거래일.

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
        { "type": "text",   "value": "포트폴리오에서 ",
          "raw": null, "unit": null, "source": null, "direction": null },
        { "type": "metric", "value": "18.2%", "raw": 0.1820, "unit": "ratio",
          "source": "portfolio_engine", "direction": null },
        { "type": "text",   "value": "를 차지하는 SK하이닉스가 ",
          "raw": null, "unit": null, "source": null, "direction": null },
        { "type": "metric", "value": "+5.12%", "raw": 0.0512, "unit": "ratio",
          "source": "portfolio_engine", "direction": "up" },
        { "type": "text",   "value": " 상승했습니다. …",
          "raw": null, "unit": null, "source": null, "direction": null }
      ],
      "related_tickers": ["000660"],
      "deeplink": "/stocks/000660?tab=ai",
      "citations": [ ]
    }
  ]
}
```

| `status` | 의미 | 프론트 처리 |
| --- | --- | --- |
| `ready` | 생성 완료 | 정상 표시 |
| `generating` | 배치 진행 중 | 스켈레톤 표시 후 30초 뒤 재조회. **현재 서버는 캐시 미스 시 즉시 생성하므로 이 값을 내보내지 않는다** |
| `empty` | 보유 종목 없음 또는 유의미한 이벤트 없음 | 영역 숨김 |

`category`는 `holding_move · earnings · filing · macro_event · portfolio_shift` 중 하나이며 최대 4건을 반환한다.

`date`·`status`·`generated_at`·`items` 네 키는 항상 실려 나온다. 보유 종목이 없거나 내보낼 항목이 없으면 `status: "empty"`에 `items: [ ]`이며 오류가 아니다. 이때 `date`는 `null`일 수 있다(조회 시 `date`를 주지 않았고 기준 거래일도 못 잡은 경우).

공시·뉴스·거시 이벤트가 `document_id`로 원문과 연결돼 있으면 `items[].citations`에 최상위 `citations`의 id를 넣는다. 여러 항목이 같은 문서를 가리키면 Citation 하나를 함께 쓴다. 보유 종목 등락·업종 변화처럼 문서에서 오지 않은 항목이나, 연결 문서가 삭제된 항목은 빈 배열이며 브리핑 생성 자체는 계속한다.

> **비용 설계**
> `relevance_score` 산출은 **LLM이 아니라 규칙 엔진**이 한다(보유 여부 × 비중 × 이벤트 중요도). LLM은 상위 4건의 문장만 생성한다. `python -m ingest.briefings`를 하루 한 번 실행하면 최근 30일 활성 사용자의 결과를 미리 만들며, 같은 거래일·프롬프트 버전 결과는 다시 생성하지 않는다. 특정 대상은 `--users`, 강제 재생성은 `--force`를 쓴다.

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

논지 수정. 수정 시 `source`는 자동으로 `user_stated`가 되며, 커밋 직후의 종목 분석 `thesis_check`를 포함한 모든 읽기에서 새 값이 사용된다. 아래의 비동기 배치 규칙은 AI가 대화에서 맥락을 추출해 기록하는 경우에만 해당하며, 사용자의 직접 수정에는 적용하지 않는다.

Phase 1에는 논지 삭제 API가 없다. `status`는 서버가 새 논지를 기록할 때 이전 논지를 `active`에서 `closed`로 보관하기 위한 내부 이력 상태이며, `PUT`으로 바꾸는 입력값이 아니다. 따라서 화면에는 논지 수정만 제공하고 삭제 동작은 제공하지 않는다.

**DELETE** `/api/ai/v1/wiki/facts/{factId}`  —  Phase 1

항목 삭제. 즉시 이후 모든 응답에서 제외된다.

| `source` | 의미 | 응답에서의 취급 |
| --- | --- | --- |
| `user_stated` | 사용자가 직접 진술 | 사실로 인용 가능 |
| `derived_from_trades` | 거래 이력에서 도출 | 사실로 인용 가능 |
| `ai_inferred` | AI가 대화에서 추론 | **단정 금지.** "~하신 것으로 보이는데 맞나요?" 형태만 허용 |

대화에서 AI가 추출하는 Wiki 갱신은 **대화 종료 후 비동기 배치**로만 수행한다. 응답 경로에서 동기적으로 쓰면 지연이 그대로 사용자에게 전가된다. 사용자가 위 `PUT`으로 직접 수정한 값은 즉시 반영한다. 동시 세션 충돌은 사용자당 단일 writer 큐로 직렬화한다.

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

같은 `request_id`로 다시 전송하면 새 행을 누적하지 않고 기존 평가를 마지막 요청으로 덮어쓴다. 따라서 화면은 전송 후에도 평가 수정 동작을 제공할 수 있다. Phase 1에는 피드백 취소(평가 삭제) API가 없다.

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
| `documents` · `embeddings` | 공시 원문·뉴스 검색 요약과 벡터 (pgvector) |
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

여섯 키는 **두 종류 모두에 실려 나온다.** `type: "text"`인 조각은 `value` 외 넷이 `null`일 뿐 키가 빠지지는 않는다. `metric`에서는 `raw`와 `source`가 필수이고, `unit`·`direction`은 없으면 `null`이다.

#### Section

```
{
  "title":     "string | null",
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
  "publisher":    "string | null",
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

## §14 구현 대조 근거

**이 문서의 응답 예시는 코드에서 확인한 것이다.** 예시와 구현이 어긋나면 구현이 맞고 이 문서가 틀린 것이므로, 다시 대조할 때 어디를 열어야 하는지를 남긴다. 봉투·조각·근거의 키 구성은 `app/core/schemas.py`의 Pydantic 모델이 정하고, `content` 안쪽은 각 라우터가 `dict`로 직접 조립한다. 라우터에 `response_model`을 걸지 않으므로 **선언된 키는 값이 `None`이어도 빠지지 않고 `null`로 나간다.**

| § | 엔드포인트 | `content` 조립 위치 |
| --- | --- | --- |
| §3 | `POST /stocks/{ticker}/analysis` | `app/api/routes/stocks.py` · `create_analysis` (섹션 키는 `SECTION_TITLES`) |
| §4 | `POST /chat` | `app/api/routes/chat.py` · `chat` |
| §5 | `POST /portfolio/diagnosis` | `app/api/routes/portfolio.py` · `diagnosis`, `_finding_payload`, `_evidence`, `_indicators` |
| §6 | `POST /portfolio/attribution` | `app/api/routes/portfolio.py` · `attribution`, `_contributor_payload` |
| §7 | `POST /orders/preview` | `app/api/routes/orders.py` · `preview`, `_measures`, `_delta`, `_raised`, `_section_fields` |
| §8 | `GET /briefing` | `app/api/routes/briefing.py` · `_item_payload`, `_values`, `_empty` |

| 공통 요소 | 출처 |
| --- | --- |
| 봉투 · `data_as_of` | `app/core/schemas.py` · `Envelope`, `DataAsOf` |
| Segment · Section · Citation | `app/core/schemas.py` · `Segment`, `Section`, `Citation` |
| 에러 코드 · HTTP 상태 | `app/core/errors.py` · `ErrorCode`, `STATUS_BY_CODE` |
| `unit` · `source` · `category` 등 열거값 | `app/core/enums.py` |
| 기계 판독용 스키마 | `docs/openapi.json` (FastAPI 생성본. `scripts/check.sh`가 최신 여부를 검사한다) |

> **문서가 앞서 나간 자리**
> 아래는 **이 문서가 약속하지만 구현이 아직 하지 않는** 항목이다. 구현을 문서에 맞추는 것이 아니라 문서가 현재 동작을 먼저 정확히 적고, 채울 때 이 목록을 지운다.

| 약속 | 현재 동작 | 응답에서 보이는 모습 |
| --- | --- | --- |
| `benchmark` 선택 ([§6](#ep-attribution)) | 요청 필드를 받기만 하고 쓰지 않음 | 어떤 값을 보내도 결과가 같다 |

---

AI 투자 비서 · AI 파트 인터페이스 계약 v0.1 — 국내 주식 전용, 앱 내 가상 포트폴리오 기준. 엔진 산식과 프롬프트 정책은 별도 문서로 분리한다.
