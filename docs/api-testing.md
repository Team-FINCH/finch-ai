# API 테스트 가이드

이 저장소 밖에서 API를 두드려 보려는 사람을 위한 문서다. 계약은
[`docs/openapi.json`](openapi.json)에 있다. FastAPI가 만든 스키마를 그대로
내보낸 것이라 코드가 진실이고, 이 파일은 그 사본이다.

Postman 기준으로 쓰지만 OpenAPI를 읽는 도구면 무엇이든 된다.

## 1. 서버 띄우기

설치와 실행 절차는 [README](../README.md)에 있다. 그대로 따르면
`http://localhost:8000`에 뜬다.

살아 있는지부터 확인한다. 이 엔드포인트만 토큰이 필요 없다.

```bash
curl http://localhost:8000/health
# {"status":"ok","env":"local","model":"..."}
```

서버가 떴으면 `http://localhost:8000/docs`에서 Swagger UI로 바로 눌러 볼 수도
있다. 한 번 확인하고 말 거면 임포트할 것 없이 이쪽이 빠르다. 아래 절차는
Postman에 컬렉션으로 두고 계속 쓰려는 경우다.

## 2. Postman에 넣기

1. Postman에서 **Import** → `docs/openapi.json` 선택
2. OpenAPI 3 문서로 인식되면 컬렉션 생성을 고른다
3. 엔드포인트 12개가 폴더로 들어온다

`servers`에 `http://localhost:8000`이 들어 있어서 baseUrl은 이미 채워져 있다.
다른 데를 보게 하려면 컬렉션 변수만 바꾼다.

> 유료 기능이나 Collection Runner는 쓰지 않는다. 무료 플랜에서 임포트와 수동
> 요청은 제한이 없고, 러너만 월 25회로 묶여 있다. 이 문서의 절차는 전부 수동
> 요청이라 걸릴 일이 없다.

## 3. 토큰 설정

인증은 `Authorization: Bearer <token>` 하나뿐이다. **사용자 식별자는 토큰에서만
읽는다** — 경로나 본문에 사용자 ID를 넣는 자리는 없고, 넣어도 무시된다.

스키마에 `bearerAuth`가 선언돼 있어서 Postman이 컬렉션 단위 Bearer 인증을
자동으로 잡아 준다. 컬렉션 **Authorization** 탭에서 토큰 값만 채우면 하위
요청이 전부 물려받는다.

개발 환경에서는 **비어 있지 않은 아무 문자열이나 통과한다.** `u_test`면 된다.
값마다 다른 사용자로 취급되니, 위키처럼 사용자별 데이터를 보는 엔드포인트는
같은 토큰으로 계속 부르는 편이 편하다.

```bash
curl http://localhost:8000/api/ai/v1/wiki -H "Authorization: Bearer u_test"
```

토큰을 빼면 401이 온다. 이건 정상 동작이다.

```json
{"error": {"code": "UNAUTHORIZED", "message": "인증 토큰이 없습니다.", "detail": {}},
 "request_id": "req_..."}
```

## 4. 엔드포인트

전부 `/api/ai/v1` 아래에 있다. `/health`만 예외다.

| 메서드 | 경로 | 본문 | 비고 |
| --- | --- | --- | --- |
| GET | `/health` | | 토큰 불필요 |
| POST | `/stocks/{ticker}/analysis` | 선택 | `sections`, `personalize` |
| POST | `/chat` | 필수 | `message` 필수 |
| POST | `/portfolio/diagnosis` | 없음 | |
| POST | `/portfolio/attribution` | 필수 | |
| POST | `/orders/preview` | 필수 | 가상 주문 — 실제 체결은 없다 |
| GET | `/briefing` | | `date` 쿼리 파라미터 |
| GET | `/wiki` | 없음 | 토큰 주인의 위키 |
| POST | `/wiki/theses` | 필수 | |
| PUT | `/wiki/theses/{ticker}` | 필수 | |
| DELETE | `/wiki/facts/{fact_id}` | 없음 | |
| POST | `/feedback` | 필수 | |

필드 정의는 스키마를 보는 게 빠르다. Postman이 요청 본문 예시를 만들어 준다.

### 처음 눌러 볼 것

```
GET  /health                          토큰 없이, 서버 확인
GET  /wiki                            토큰 붙여서, 인증 확인
POST /api/ai/v1/chat                  {"message": "삼성전자 어때?"}
```

## 5. 에러 읽는 법

에러는 형태가 하나다. `request_id`는 로그를 찾을 때 쓴다.

```json
{"error": {"code": "...", "message": "...", "detail": {}}, "request_id": "req_..."}
```

| code | HTTP | 뜻 |
| --- | --- | --- |
| `INVALID_REQUEST` | 400 | 요청 형식 오류 |
| `UNSUPPORTED_MARKET` | 400 | 국내 주식만 다룬다 |
| `UNAUTHORIZED` | 401 | 토큰 없음 |
| `INSTRUMENT_NOT_FOUND` | 404 | 없는 종목 |
| `INSUFFICIENT_DATA` | 409 | 근거 데이터가 모자람 |
| `GUARDRAIL_BLOCKED` | 422 | 출력 가드에 걸림 |
| `RATE_LIMITED` | 429 | |
| `RETRIEVAL_FAILED` | 502 | 검색 실패 |
| `LLM_TIMEOUT` | 504 | |

### 409는 버그가 아니다

**API 키와 적재된 데이터가 없으면 대부분의 엔드포인트가 409
`INSUFFICIENT_DATA`를 돌려준다.** 설정이 덜 된 환경에서 보게 되는 정상
응답이다. 이 서비스는 근거가 없으면 답을 지어내지 않고 거절한다.

200을 보려면 `.env`에 키를 채우고 데이터를 적재해야 한다. 그 전까지는
요청/응답 **형태**를 확인하는 용도로 쓰면 된다 — 409도 위의 에러 봉투를
그대로 따르므로 연동 코드를 붙이기에는 충분하다.

## 6. 스키마가 바뀌면

`docs/openapi.json`은 **손으로 고치지 않는다.** 라우터나 스키마를 바꿨으면
다시 뽑아서 같이 커밋한다.

```bash
python -m scripts.export_openapi            # 다시 생성
python -m scripts.export_openapi --check    # 코드와 같은지 확인
```

CI가 `--check`를 돌린다. 코드만 고치고 재생성을 잊으면 거기서 막힌다.
저장소 밖 사람이 낡은 계약을 읽는 것보다 PR이 빨간 게 낫다.
