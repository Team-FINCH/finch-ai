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

    async def scalar(self, statement: Any) -> Any:
        return None


def _app(monkeypatch, **patches: Any) -> TestClient:
    for target, value in patches.items():
        monkeypatch.setattr(target.replace("__", "."), value)
    app = create_app()
    app.dependency_overrides[get_session] = _Session
    return TestClient(app)


async def _timeout(*_: Any, **__: Any) -> LlmResult:
    raise LLMTimeout("LLM 응답이 지연되어 중단했습니다.")


async def _retrieval_failed(*_: Any, **__: Any) -> list[dict]:
    raise RetrievalFailed("근거 검색에 실패했습니다.")


async def _no_hits(*_: Any, **__: Any) -> list[dict]:
    return []


async def _no_thesis(*_: Any, **__: Any) -> None:
    return None


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
        app__api__routes__stocks__get_active_thesis=_no_thesis,
    )
    res = client.post(STOCKS_URL, json={"sections": ["current"]}, headers=AUTH)
    assert res.status_code == 504
    assert res.json()["error"]["code"] == "LLM_TIMEOUT"


def test_종목분석_근거_검색_실패는_502다(monkeypatch):
    """검색이 고장난 것과 자료가 없는 것은 다르다. 502는 재시도해 볼 값어치가 있다."""
    client = _app(
        monkeypatch,
        app__api__routes__stocks__get_llm_client=lambda: _LiveClient(),
        app__api__routes__stocks__search=_retrieval_failed,
        app__api__routes__stocks__get_active_thesis=_no_thesis,
    )
    res = client.post(STOCKS_URL, json={"sections": ["current"]}, headers=AUTH)
    assert res.status_code == 502
    assert res.json()["error"]["code"] == "RETRIEVAL_FAILED"


def test_종목분석_LLM_키가_없으면_409다(monkeypatch):
    """엔진 데이터 부족은 재시도해도 소용없다. 프롬프트 정책 §7."""
    client = _app(
        monkeypatch,
        app__api__routes__stocks__search=_no_hits,
        app__api__routes__stocks__get_active_thesis=_no_thesis,
    )
    res = client.post(STOCKS_URL, json={"sections": ["current"]}, headers=AUTH)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "INSUFFICIENT_DATA"


# ── 대화 ─────────────────────────────────────────────────────────────────
def test_대화_LLM_타임아웃은_504다(monkeypatch):
    client = _app(
        monkeypatch,
        app__api__routes__chat__get_llm_client=lambda: _TimingOutClient(),
    )
    res = client.post(CHAT_URL, json={"message": "PER이 뭐야?"}, headers=AUTH)
    assert res.status_code == 504
    assert res.json()["error"]["code"] == "LLM_TIMEOUT"


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
