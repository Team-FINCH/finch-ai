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
    _seconds,
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


# ── 분당 토큰 한도 대응 ───────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("35.161s", 35.161),        # x-ratelimit-reset-tokens
        ("23h59m48.732s", 86388.732),
        ("161ms", 0.161),
        ("1", 1.0),                 # Retry-After는 맨숫자로 온다
        (None, 0.0),
        ("알 수 없음", 0.0),
    ],
)
def test_한도_리셋_시간을_초로_읽는다(raw: str | None, want: float) -> None:
    assert _seconds(raw) == pytest.approx(want)


def test_429를_맞으면_리셋만큼_쉬고_재시도한다(monkeypatch: pytest.MonkeyPatch) -> None:
    """429도 일당 요청 한도를 깎는다. 그냥 실패시키면 재실행 때 또 깎는다."""
    slept: list[float] = []
    monkeypatch.setattr("app.rag.embedding.time.sleep", slept.append)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"x-ratelimit-reset-tokens": "12s"})
        return _ok(request)

    emb = OpenAIEmbedder("k", dim=DIM, client=_client(handler))
    assert emb.embed(["가"])[0] is not None
    assert slept == [12.0]


def test_잔량이_모자라면_미리_쉰다(monkeypatch: pytest.MonkeyPatch) -> None:
    """429를 맞고 물러나는 것보다 헤더를 보고 먼저 쉬는 편이 싸다."""
    slept: list[float] = []
    monkeypatch.setattr("app.rag.embedding.time.sleep", slept.append)

    def handler(request: httpx.Request) -> httpx.Response:
        res = _ok(request)
        return httpx.Response(
            200,
            json=res.json() | {"usage": {"prompt_tokens": 100}},
            headers={"x-ratelimit-remaining-tokens": "5", "x-ratelimit-reset-tokens": "9s"},
        )

    emb = OpenAIEmbedder("k", dim=DIM, batch_size=1, client=_client(handler))
    emb.embed(["가" * 50, "나" * 50])
    # 첫 배치는 잔량을 모르니 그냥 보내고, 두 번째부터 5토큰 잔량을 보고 쉰다.
    assert slept == [10.0]


def test_실제_사용량으로_문자당_토큰을_학습한다() -> None:
    """문자 수를 그대로 토큰으로 세면 과대평가라 필요 이상으로 쉰다."""

    def handler(request: httpx.Request) -> httpx.Response:
        res = _ok(request)
        return httpx.Response(200, json=res.json() | {"usage": {"prompt_tokens": 7}})

    emb = OpenAIEmbedder("k", dim=DIM, client=_client(handler))
    emb.embed(["가" * 10])
    assert emb._per_char == pytest.approx(0.7)
