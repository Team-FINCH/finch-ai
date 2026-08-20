# AI 서비스 API 계약 — 0-5 협의용 제안

> **이 문서는 제안이지 확정이 아닙니다.**
> AI 서비스가 **이미 구현해 운영 중인** 규약을 그대로 정리한 것입니다.
> 0-5 공통 API 규격(FINCH-23)이 다르게 확정되면 AI 파트가 맞춥니다.
> 백지에서 설계하는 대신 이걸 놓고 고치는 편이 빠를 것 같아 먼저 올립니다.
>
> 관련: FINCH-70 · 선행 대상 FINCH-23 · 후속 FINCH-30

---

## 1. 경로 접두사

```
/api/ai/v1
```

`app/api/main.py` 의 `API_PREFIX`. 모든 라우터가 이 아래 붙습니다.

### 현재 엔드포인트

| 메서드 | 경로 | 용도 |
|---|---|---|
| `POST` | `/stocks/{ticker}/analysis` | 종목 분석 |
| `POST` | `/chat` | 대화 에이전트 |
| `POST` | `/portfolio/diagnosis` | 포트폴리오 진단 |
| `POST` | `/portfolio/attribution` | 성과 기여도 분석 |
| `POST` | `/orders/preview` | 주문 전 점검 |
| `GET` | `/briefing` | 데일리 브리핑 |
| `GET` | `/wiki` | 사용자 위키 조회 |
| `POST` | `/wiki/theses` | 논지 등록 |
| `PUT` | `/wiki/theses/{ticker}` | 논지 수정 |
| `DELETE` | `/wiki/facts/{fact_id}` | 사실 삭제 |
| `POST` | `/feedback` | 응답 피드백 |
| `GET` | `/health` | 헬스체크 (접두사 없음) |

---

## 2. 성공 응답 봉투

모든 성공 응답은 아래 봉투로 감쌉니다. `content` 만 엔드포인트마다 다릅니다.

```json
{
  "request_id": "...",
  "generated_at": "2026-08-20T14:30:00+09:00",
  "data_as_of": { "price": "...", "news": "...", "macro": "..." },
  "model": "gpt-5.4-mini",
  "cached": false,
  "content": { },
  "citations": [],
  "disclaimer": "투자 판단의 최종 책임은 본인에게 있습니다."
}
```

| 필드 | 이유 |
|---|---|
| `request_id` | 장애 추적. 에러 응답에도 동일 필드가 있어 짝을 맞춥니다 |
| `data_as_of` | **금융 서비스라 필수입니다.** 시세·뉴스·거시 지표의 기준 시각이 다르고, 화면에 "몇 시 기준"을 못 쓰면 오해를 삽니다 |
| `model` · `cached` | 같은 질문에 다른 답이 나올 때 원인 구분용 |
| `citations` | 근거 표시. 없으면 LLM 출력이 사실처럼 보입니다 |
| `disclaimer` | 투자 자문 회피 문구. 응답마다 실려야 합니다 |

**백엔드가 그대로 프록시할지, 벗겨서 팀 공통 봉투로 다시 감쌀지 정해주셔야 합니다.**
벗기신다면 `data_as_of` · `citations` · `disclaimer` 는 어떤 형태로든 프런트까지 전달되어야 합니다.

---

## 3. 에러 응답

```json
{
  "error": { "code": "LLM_TIMEOUT", "message": "...", "detail": {} },
  "request_id": "..."
}
```

### 에러 코드와 HTTP 상태

`app/core/errors.py` 의 `STATUS_BY_CODE` 그대로입니다.

| 코드 | 상태 | 발생 상황 |
|---|---|---|
| `INVALID_REQUEST` | 400 | 요청 형식 오류 |
| `UNSUPPORTED_MARKET` | 400 | 해외 종목 요청 (국내 주식 전용) |
| `UNAUTHORIZED` | 401 | 인증 실패 |
| `INSTRUMENT_NOT_FOUND` | 404 | 없는 종목 |
| `INSUFFICIENT_DATA` | 409 | 분석에 필요한 데이터 부족 |
| `GUARDRAIL_BLOCKED` | 422 | 출력 가드레일 차단 |
| `RATE_LIMITED` | 429 | 호출 한도 초과 |
| `RETRIEVAL_FAILED` | 502 | 근거 검색 실패 |
| `LLM_TIMEOUT` | 504 | LLM 응답 시간 초과 |

HTTP 상태는 `errors.py` 한 곳에서만 결정하고 라우터가 직접 지정하지 않습니다.

### 프런트가 다르게 다뤄야 하는 것들

아래 넷은 "그냥 실패"가 아니라 **화면에서 다른 문구가 나가야 합니다.** 뭉뚱그리면 사용자가 재시도할지 포기할지 판단할 수 없습니다.

| 코드 | 프런트 처리 |
|---|---|
| `INSUFFICIENT_DATA` | 재시도해도 소용없음 — 데이터가 쌓여야 함을 안내 |
| `GUARDRAIL_BLOCKED` | 답변 거부. 재시도 유도하지 않음 |
| `LLM_TIMEOUT` | 재시도 가능 |
| `RETRIEVAL_FAILED` | 근거 없이 답할 수 없음. 재시도 가능 |

이게 3-4(FINCH-56)의 본체이므로 코드 체계가 바뀌면 그쪽도 같이 바뀝니다.

---

## 4. 인증 — **결정이 필요한 부분**

현재 `app/api/deps.py` 는 Bearer 토큰을 받는 골격만 있고, **검증은 미구현**입니다.

```python
# TODO: 백엔드와 서명 키를 맞춘 뒤 실제 JWT 검증으로 교체
```

```
Authorization: Bearer <token>
```

### 백엔드가 정해주셔야 하는 것

1. **AI 서비스가 JWT 를 직접 검증하는가**, 아니면 백엔드가 검증 후 신뢰된 사용자 식별자를 헤더로 넘기는가
2. 직접 검증이면 — 서명 알고리즘, 공개키 배포 방식, `iss` / `aud` 값
3. 헤더 전달이면 — 헤더 이름과 값 형식, 그리고 **AI 서비스가 외부에 직접 노출되지 않는다는 보장**
4. 사용자 식별자를 담는 클레임 이름

개인 위키·포트폴리오·피드백이 전부 사용자별 데이터라, 이게 확정되기 전에는 0-12 를 끝낼 수 없습니다.

---

## 5. 요약 — 결정이 필요한 항목

| # | 항목 | 결정 주체 |
|---|---|---|
| 1 | 응답 봉투를 프록시할지 재포장할지 | 백엔드 |
| 2 | 재포장 시 `data_as_of` · `citations` · `disclaimer` 전달 방식 | 백엔드 · 프런트 |
| 3 | 에러 코드 9종을 팀 공통 코드로 쓸지, 매핑 테이블을 둘지 | 백엔드 |
| 4 | JWT 직접 검증 vs 신뢰 헤더 전달 | 백엔드 |
| 5 | 사용자 식별자 클레임 · 헤더 이름 | 백엔드 |
| 6 | 경로 접두사 `/api/ai/v1` 유지 여부 | 백엔드 |

1·3·4번이 정해지면 0-12(FINCH-30)를 바로 진행할 수 있고, 그 뒤 2-10 · 3-3 · 3-4 가 열립니다.

---

## 참고

- 실제 스키마 전문: `ai/docs/api-spec.md`, `ai/docs/openapi.json`
- 에러 정의: `ai/app/core/errors.py`
- 봉투 정의: `ai/app/core/schemas.py`
- 인증 골격: `ai/app/api/deps.py`
