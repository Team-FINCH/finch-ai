"""`POST /portfolio/attribution` 계약과 Attribution Engine 항등식 (§4 · §6).

엔진 단위 검사는 순수 함수라 픽스처 없이 직접 부른다. 라우터 검사는 진단 쪽과 같은
방식으로 시드 원장 + 스텁 세션 위에서 돈다 — DB가 없어도 계약이 지켜지는지를 본다.
"""

from __future__ import annotations

import asyncio
import math
import re
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_session
from app.api.main import create_app
from app.core.models import AIFeedback, AIResponse
from app.core.schemas import now_kst
from app.engines.attribution import (
    BenchmarkDay,
    EventRecord,
    attribute,
    carino_factors,
    match_events,
)
from app.llm.client import LlmResult, NullLlmClient

URL = "/api/ai/v1/portfolio/attribution"
HOLDER = "mixed_with_cash"
STRANGER = "nobody"

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")

#: 시드 원장 구간. 8종목 전부 2025-09-01에 매수되어 있다.
DAYS = [date(2025, 9, 1) + timedelta(days=i) for i in range(12)]
SEED_TICKERS = {
    "005930": "반도체",
    "000660": "반도체",
    "042700": "반도체",
    "035420": "인터넷",
    "105560": "은행",
    "068270": "바이오/제약",
    "005380": "자동차",
    "373220": "2차전지",
}


# ── 엔진 ──────────────────────────────────────────────────────────────────────
def _bench(days, weights, rate=0.004):
    """섹터 수익률이 모두 같은 단조로운 벤치마크. 항등식 검사에는 이걸로 충분하다."""
    return [BenchmarkDay(day, weights, dict.fromkeys(weights, rate)) for day in days]


def _identity_gap(result) -> float:
    return abs(result.market + result.sector + result.selection - result.portfolio_return)


@pytest.mark.parametrize("span", [1, 2, 5, 12])
def test_항등식이_구간_길이와_무관하게_성립한다(span: int) -> None:
    """§6.3 market + sector + selection = r_p. 링킹이 붙는 다기간에서도 같다."""
    days = DAYS[:span]
    weights = [{"005930": 0.5, "035420": 0.3} for _ in days]
    returns = [{"005930": 0.01 * (i + 1), "035420": -0.005 * (i + 1)} for i, _ in enumerate(days)]
    result = attribute(
        trading_days=days,
        portfolio_weights=weights,
        portfolio_returns=returns,
        benchmark=_bench(days, {"반도체": 0.7, "인터넷": 0.3}),
        sectors={"005930": "반도체", "035420": "인터넷"},
    )
    assert _identity_gap(result) < 1e-12


def test_기여도_합이_기간_수익률과_같다() -> None:
    """§4.3 Carino. 단순 합산이면 복리 효과만큼 어긋난다."""
    days = DAYS[:10]
    weights = [{"005930": 0.6, "035420": 0.4} for _ in days]
    returns = [{"005930": 0.02, "035420": -0.01} for _ in days]
    result = attribute(
        trading_days=days,
        portfolio_weights=weights,
        portfolio_returns=returns,
        benchmark=_bench(days, {"반도체": 0.6, "인터넷": 0.4}),
        sectors={"005930": "반도체", "035420": "인터넷"},
    )
    linked = sum(row.contribution for row in result.contributors)
    assert linked == pytest.approx(result.portfolio_return, abs=1e-12)

    naive = sum(0.6 * 0.02 + 0.4 * -0.01 for _ in days)
    assert abs(naive - result.portfolio_return) > 1e-6  # 단순 합산은 실제로 어긋난다


def test_하루_구간은_링킹을_건너뛴다() -> None:
    """§4.3 — 1d는 k/K가 1이라 기여도가 곧 w×r이다."""
    result = attribute(
        trading_days=DAYS[:1],
        portfolio_weights=[{"005930": 0.5}],
        portfolio_returns=[{"005930": 0.04}],
        benchmark=_bench(DAYS[:1], {"반도체": 1.0}),
        sectors={"005930": "반도체"},
    )
    assert result.contributors[0].contribution == pytest.approx(0.02)
    assert any("링킹" in note for note in result.notes)


