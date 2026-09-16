"""오류·타임아웃·근거 누락이 계약대로 표면화되는지.

프런트는 이 넷을 서로 다른 문구로 처리한다(계약서 §3). 뭉뚱그리면 사용자가
재시도할지 포기할지 판단할 수 없다.

| 코드 | 뜻 | 프런트 |
| --- | --- | --- |
| `INSUFFICIENT_DATA` | 데이터가 쌓여야 함 | 재시도 무의미 |
| `GUARDRAIL_BLOCKED` | 답변 거부 | 재시도 유도 안 함 |
| `LLM_TIMEOUT` | 모델이 늦음 | 재시도 가능 |
| `RETRIEVAL_FAILED` | 근거 검색이 고장남 | 재시도 가능 |

특히 마지막 둘을 조용한 성공으로 삼키면 안 된다. 검색이 죽은 채로
"관련 자료를 찾지 못했습니다"가 나가면 사용자는 자료가 없는 줄 안다.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.core.adapters import Ledger
from app.core.db import get_session
from app.core.errors import LLMTimeout, RetrievalFailed
from app.llm.client import LlmResult

STOCKS_URL = "/api/ai/v1/stocks/005930/analysis"
CHAT_URL = "/api/ai/v1/chat"
HOLDER = "golden_1_single"
AUTH = {"X-User-Id": HOLDER}


class _Session:
    """응답 로그만 받아 넘긴다. 이 파일은 저장을 검증하지 않는다."""

    def add(self, obj: Any) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def scalars(self, statement: Any) -> Any:
        class _Empty:
            def all(self) -> list[Any]:
                return []

        return _Empty()

    async def scalar(self, statement: Any) -> Any:
        return None


def _app(monkeypatch, **patches: Any) -> TestClient:
    for target, value in patches.items():
        monkeypatch.setattr(target.replace("__", "."), value)
    app = create_app()
    app.dependency_overrides[get_session] = _Session

    # 동기 /chat 은 작업 실행기를 거친다. 실행기는 자기 DB 세션을 여니 여기서는
    # 가짜 세션으로 답만 만들고, 예외는 실행기와 같은 (status, result, error) 로 옮긴다.
    async def _inline_run(job, guard):
        from app.api.routes.chat import ChatRequest, answer_question
        from app.core.errors import AppError

        body = ChatRequest(
            conversation_id=job.conversation_id, message=job.question, context=job.context
        )
        try:
            envelope = await answer_question(body, job.user_id, _Session())
        except AppError as exc:
            return "failed", None, {"code": exc.code.value, "message": exc.message}
        return "completed", envelope.model_dump(mode="json"), None

    monkeypatch.setattr("app.chat_jobs.run_job", _inline_run)
    return TestClient(app)


async def _timeout(*_: Any, **__: Any) -> LlmResult:
    raise LLMTimeout("LLM 응답이 지연되어 중단했습니다.")


async def _retrieval_failed(*_: Any, **__: Any) -> list[dict]:
    raise RetrievalFailed("근거 검색에 실패했습니다.")


async def _no_hits(*_: Any, **__: Any) -> list[dict]:
    return []


class _LiveClient:
    """NullLlmClient만 아니면 된다. 이 자리의 테스트는 LLM에 닿기 전에 끝난다."""

    async def generate(self, **_: Any) -> LlmResult:
        return LlmResult(payload={"narrative": "", "used_placeholders": [], "used_citations": []})


class _TimingOutClient:
    async def generate(self, **_: Any) -> LlmResult:
        raise LLMTimeout("LLM 응답이 지연되어 중단했습니다.")

    async def converse(self, **_: Any) -> Any:
        raise LLMTimeout("LLM 응답이 지연되어 중단했습니다.")


# ── 종목 분석 ─────────────────────────────────────────────────────────────
def test_종목분석_LLM_타임아웃은_504다(monkeypatch):
    client = _app(
        monkeypatch,
        app__api__routes__stocks__get_llm_client=lambda: _TimingOutClient(),
        app__api__routes__stocks__search=_no_hits,
    )
    res = client.post(STOCKS_URL, json={"sections": ["current"]}, headers=AUTH)
    assert res.status_code == 504
    assert res.json()["code"] == "LLM_TIMEOUT"


def test_종목분석_근거_검색_실패는_502다(monkeypatch):
    """검색이 고장난 것과 자료가 없는 것은 다르다. 502는 재시도해 볼 값어치가 있다."""
    client = _app(
        monkeypatch,
        app__api__routes__stocks__get_llm_client=lambda: _LiveClient(),
        app__api__routes__stocks__search=_retrieval_failed,
    )
    res = client.post(STOCKS_URL, json={"sections": ["current"]}, headers=AUTH)
    assert res.status_code == 502
    assert res.json()["code"] == "RETRIEVAL_FAILED"


def test_종목분석_LLM_키가_없으면_409다(monkeypatch):
    """엔진 데이터 부족은 재시도해도 소용없다. 프롬프트 정책 §7."""
    client = _app(
        monkeypatch,
        app__api__routes__stocks__search=_no_hits,
    )
    res = client.post(STOCKS_URL, json={"sections": ["current"]}, headers=AUTH)
    assert res.status_code == 409
    assert res.json()["code"] == "INSUFFICIENT_DATA"


@pytest.mark.parametrize(
    ("endpoint", "payload"),
    [
        ("portfolio/diagnosis", {}),
        ("portfolio/attribution", {"period": "1d"}),
        ("orders/preview", {"orders": [{"ticker": "005930", "side": "buy", "quantity": 1}]}),
    ],
)
@pytest.mark.parametrize(
    "source_state", ["empty", "disabled", "missing_user", "missing_file", "io_error"]
)
def test_빈_원장과_읽기_실패를_구분한다(monkeypatch, endpoint, payload, source_state):
    """_ledger를 우회하지 않고 로드 결과부터 세 API의 detail.reason까지 검증한다."""

    class Source:
        async def load(self, user_id: str) -> Ledger:
            failures = {
                "missing_user": KeyError,
                "missing_file": FileNotFoundError,
                "io_error": OSError,
            }
            if source_state in failures:
                raise failures[source_state]("unavailable")
            return Ledger(user_id=user_id, trading_days=(), instruments={}, prices={})

    source = None if source_state == "disabled" else Source()
    with _app(
        monkeypatch,
        app__api__routes__portfolio__ledger_source=lambda: source,
    ) as client:
        response = client.post(f"/api/ai/v1/{endpoint}", json=payload, headers=AUTH)

    assert response.status_code == 409
    body = response.json()
    assert set(body) == {"code", "message", "detail", "request_id"}
    assert isinstance(body["message"], str) and body["message"]
    assert isinstance(body["request_id"], str) and body["request_id"]
    assert body["code"] == "INSUFFICIENT_DATA"
    if source_state == "empty":
        assert body["detail"] == {}
        assert "거래일" in body["message"]
    else:
        assert body["detail"] == {"reason": "ledger_unavailable"}


# ── 대화 ─────────────────────────────────────────────────────────────────
def test_대화_LLM_타임아웃은_504다(monkeypatch):
    client = _app(
        monkeypatch,
        app__api__routes__chat__get_llm_client=lambda: _TimingOutClient(),
    )
    res = client.post(CHAT_URL, json={"message": "PER이 뭐야?"}, headers=AUTH)
    assert res.status_code == 504
    assert res.json()["code"] == "LLM_TIMEOUT"


# ── 응답 형식 ─────────────────────────────────────────────────────────────
def test_에러는_최상위_code_message_detail이다(monkeypatch):
    """백엔드 §1.3 형식. 예전처럼 error 로 한 겹 감싸지 않는다."""
    client = _app(monkeypatch)
    body = client.post(STOCKS_URL, json={}, headers={"X-User-Id": "u"}).json()

    assert "error" not in body
    assert set(body) >= {"code", "message", "detail"}
    # request_id 는 §1.3 에 없지만 남긴다. /feedback 이 이 값으로 응답을 찾는다.
    assert body["request_id"]


def test_사용자에게_보이는_문구에_내부_용어를_넣지_않는다(monkeypatch):
    """백엔드가 message 를 프런트에 그대로 노출한다(§1.3).

    "LLM 키가 없습니다" 같은 문구는 사용자에게 아무 의미가 없고 설정을 흘린다.
    사유는 detail.reason 으로 보낸다.
    """
    client = _app(monkeypatch)
    body = client.post(STOCKS_URL, json={}, headers={"X-User-Id": "u"}).json()

    for word in ("LLM", "원장", "토큰", "임베딩", "adapter", "None", "Traceback"):
        assert word not in body["message"], body["message"]
    assert body["message"].endswith("다.")


# ── 검색 계층 ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_임베더가_없으면_빈_결과가_아니라_실패다(monkeypatch):
    """0건으로 뭉개면 '관련 자료를 찾지 못했습니다'가 태연히 나간다."""
    from app.rag import search as search_mod
    from app.rag.embedding import NullEmbedder

    monkeypatch.setattr(search_mod, "get_embedder", NullEmbedder)
    with pytest.raises(RetrievalFailed):
        await search_mod.search("삼성전자 실적")


@pytest.mark.asyncio
async def test_질의_임베딩_실패도_실패로_올린다(monkeypatch):
    from app.rag import search as search_mod

    class _Broken:
        def embed(self, _texts: list[str]) -> list[None]:
            return [None]

    monkeypatch.setattr(search_mod, "get_embedder", _Broken)
    with pytest.raises(RetrievalFailed):
        await search_mod.search("삼성전자 실적")
