"""주문 전 점검 엔드포인트 테스트.

이 기능은 엔진이 아니라 *차분*이다. 그래서 값 자체보다 "전·후가 같은 규칙으로 만들어
졌는가", "경고를 엔진이 골랐는가", "없는 충돌을 지어내지 않는가"를 본다.

가장자리 경로가 본체다 — 전량 매도로 보유가 0이 되는 경우, 처음 담는 종목, 보유보다
많은 매도, 종가가 없는 종목, 위키가 비어 있는 경우. 시드 픽스처는 10거래일뿐이라
`min_history_days`(60)에 못 미치고, 따라서 변동성·상관은 전·후 모두 null이 기본이다.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.core.adapters import Instrument, Ledger
from app.core.db import get_session
from app.core.models import AIFeedback, AIResponse
from app.llm.client import LlmResult, NullLlmClient

URL = "/api/ai/v1/orders/preview"
#: 8종목·현금 4.2%. 최대 종목은 005930(19.2%), 반도체 업종 합이 46.1%다.
HOLDER = "mixed_with_cash"
#: 005930 100주 하나뿐. 전량 매도하면 보유가 0이 된다.
LONELY = "golden_1_single"
STRANGER = "no_such_user"

#: 2025-09-12 종가. 픽스처가 이 값을 쥐고 있어 주문 금액이 여기서 나온다.
CLOSE_005930 = 75800
CLOSE_000660 = 226500

#: 005930·000660만 남기는 매도 묶음. 현금을 쓰지 않고 집중도를 임계 위로 올린다.
CONCENTRATE = [
    {"ticker": "005380", "side": "sell", "quantity": 55, "price": 206000},
    {"ticker": "105560", "side": "sell", "quantity": 150, "price": 74800},
    {"ticker": "035420", "side": "sell", "quantity": 60, "price": 184000},
    {"ticker": "068270", "side": "sell", "quantity": 70, "price": 156200},
    {"ticker": "373220", "side": "sell", "quantity": 25, "price": 345000},
    {"ticker": "042700", "side": "sell", "quantity": 100, "price": 83100},
]

_PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")
_NO_CONFLICT = "해당 없음"


class FakeClient:
    """턴에 실린 자리표시자만 골라 쓰는, 검사를 통과하는 응답.

    항목별로 허용 목록이 다르므로 자리표시자를 하드코딩하면 못 쓰는 key를 써서 차단된다.
    `conflict=False`면 논지 항목에서 지정 문구를 그대로 돌려준다.
    """

    def __init__(self, *, conflict: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self.conflict = conflict

    async def generate(self, **kwargs: Any) -> LlmResult:
        self.calls.append(kwargs)
        turn: str = kwargs["user"]
        if "논지" in turn.split("[기준 시각]")[0] and not self.conflict:
            return LlmResult(
                payload={
                    "narrative": f"{_NO_CONFLICT}. 이 주문은 기록된 논지와 어긋나지 않습니다.",
                    "used_placeholders": [],
                    "used_citations": [],
                }
            )
        keys = _PLACEHOLDER_RE.findall(turn.split("[사용 가능한 수치 자리표시자]")[-1])
        first = keys[0] if keys else None
        fact = (
            f"확인된 지표는 {{{{{first}}}}}입니다. "
            if first
            else "수치로 확인된 지표가 없습니다. "
        )
        # before_you_trade는 2~3문장이다(응답 정책 §2).
        return LlmResult(
            payload={
                "narrative": fact + "이 주문은 같은 국면에 대한 노출을 넓힙니다.",
                "used_placeholders": [first] if first else [],
                "used_citations": [],
            }
        )


class StubResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return self._rows


class StubScalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class StubThesis:
    def __init__(self, ticker: str, text: str, source: str = "user_stated") -> None:
        self.id = uuid.uuid4()
        self.ticker = ticker
        self.text = text
        self.source = source
        self.status = "active"
        self.recorded_at = datetime(2026, 4, 2, 20, 11)


class StubFact:
    def __init__(self, text: str, source: str = "user_stated") -> None:
        self.text = text
        self.source = source


class StubSession:
    """쿼리 문자열로 갈라 낸다 — 호출 순서에 기대면 인자 평가 순서가 바뀌는 순간 깨진다."""

    def __init__(
        self,
        *,
        benchmark: list[tuple] | None = None,
        ranks: list[tuple] | None = None,
        instruments: list[tuple] | None = None,
        prices: list[tuple] | None = None,
        theses: list[Any] | None = None,
        facts: list[Any] | None = None,
    ) -> None:
        self.benchmark = benchmark or []
        self.ranks = ranks or []
        self.instruments = instruments or []
        self.prices = prices if prices is not None else []
        self.theses = theses or []
        self.facts = facts or []
        self.seen: list[str] = []
        self.added: list[Any] = []

    async def execute(self, statement: Any) -> StubResult:
        sql = str(statement)
        self.seen.append(sql)
        if "index_daily" in sql:
            return StubResult(self.benchmark)
        if "price_daily" in sql:
            return StubResult(self.prices)
        if "row_number()" in sql.lower():
            return StubResult(self.ranks)
        return StubResult(self.instruments)

    async def scalars(self, statement: Any) -> StubScalars:
        sql = str(statement)
        self.seen.append(sql)
        return StubScalars(self.theses if "wiki_theses" in sql else self.facts)

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


def _make_client(
    monkeypatch: pytest.MonkeyPatch, session: Any, *, llm: Any | None = None
) -> TestClient:
    fake = llm if llm is not None else FakeClient()
    monkeypatch.setattr("app.api.routes.orders.get_llm_client", lambda: fake)
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


def _post(client: TestClient, user: str, orders: list[dict[str, Any]]) -> Any:
    return client.post(
        URL, headers={"Authorization": f"Bearer {user}"}, json={"orders": orders}
    )


def _buy(ticker: str = "000660", quantity: int = 15, **extra: Any) -> dict[str, Any]:
    return {"ticker": ticker, "side": "buy", "quantity": quantity, **extra}


def _sell(ticker: str, quantity: int, **extra: Any) -> dict[str, Any]:
    return {"ticker": ticker, "side": "sell", "quantity": quantity, **extra}


# ── 통과 경로 ─────────────────────────────────────────────────────────────────
def test_전후와_차분을_함께_돌려준다(client: TestClient) -> None:
    content = _post(client, HOLDER, [_buy(price=214000)]).json()["content"]

    assert content["order_summary"] == [
        {"ticker": "000660", "side": "buy", "quantity": 15, "price": 214000, "amount": 3210000}
    ]
    assert content["orders_value"] == 3210000
    assert content["feasible"] is True
    assert content["shortfall"] is None
    assert 0.0 < content["before"]["hhi"] <= 1.0
    assert 0.0 < content["after"]["hhi"] <= 1.0
    assert content["summary"]["text"]


def test_응답을_저장해_피드백을_받는다(client: TestClient) -> None:
    response = _post(client, HOLDER, [_buy(price=214000)])
    request_id = response.json()["request_id"]

    row = next(row for row in client.db.added if isinstance(row, AIResponse))
    assert row.request_id == request_id
    assert row.endpoint == "orders.preview"
    feedback = client.post(
        "/api/ai/v1/feedback",
        headers={"Authorization": f"Bearer {HOLDER}"},
        json={"request_id": request_id, "rating": "up", "reasons": []},
    )
    assert feedback.status_code == 200


def test_delta가_after_빼기_before다(client: TestClient) -> None:
    """모델이 쓴 값이 아니라 서버가 뺀 값이어야 한다."""
    content = _post(client, HOLDER, [_buy(price=214000)]).json()["content"]
    for key, value in content["delta"].items():
        assert value == pytest.approx(content["after"][key] - content["before"][key])
    # 등급 문자열은 뺄 수 없으므로 차분에서 빠진다.
    assert "rate_sensitivity" not in content["delta"]
    assert content["before"]["rate_sensitivity"] in {"low", "moderate", "high"}


def test_매수는_집중도를_올리고_현금을_줄인다(client: TestClient) -> None:
    """이미 반도체가 무거운 포트폴리오에 SK하이닉스를 더 담는 주문이다."""
    content = _post(client, HOLDER, [_buy(price=214000)]).json()["content"]
    assert content["delta"]["hhi"] > 0.0
    assert content["delta"]["top1_weight"] > 0.0
    assert content["delta"]["cash_ratio"] < 0.0
    assert content["delta"]["top_sector_weight"] > 0.0


def test_MDD는_주문으로_바뀌지_않는다(client: TestClient) -> None:
    """낙폭은 과거 총자산 시계열에서 나온다. 아직 내지 않은 주문이 지난 낙폭을 바꿀 수 없다."""
    content = _post(client, HOLDER, [_buy(price=214000)]).json()["content"]
    assert content["delta"]["max_drawdown_1y"] == pytest.approx(0.0)


def test_price를_생략하면_최근_종가로_본다(client: TestClient) -> None:
    """추측이 아니라 조회다. 000660의 2025-09-12 종가는 픽스처에 있다."""
    content = _post(client, HOLDER, [_buy(quantity=10)]).json()["content"]
    row = content["order_summary"][0]
    assert row["price"] == CLOSE_000660
    assert row["amount"] == 10 * CLOSE_000660


def test_모델이_숫자를_직접_쓰지_않는다(client: TestClient) -> None:
    content = _post(client, HOLDER, CONCENTRATE).json()["content"]
    sections = [content["summary"], *content["warnings"]]
    for section in sections:
        assert section["text"] == "".join(s["value"] for s in section["segments"])
        assert "{{" not in section["text"]
        for segment in section["segments"]:
            if segment["type"] == "text":
                assert not re.search(r"\d", segment["value"]), segment
            else:
                assert segment["source"] == "risk_engine"


# ── 경고 ──────────────────────────────────────────────────────────────────────
def test_경고는_엔진_임계가_고른다(client: TestClient) -> None:
    """새로 걸리거나 등급이 오른 finding만 경고다. 모델은 문장만 쓴다."""
    content = _post(client, HOLDER, CONCENTRATE).json()["content"]
    raised = {w["id"]: w for w in content["warnings"]}
    # 005930만 51%가 되므로 종목 집중이 새로 걸리고, 반도체 100%라 업종 집중은
    # medium에서 high로 올라간다.
    assert raised["ticker_concentration"]["severity"] == "high"
    assert raised["sector_concentration"]["severity"] == "high"
    assert content["before"]["top1_weight"] < 0.25 <= content["after"]["top1_weight"]
    for warning in content["warnings"]:
        assert warning["after"] >= warning["threshold"], "임계를 넘었기에 경고다"
        assert warning["title"] and warning["text"]


def test_위험을_낮추는_주문은_경고를_만들지_않는다(client: TestClient) -> None:
    """최대 종목을 전량 정리하면 업종 집중과 현금 부족이 함께 풀린다."""
    content = _post(client, HOLDER, [_sell("005930", 250, price=CLOSE_005930)]).json()["content"]
    assert content["delta"]["top_sector_weight"] < 0.0
    assert content["delta"]["cash_ratio"] > 0.0
    assert content["warnings"] == [], "나아진 항목은 경고가 아니다"


# ── 가장자리 ──────────────────────────────────────────────────────────────────
def test_전량_매도하면_after가_현금뿐이다(client: TestClient) -> None:
    """보유 0종목은 상관·변동성이 정의되지 않는 경로다. 터지지 않고 null로 나와야 한다."""
    content = _post(client, LONELY, [_sell("005930", 100, price=CLOSE_005930)]).json()["content"]
    assert content["after"]["hhi"] == pytest.approx(0.0)
    assert content["after"]["top1_weight"] == pytest.approx(0.0)
    assert content["after"]["cash_ratio"] == pytest.approx(1.0)
    assert content["after"]["diversification_ratio"] is None
    assert content["before"]["top1_weight"] == pytest.approx(1.0)


def test_처음_담는_종목은_DB에서_시세를_가져온다(monkeypatch: pytest.MonkeyPatch) -> None:
    """원장에 없는 종목이면 종목명·업종·시세 전체를 DB에서 읽는다.

    시세를 통째로 가져오는 이유는 §3.2가 종목별 일별 수익률을 요구하기 때문이다 —
    히스토리 없이 붙이면 주문 후만 변동성이 사라져 차분이 거짓말을 한다.
    """
    days = [date(2025, 9, d) for d in (1, 2, 3, 4, 5, 8, 9, 10, 11, 12)]
    session = StubSession(
        instruments=[("051910", "LG화학", "화학")],
        prices=[(day, 300000 + 1000 * index) for index, day in enumerate(days)],
    )
    with _make_client(monkeypatch, session) as client:
        content = _post(client, HOLDER, [_buy("051910", 10)]).json()["content"]

    assert content["order_summary"][0]["price"] == 309000, "기준일 종가를 써야 한다"
    assert "price_daily" in " ".join(session.seen)
    assert content["delta"]["hhi"] != pytest.approx(0.0)


def test_첫_종목을_담는_경우도_터지지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """보유 0에서 시작하는 원장. N=1이라 상관계수가 정의되지 않는다."""
    days = (date(2026, 8, 17), date(2026, 8, 18))
    empty = Ledger(
        user_id="fresh",
        trading_days=days,
        instruments={"005930": Instrument("005930", "삼성전자", "반도체")},
        prices={"005930": dict.fromkeys(days, 70000.0)},
    )
    monkeypatch.setattr("app.api.routes.orders._ledger", lambda _user: empty)
    with _make_client(monkeypatch, StubSession()) as client:
        content = _post(client, "fresh", [_buy("005930", 10, price=70000)]).json()["content"]

    assert content["before"]["hhi"] == pytest.approx(0.0)
    assert content["after"]["top1_weight"] == pytest.approx(1.0)
    assert content["after"]["diversification_ratio"] is None
    # 현금이 0인 원장이므로 매수하면 부족하다 — 사실로 알리고 끊지는 않는다.
    assert content["feasible"] is False
    assert content["shortfall"] == 700000


def test_보유보다_많이_팔면_400(client: TestClient) -> None:
    """조용히 잘라 주면 사용자가 낸 주문과 다른 것을 점검하게 된다."""
    response = _post(client, LONELY, [_sell("005930", 101, price=CLOSE_005930)])
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert "매도" in response.json()["error"]["message"]


def test_보유하지_않은_종목_매도도_400(client: TestClient) -> None:
    """시세가 없어서가 아니라 팔 것이 없어서 틀린 주문이다 — 409가 아니라 400이다."""
    response = _post(client, LONELY, [_sell("000660", 1, price=CLOSE_000660)])
    assert response.status_code == 400


def test_종가가_없는_종목은_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """0으로 치지 않고 크게 실패한다 — 0원짜리 주문은 차분을 통째로 거짓말로 만든다."""
    with _make_client(monkeypatch, StubSession(prices=[])) as client:
        response = _post(client, HOLDER, [_buy("999999", 10)])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INSUFFICIENT_DATA"
    assert "999999" in response.json()["error"]["message"]


def test_리밸런싱은_한_번에_점검한다(client: TestClient) -> None:
    """동시 매수·매도. 배열로 받는 이유가 이것이다(§7)."""
    content = _post(
        client,
        HOLDER,
        [_sell("000660", 20, price=CLOSE_000660), _buy("005930", 100, price=74000)],
    ).json()["content"]
    assert len(content["order_summary"]) == 2
    assert content["orders_value"] == 7400000, "매수분만 센다"


# ── 위키 ──────────────────────────────────────────────────────────────────────
def test_위키가_비면_충돌은_빈_목록이다(client: TestClient) -> None:
    """맞물리는 행이 없으면 지어내지 않는다."""
    content = _post(client, HOLDER, [_buy(price=214000)]).json()["content"]
    assert content["thesis_conflicts"] == []


def test_사용자가_적어_둔_논지와_어긋나면_충돌이다(monkeypatch: pytest.MonkeyPatch) -> None:
    session = StubSession(
        theses=[StubThesis("000660", "HBM 증설 사이클이 끝날 때까지 들고 갈 생각입니다")],
        facts=[StubFact("분산 투자를 중시하며 단일 업종 40% 이상을 피하고 싶습니다")],
    )
    with _make_client(monkeypatch, session) as client:
        content = _post(client, HOLDER, [_sell("000660", 40, price=CLOSE_000660)]).json()[
            "content"
        ]

    assert len(content["thesis_conflicts"]) == 1
    conflict = content["thesis_conflicts"][0]
    assert conflict["ticker"] == "000660"
    assert conflict["source"] == "user_stated"
    assert conflict["fact"].startswith("HBM")
    assert conflict["conflict"]
    # 사용자 성향은 요약의 배경으로 넘어간다.
    assert "분산 투자를 중시" in " ".join(call["user"] for call in client.llm.calls)


def test_어긋나지_않으면_충돌로_올리지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """논지가 있다는 사실만으로 충돌이 되면 매번 이의를 제기하게 된다."""
    session = StubSession(theses=[StubThesis("000660", "HBM 사이클을 보고 담았습니다")])
    with _make_client(monkeypatch, session, llm=FakeClient(conflict=False)) as client:
        content = _post(client, HOLDER, [_buy(price=214000)]).json()["content"]
    assert content["thesis_conflicts"] == []


def test_AI가_추론한_성향으로는_이의를_제기하지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """§7 — `user_stated` 항목만 쓴다. 추론한 성향으로 따지면 근거 없는 참견이 된다."""
    session = StubSession(
        theses=[StubThesis("000660", "반도체를 오래 볼 생각입니다", source="ai_inferred")],
        facts=[StubFact("위험을 싫어하는 편으로 보입니다", source="ai_inferred")],
    )
    with _make_client(monkeypatch, session) as client:
        content = _post(client, HOLDER, [_sell("000660", 40, price=CLOSE_000660)]).json()[
            "content"
        ]
    assert content["thesis_conflicts"] == []
    assert "위험을 싫어하는" not in " ".join(call["user"] for call in client.llm.calls)


def test_다른_종목의_논지는_끌어오지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    session = StubSession(theses=[StubThesis("005380", "현대차는 배당을 보고 담았습니다")])
    with _make_client(monkeypatch, session) as client:
        content = _post(client, HOLDER, [_buy("000660", 15, price=214000)]).json()["content"]
    assert content["thesis_conflicts"] == []


# ── 실패 경로 ─────────────────────────────────────────────────────────────────
def test_원장이_없으면_409(client: TestClient) -> None:
    response = _post(client, STRANGER, [_buy(price=214000)])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INSUFFICIENT_DATA"


def test_LLM_키가_없으면_409(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.api.routes.orders.get_llm_client", lambda: NullLlmClient())
    app = create_app()
    app.dependency_overrides[get_session] = lambda: StubSession()
    with TestClient(app) as client:
        response = _post(client, HOLDER, [_buy(price=214000)])
    assert response.status_code == 409
    assert "ANTHROPIC_API_KEY" in response.json()["error"]["message"]


def test_주문이_비면_400(client: TestClient) -> None:
    """`min_length=1`은 pydantic이 잡고 핸들러가 400으로 옮긴다."""
    assert _post(client, HOLDER, []).status_code == 400


def test_문장_생성이_실패해도_수치는_나온다(monkeypatch: pytest.MonkeyPatch) -> None:
    class Blocked:
        async def generate(self, **_: Any) -> LlmResult:
            # 수치를 직접 쓴 응답 — 검사에서 차단된다.
            return LlmResult(
                payload={
                    "narrative": "단일 종목 비중이 42.3%가 됩니다.",
                    "used_placeholders": [],
                    "used_citations": [],
                }
            )

    with _make_client(monkeypatch, StubSession(), llm=Blocked()) as client:
        content = _post(client, HOLDER, CONCENTRATE).json()["content"]

    assert content["summary"] is None
    assert content["warnings"], "문장이 없어도 경고 목록은 남는다"
    assert all(w["text"] is None for w in content["warnings"])
    assert all(isinstance(w["threshold"], float) for w in content["warnings"])
    assert content["delta"]["hhi"] > 0.0