def test_벤치마크에_없는_섹터를_들고_있어도_항등식이_남는다() -> None:
    """w_j^b = 0인 섹터. 초과분은 배분이 아니라 선택으로 잡힌다."""
    days = DAYS[:5]
    weights = [{"005930": 0.5, "999999": 0.3} for _ in days]
    returns = [{"005930": 0.01, "999999": 0.05} for _ in days]
    result = attribute(
        trading_days=days,
        portfolio_weights=weights,
        portfolio_returns=returns,
        benchmark=_bench(days, {"반도체": 1.0}),
        sectors={"005930": "반도체", "999999": "우주개발"},
    )
    assert _identity_gap(result) < 1e-12

    orphan = next(e for e in result.sector_effects if e.sector == "우주개발")
    assert orphan.benchmark_weight == 0.0
    assert orphan.allocation == pytest.approx(0.0, abs=1e-12)
    assert orphan.selection > 0  # 벤치마크에 없는 걸 골라 이겼다


def test_벤치마크_편입_섹터의_당일_시세가_없어도_항등식이_남는다() -> None:
    """총수익률과 Brinson 분해가 누락 섹터 수익률을 모두 0%로 취급한다."""
    day = DAYS[:1]
    result = attribute(
        trading_days=day,
        portfolio_weights=[{"005930": 0.6, "035420": 0.4}],
        portfolio_returns=[{"005930": 0.01, "035420": -0.005}],
        benchmark=[
            BenchmarkDay(
                day[0],
                {"반도체": 0.7, "인터넷": 0.3},
                {"반도체": 0.004},
            )
        ],
        sectors={"005930": "반도체", "035420": "인터넷"},
    )

    assert _identity_gap(result) < 1e-12


def test_구간_중간에_산_종목이_목록에_남는다() -> None:
    """t-1 비중이 없다고 빼면 기여도 합이 기간 수익률과 어긋난다."""
    days = DAYS[:6]
    weights = [
        {"005930": 0.6} if i < 3 else {"005930": 0.6, "035420": 0.2} for i, _ in enumerate(days)
    ]
    returns = [{"005930": 0.01, "035420": 0.03} for _ in days]
    result = attribute(
        trading_days=days,
        portfolio_weights=weights,
        portfolio_returns=returns,
        benchmark=_bench(days, {"반도체": 0.7, "인터넷": 0.3}),
        sectors={"005930": "반도체", "035420": "인터넷"},
    )
    late = next(row for row in result.contributors if row.ticker == "035420")
    assert late.held_at_start is False
    assert late.weight == 0.0  # 기간 시작 시점 비중은 0이다
    assert late.contribution > 0  # 그래도 편입 이후 기여는 잡힌다
    assert sum(r.contribution for r in result.contributors) == pytest.approx(
        result.portfolio_return, abs=1e-12
    )


def test_현금을_섹터로_넣지_않으면_항등식이_깨진다() -> None:
    """비중 합이 1이 아니면 Brinson 항등식의 전제가 무너진다 — 엔진이 현금으로 메운다."""
    result = attribute(
        trading_days=DAYS[:1],
        portfolio_weights=[{"005930": 0.4}],  # 나머지 0.6은 현금
        portfolio_returns=[{"005930": 0.05}],
        benchmark=_bench(DAYS[:1], {"반도체": 1.0}, rate=0.01),
        sectors={"005930": "반도체"},
    )
    assert _identity_gap(result) < 1e-12
    assert result.portfolio_return == pytest.approx(0.02)
    assert "현금" in {e.sector for e in result.sector_effects}
    assert all(row.ticker != "현금" for row in result.contributors)  # 설명 대상은 아니다


def test_carino_계수는_수익률이_0이면_1이다() -> None:
    factors, scale = carino_factors([0.0, 0.0])
    assert factors == [1.0, 1.0]
    assert scale == 1.0


def test_거래일이_없으면_거절한다() -> None:
    with pytest.raises(ValueError, match="거래일이 없어"):
        attribute(
            trading_days=[],
            portfolio_weights=[],
            portfolio_returns=[],
            benchmark=[],
            sectors={},
        )


# ── §5.1 이벤트 매칭 ──────────────────────────────────────────────────────────
def _event(day: date, ticker: str | None, *, doc: str | None = None) -> EventRecord:
    return EventRecord("e1", "disclosure", "유상증자 결정", day, ticker, doc)


