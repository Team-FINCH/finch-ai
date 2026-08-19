"""임베딩 구현 테스트.

네트워크 없이 돈다. 사내 게이트웨이 주소를 모르는 상태에서도 계약을 고정해두려는
것이라, 실제 응답 대신 httpx.MockTransport로 OpenAI 스키마를 흉내낸다.
"""

from __future__ import annotations

import httpx
import pytest

from app.rag.embedding import (
    OPENAI_DEFAULT_BASE_URL,
    Embedder,
    NullEmbedder,
    OpenAIEmbedder,
)

DIM = 4


def _ok(request: httpx.Request) -> httpx.Response:
    body = request.read().decode()
    import json

    payload = json.loads(body)
    n = len(payload["input"])
    dim = payload["dimensions"]
    # 순서를 일부러 뒤집어 보낸다. 구현이 index로 되돌리는지 보려는 것이다.
    data = [{"index": i, "embedding": [float(i)] * dim} for i in reversed(range(n))]
    return httpx.Response(200, json={"data": data, "model": payload["model"]})


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_null_embedder_returns_none_not_zeros() -> None:
    """0 벡터를 채우면 유사도 검색이 조용히 엉뚱한 결과를 낸다."""
    assert NullEmbedder().embed(["가", "나"]) == [None, None]


def test_requires_api_key() -> None:
    with pytest.raises(ValueError):
        OpenAIEmbedder("")


def test_default_base_url_is_openai() -> None:
    e = OpenAIEmbedder("k")
    assert e._url == OPENAI_DEFAULT_BASE_URL


def test_gateway_base_url_overrides() -> None:
    """사내 게이트웨이도 같은 스키마라 주소만 바꾸면 붙는다."""
    e = OpenAIEmbedder("k", base_url="https://gw.example.com/v1/")
    assert e._url == "https://gw.example.com/v1"


def test_embeds_in_request_order_not_response_order() -> None:
    e = OpenAIEmbedder("k", dim=DIM, client=_client(_ok))
    out = e.embed(["a", "b", "c"])
    assert out == [[0.0] * DIM, [1.0] * DIM, [2.0] * DIM]


def test_sends_dimensions_so_schema_stays_1024() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.read().decode()))
        return _ok(request)

    OpenAIEmbedder("k", dim=DIM, model="text-embedding-3-small", client=_client(handler)).embed(["a"])
    assert seen["dimensions"] == DIM, "dimensions를 보내지 않으면 기본 1536이 와서 INSERT가 거부된다"
    assert seen["model"] == "text-embedding-3-small"


def test_batches_are_split() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        calls.append(len(json.loads(request.read().decode())["input"]))
        return _ok(request)

    e = OpenAIEmbedder("k", dim=DIM, batch_size=2, client=_client(handler))
    out = e.embed(["a", "b", "c", "d", "e"])
    assert calls == [2, 2, 1]
    assert all(v is not None for v in out)


def test_failed_batch_leaves_none_and_keeps_the_rest() -> None:
    """한 배치가 실패해도 나머지는 살린다. None 자리는 재실행 때 다시 시도된다."""
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 2:
            return httpx.Response(500, json={"error": "boom"})
        return _ok(request)

    e = OpenAIEmbedder("k", dim=DIM, batch_size=2, client=_client(handler))
    out = e.embed(["a", "b", "c", "d", "e", "f"])
    assert [v is None for v in out] == [False, False, True, True, False, False]


def test_short_vector_is_rejected() -> None:
    """짧게 오면 늘릴 방법이 없다. INSERT까지 가기 전에 잡는다."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    e = OpenAIEmbedder("k", dim=DIM, client=_client(handler))
    assert e.embed(["a"]) == [None]


def test_long_vector_is_truncated_and_renormalized() -> None:
    """게이트웨이가 dimensions를 통과시키지 않으면 기본 1536이 돌아온다.

    text-embedding-3은 Matryoshka 학습이라 뒤를 잘라내고 L2 정규화하면 된다.
    OpenAI가 문서화한 축소 방법이라 검색 품질 손실이 작다.
    """
    import math

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [3.0] * (DIM * 4)}]}
        )

    out = OpenAIEmbedder("k", dim=DIM, client=_client(handler)).embed(["a"])
    assert out[0] is not None
    assert len(out[0]) == DIM
    assert math.isclose(math.sqrt(sum(x * x for x in out[0])), 1.0, abs_tol=1e-9)


def test_count_mismatch_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.0] * DIM}]})

    e = OpenAIEmbedder("k", dim=DIM, client=_client(handler))
    assert e.embed(["a", "b"]) == [None, None]


def test_is_an_embedder() -> None:
    assert isinstance(OpenAIEmbedder("k"), Embedder)
    assert isinstance(NullEmbedder(), Embedder)