def test_같은_날_직접_공시가_가장_높은_신뢰도를_받는다() -> None:
    matched = match_events(
        [_event(DAYS[2], "005930", doc="d1")],
        ticker="005930",
        sector="반도체",
        anchor=DAYS[2],
        trading_days=DAYS[:5],
        sectors=SEED_TICKERS,
    )
    assert len(matched) == 1
    assert matched[0].matched_confidence == pytest.approx(1.0, abs=1e-4)
    assert matched[0].citation_id == "cit_d1"


def test_신뢰도가_거래일_간격에_따라_떨어진다() -> None:
    """§5.1 recency = exp(−Δt/τ), τ = 1 거래일."""

    def confidence(at: int) -> float:
        matched = match_events(
            [_event(DAYS[at], "005930")],
            ticker="005930",
            sector="반도체",
            anchor=DAYS[0],
            trading_days=DAYS[:5],
            sectors=SEED_TICKERS,
        )
        return matched[0].matched_confidence if matched else 0.0

    assert confidence(0) > confidence(1) > confidence(2)
    assert confidence(0) == pytest.approx((0.40 + 0.30) / 0.70, abs=1e-4)
    assert confidence(1) == pytest.approx((0.40 * math.exp(-1) + 0.30) / 0.70, abs=1e-4)


def test_같은_섹터_다른_종목_공시는_약하게_붙는다() -> None:
    matched = match_events(
        [_event(DAYS[0], "000660")],
        ticker="005930",
        sector="반도체",
        anchor=DAYS[0],
        trading_days=DAYS[:5],
        sectors=SEED_TICKERS,
    )
    assert matched[0].matched_confidence == pytest.approx((0.40 + 0.30 * 0.5) / 0.70, abs=1e-4)


def test_무관한_섹터_공시는_붙지_않는다() -> None:
    assert (
        match_events(
            [_event(DAYS[0], "105560")],
            ticker="005930",
            sector="반도체",
            anchor=DAYS[0],
            trading_days=DAYS[:5],
            sectors=SEED_TICKERS,
        )
        == ()
    )


def test_이벤트가_없는_구간은_빈_목록이다() -> None:
    """근거가 없으면 만들어내지 않는다."""
    result = attribute(
        trading_days=DAYS[:3],
        portfolio_weights=[{"005930": 1.0} for _ in DAYS[:3]],
        portfolio_returns=[{"005930": 0.01} for _ in DAYS[:3]],
        benchmark=_bench(DAYS[:3], {"반도체": 1.0}),
        sectors={"005930": "반도체"},
        events=[],
    )
    assert result.contributors[0].events == ()
    assert any("이벤트가 없" in note for note in result.notes)


def test_구간_밖_공시는_붙지_않는다() -> None:
    """거래일 목록에 없는 날짜는 거리 자체를 잴 수 없다."""
    assert (
        match_events(
            [_event(date(2024, 1, 1), "005930")],
            ticker="005930",
            sector="반도체",
            anchor=DAYS[0],
            trading_days=DAYS[:5],
            sectors=SEED_TICKERS,
        )
        == ()
    )


# ── 라우터 ────────────────────────────────────────────────────────────────────
class FakeClient:
    """턴에서 첫 자리표시자를 뽑아 두 문장을 만든다(성과 요인은 2~4문장)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate(self, **kwargs: Any) -> LlmResult:
        self.calls.append(kwargs)
        turn: str = kwargs["user"]
        keys = _PLACEHOLDER_RE.findall(turn.split("[사용 가능한 수치 자리표시자]")[-1])
        first = keys[0] if keys else None
        fact = (
            f"기간 수익률은 {{{{{first}}}}}입니다. " if first else "수치로 확인된 지표가 없습니다. "
        )
        return LlmResult(
            payload={
                "narrative": fact + "시장이 움직인 몫과 종목 선택이 만든 몫이 갈렸습니다.",
                "used_placeholders": [first] if first else [],
                "used_citations": [],
            }
        )


class StubResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return self._rows


class StubSession:
    """`instruments` · `price_daily` · `events` 조회에만 답하는 최소 세션.

    쿼리 문자열로 갈라 낸다 — 호출 순서에 기대면 인자 평가 순서가 바뀌는 순간
    조용히 엉뚱한 값을 넘긴다.
    """

    def __init__(
        self,
        *,
        instruments: list[tuple] | None = None,
        prices: list[tuple] | None = None,
        events: list[tuple] | None = None,
    ) -> None:
        self.instruments = instruments if instruments is not None else _default_instruments()
        self.prices = prices if prices is not None else _default_prices()
        self.events = events if events is not None else []
        self.added: list[Any] = []

    async def execute(self, statement: Any) -> StubResult:
        sql = str(statement)
        if "events" in sql:
            return StubResult(self.events)
        if "price_daily" in sql:
            return StubResult(self.prices)
        if "instruments" in sql:
            return StubResult(self.instruments)
        return StubResult([])

    async def scalar(self, statement: Any) -> Any:
        if "ai_responses" in str(statement):
            row = next((row for row in self.added if isinstance(row, AIResponse)), None)
            return row.user_id if row else None
        return next((row for row in self.added if isinstance(row, AIFeedback)), None)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _default_instruments() -> list[tuple]:
    """시드 8종목 + 포트폴리오가 안 든 종목 하나. 시장이 보유분보다 넓어야 의미가 있다."""
    caps = {t: 10_000_000 * (i + 1) for i, t in enumerate(SEED_TICKERS)}
    rows = [(t, SEED_TICKERS[t], caps[t]) for t in SEED_TICKERS]
    rows.append(("000270", "자동차", 50_000_000))
    return rows


#: 섹터마다 다른 일일 드리프트. 전부 같은 비율로 움직이면 배분 효과가 항상 0이 되어
#: allocation을 0으로 만드는 버그가 그대로 통과한다.
_SECTOR_DRIFT = {
    "반도체": 0.014,
    "인터넷": 0.006,
    "은행": -0.003,
    "바이오/제약": 0.002,
    "자동차": 0.009,
    "2차전지": -0.008,
}


def _default_prices() -> list[tuple]:
    """섹터별로 다르게 움직이는 시장. 배분 효과가 0이 아니게 나온다."""
    rows: list[tuple] = []
    for ticker, sector, _ in _default_instruments():
        drift = _SECTOR_DRIFT[sector]
        for offset in range(-14, 12):
            day = DAYS[0] + timedelta(days=offset)
            rows.append((ticker, day, 10_000 * ((1 + drift) ** offset)))
    return rows


def _make_client(monkeypatch: pytest.MonkeyPatch, session: Any) -> TestClient:
    fake = FakeClient()
    monkeypatch.setattr("app.api.routes.portfolio.get_llm_client", lambda: fake)
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    test_client = TestClient(app)
    test_client.llm = fake  # type: ignore[attr-defined]
    test_client.db = session  # type: ignore[attr-defined]
    return test_client


@pytest.fixture
def client(monkeypatch):
    with _make_client(monkeypatch, StubSession()) as test_client:
        yield test_client


def _post(client: TestClient, user: str, period: str = "1d") -> Any:
    return client.post(URL, json={"period": period}, headers={"X-User-Id": user})


def test_계약대로_돌려준다(client: TestClient) -> None:
    body = _post(client, HOLDER).json()
    content = body["content"]
    assert content["period"] == "1d"
    assert set(content["breakdown"]) == {"market", "sector", "selection"}
    assert content["summary"]["text"]
    assert content["text"] == content["summary"]["text"]
    assert body["data_as_of"]["price"]


def test_응답을_저장해_피드백을_받는다(client: TestClient) -> None:
    response = _post(client, HOLDER)
    request_id = response.json()["request_id"]

    row = next(row for row in client.db.added if isinstance(row, AIResponse))
    assert row.request_id == request_id
    assert row.endpoint == "portfolio.attribution"
    feedback = client.post(
        "/api/ai/v1/feedback",
        headers={"X-User-Id": HOLDER},
        json={"request_id": request_id, "rating": "up", "reasons": []},
    )
    assert feedback.status_code == 200


def test_응답에서도_항등식이_성립한다(client: TestClient) -> None:
    """§6.3 — 화면에 나가는 숫자끼리 맞아야 사용자가 검산할 수 있다."""
    content = _post(client, HOLDER, "1w").json()["content"]
    parts = content["breakdown"]
    assert parts["market"] + parts["sector"] + parts["selection"] == pytest.approx(
        content["portfolio_return"], abs=1e-9
    )
    assert content["excess_return"] == pytest.approx(
        content["portfolio_return"] - content["benchmark_return"], abs=1e-12
    )
    assert parts["market"] == pytest.approx(content["benchmark_return"], abs=1e-12)
    # 섹터마다 수익률이 다른 시장이므로 배분 효과가 0으로 뭉개지면 안 된다.
    assert parts["sector"] != 0.0
    assert any(s["allocation"] != 0.0 for s in content["sectors"])


@pytest.mark.parametrize("period", ["1d", "1w", "1m", "3m"])
def test_짧은_구간도_전부_계산된다(client: TestClient, period: str) -> None:
    """시드 원장은 10거래일뿐이라 1m·3m은 가진 만큼으로 잘린다 — 409가 아니다."""
    content = _post(client, HOLDER, period).json()["content"]
    assert content["trading_days"] >= 1
    assert content["contributors"] or content["detractors"]


def test_기여도_순으로_정렬된다(client: TestClient) -> None:
    """§4.1 — 수익률이 아니라 기여도 순이다."""
    content = _post(client, HOLDER, "1w").json()["content"]
    values = [row["contribution"] for row in content["contributors"]]
    assert values == sorted(values, reverse=True)
    assert all(row["contribution"] >= 0 for row in content["contributors"])
    assert all(row["contribution"] < 0 for row in content["detractors"])


def test_이벤트가_없으면_빈_배열이다(client: TestClient) -> None:
    content = _post(client, HOLDER, "1w").json()["content"]
    for row in content["contributors"] + content["detractors"]:
        assert row["events"] == []


def test_공시가_있으면_신뢰도와_함께_붙는다(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [("ev-1", "disclosure", "005930", "잠정 실적 공시", DAYS[9], None, 0.8)]
    with _make_client(monkeypatch, StubSession(events=events)) as client:
        content = _post(client, HOLDER, "1w").json()["content"]
    rows = content["contributors"] + content["detractors"]
    attached = [e for row in rows for e in row["events"]]
    assert attached, "구간에 걸친 공시가 하나도 붙지 않았다"
    assert all(0.0 < e["matched_confidence"] <= 1.0 for e in attached)
    assert all(e["type"] == "disclosure" for e in attached)


def test_모델이_숫자를_직접_쓰지_않는다(client: TestClient) -> None:
    """수치는 전부 자리표시자로 나가고 서버가 치환한다."""
    content = _post(client, HOLDER).json()["content"]
    assert "{{" not in content["summary"]["text"]
    metrics = [s for s in content["summary"]["segments"] if s["type"] == "metric"]
    assert metrics
    assert all(s["source"] == "attribution_engine" for s in metrics)


def test_벤치마크를_만들_수_없으면_409(monkeypatch: pytest.MonkeyPatch) -> None:
    with _make_client(monkeypatch, StubSession(instruments=[])) as client:
        assert _post(client, HOLDER).status_code == 409


def test_원장이_없으면_409(client: TestClient) -> None:
    assert _post(client, STRANGER).status_code == 409


def test_LLM_키가_없으면_409(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.api.routes.portfolio.get_llm_client", lambda: NullLlmClient())
    app = create_app()
    app.dependency_overrides[get_session] = lambda: StubSession()
    with TestClient(app) as client:
        assert _post(client, HOLDER).status_code == 409


def test_문장_생성이_실패해도_숫자는_나온다(monkeypatch: pytest.MonkeyPatch) -> None:
    """§6 — 요약이 막혀도 분해 결과는 화면에 남아야 한다."""

    class Blocked:
        async def generate(self, **_: Any) -> LlmResult:
            return LlmResult(
                payload={
                    "narrative": "숫자 12.3% 를 직접 씁니다.",
                    "used_placeholders": [],
                    "used_citations": [],
                }
            )

    monkeypatch.setattr("app.api.routes.portfolio.get_llm_client", lambda: Blocked())
    app = create_app()
    app.dependency_overrides[get_session] = lambda: StubSession()
    with TestClient(app) as client:
        content = _post(client, HOLDER).json()["content"]
    assert content["summary"] is None
    assert content["breakdown"]["market"] is not None
    assert content["contributors"] or content["detractors"]


def test_기여_종목의_return_은_저장된_이름으로도_되읽힌다():
    """응답 로그가 `return_` 로 저장한 JSON 을 캐시 조회가 검증할 수 있어야 한다."""
    from app.api.routes.portfolio import AttributionContributor

    base = {
        "ticker": "005930",
        "name": "삼성전자",
        "sector": "반도체",
        "weight": 0.5,
        "contribution": 0.005,
        "held_at_start": True,
        "events": [],
    }
    assert AttributionContributor.model_validate({**base, "return_": 0.01}).return_ == 0.01
    assert AttributionContributor.model_validate({**base, "return": 0.02}).return_ == 0.02


# ── 요약 없는 응답의 캐시 재사용 (이슈 #101) ─────────────────
#
# 요약 생성이 가드에 반려되면 응답은 200 으로 나가지만 예전에는 캐시 원본으로
# 잡히지 않아, 같은 화면을 다시 열 때마다 실패한 생성을 처음부터 되풀이했다.
# 한 번에 15~29초와 그만큼의 토큰이다.


class _StubRows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _StubDb:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    async def scalars(self, _statement: Any) -> _StubRows:
        return _StubRows(self._rows)


def _row(*, created_at: Any, summary: dict[str, Any] | None, period: str = "1m") -> AIResponse:
    return AIResponse(
        request_id=f"req_{created_at.isoformat()}",
        endpoint="portfolio.attribution",
        cached=False,
        created_at=created_at,
        payload={
            "model": "test-model",
            "data_as_of": {},
            "content": {
                "period": period,
                "start": "2026-08-24",
                "end": "2026-09-23",
                "trading_days": 21,
                "portfolio_return": 0.02,
                "total_return": 0.02,
                "benchmark_return": 0.01,
                "excess_return": 0.01,
                "breakdown": {"market": 0.01, "sector": 0.0, "selection": 0.01},
                "contributors": [],
                "detractors": [],
                "sectors": [],
                "notes": [],
                "summary": summary,
                "text": None,
                "segments": None,
            },
        },
    )


async def _lookup(rows: list[AIResponse]) -> Any:
    from app.api.routes.portfolio import _cached_attribution

    return await _cached_attribution(_StubDb(rows), "u1", "1m", date(2026, 9, 23))


@pytest.mark.parametrize(
    ("use_cache", "expected"),
    [
        pytest.param(True, 1, id="기본값은 캐시를 본다"),
        pytest.param(False, 0, id="끄면 보지 않는다"),
    ],
)
def test_use_cache_가_캐시_조회를_가른다(
    client: TestClient, monkeypatch, use_cache, expected
) -> None:
    """실패를 재현하려는 호출이 실패 캐시에 걸리지 않게 한다 (이슈 #106).

    반환값이 아니라 **조회가 불렸는지**를 센다. 캐시가 비어 있으면 어느 쪽이든
    같은 결과가 나와 검사가 의미를 잃기 때문이다.
    """
    from app.api.routes import portfolio as module

    calls: list[int] = []

    async def spy(*_args: Any, **_kwargs: Any) -> None:
        calls.append(1)
        return None

    monkeypatch.setattr(module, "_cached_attribution", spy)
    asyncio.run(module.build_attribution(HOLDER, client.db, module.Period.D1, use_cache=use_cache))
    assert len(calls) == expected


@pytest.mark.anyio
async def test_요약이_있는_행을_먼저_고른다():
    fresh_empty = _row(created_at=now_kst(), summary=None)
    older_good = _row(created_at=now_kst() - timedelta(minutes=1), summary={"text": "요약"})
    hit = await _lookup([fresh_empty, older_good])
    assert hit is not None
    assert hit.content.summary is not None
    assert hit.content.summary.text == "요약"


@pytest.mark.anyio
async def test_요약이_없어도_짧은_창_안이면_캐시로_쓴다():
    """같은 실패를 15초씩 다시 만들지 않는 것이 이 분기의 목적이다."""
    hit = await _lookup([_row(created_at=now_kst() - timedelta(minutes=1), summary=None)])
    assert hit is not None
    assert hit.cached is True
    assert hit.content.summary is None


@pytest.mark.anyio
async def test_창이_지나면_다시_만들게_한다():
    """24시간 굳히면 요약이 빈 화면이 하루 종일 고정된다. 다시 만들 기회를 남긴다."""
    assert await _lookup([_row(created_at=now_kst() - timedelta(hours=2), summary=None)]) is None
