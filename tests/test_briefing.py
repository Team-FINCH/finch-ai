"""데일리 브리핑(§8)과 그 랭킹(§5.2).

순위는 코드가, 문장은 LLM이 만든다는 경계가 요점이다. 그래서 랭킹은 엔진 함수를
직접 불러 고정하고, 라우터 쪽은 "그 순서가 그대로 나가는가"만 본다.

`events` 표는 지금 비어 있다(W15가 채우는 중이다). 여기 있는 어떤 테스트도 그 표에
행이 있어야 통과하지 않는다 — 이벤트가 걸린 경우는 EventRecord를 손으로 만들어
엔진 함수에 직접 넣는다. 남의 적재기가 돌아야 통과하는 테스트는 CI에서 깨진다.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.core.db import get_session
from app.core.enums import BriefingCategory, EventType, RateSensitivity
from app.engines import briefing as briefing_engine
from app.engines.attribution import EventRecord
from app.engines.briefing import (
    MAX_ITEMS,
    Candidate,
    event_candidates,
    holding_moves,
    rank,
    sector_shifts,
)
from app.engines.portfolio import Contribution, DailyReturn, Holding, PortfolioSnapshot
from app.llm.client import LlmResult, NullLlmClient

URL = "/api/ai/v1/briefing"
#: 8종목·현금 4.2%. 10거래일 중 마지막 날 등락이 ±0.1~1.6%로 흩어져 있다.
HOLDER = "mixed_with_cash"
#: 1종목. 업종이 하나뿐이라 구조 변화가 정의되지 않는 경로다.
LONELY = "golden_1_single"
STRANGER = "no_such_user"

_PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")
DAY = date(2025, 9, 12)


class FakeClient:
    """사용자 턴에 실린 자리표시자만 골라 쓰는, 검사를 통과하는 응답.

    브리핑은 정확히 2문장이다(SENTENCE_LIMITS). 자리표시자는 항목마다 허용 목록이
    달라 하드코딩하면 못 쓰는 key를 써서 차단된다.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate(self, **kwargs: Any) -> LlmResult:
        self.calls.append(kwargs)
        turn: str = kwargs["user"]
        keys = _PLACEHOLDER_RE.findall(turn.split("[사용 가능한 수치 자리표시자]")[-1])
        first = keys[0] if keys else None
        fact = (
            f"확인된 지표는 {{{{{first}}}}}입니다. "
            if first
            else "수치로 확인된 지표가 없습니다. "
        )
        return LlmResult(
            payload={
                "narrative": fact + "포트폴리오에서 차지하는 자리를 함께 보십시오.",
                "used_placeholders": [first] if first else [],
                "used_citations": [],
            }
        )


class StubResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return self._rows

    def scalars(self) -> list[Any]:
        return [row[0] for row in self._rows]


class StubSession:
    """`events` 조회와 응답 로그에 답하는 최소 세션.

    쿼리 문자열로 갈라 낸다 — 호출 순서에 기대면 인자 평가 순서가 바뀌는 순간
    조용히 엉뚱한 값을 넘긴다.
    """

    def __init__(
        self,
        *,
        events: list[tuple] | None = None,
        payloads: list[dict] | None = None,
    ) -> None:
        self.events = events if events is not None else []
        #: 최근 7일 브리핑 봉투. 비어 있으면 전부 신규다.
        self.payloads = payloads if payloads is not None else []
        self.seen: list[str] = []
        self.added: list[Any] = []
        self.commits = 0

    async def execute(self, statement: Any) -> StubResult:
        sql = str(statement)
        self.seen.append(sql)
        if "ai_responses" in sql:
            return StubResult([(payload,) for payload in self.payloads])
        return StubResult(self.events)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


def _make_client(monkeypatch: pytest.MonkeyPatch, session: Any) -> TestClient:
    fake = FakeClient()
    monkeypatch.setattr("app.api.routes.briefing.get_llm_client", lambda: fake)
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


def _get(client: TestClient, user: str, **params: Any) -> Any:
    return client.get(URL, headers={"Authorization": f"Bearer {user}"}, params=params)


def _holding(
    symbol: str, *, sector: str = "반도체", weight: float, stock_weight: float | None = None
) -> Holding:
    share = stock_weight if stock_weight is not None else weight
    return Holding(
        symbol=symbol,
        name=f"종목{symbol}",
        sector=sector,
        quantity=1.0,
        price=1000.0,
        avg_cost=1000.0,
        market_value=1000.0,
        weight=weight,
        stock_weight=share,
        unrealized_pnl=0.0,
        return_rate=0.0,
    )


def _snapshot(*holdings: Holding, day: date = DAY) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        trade_date=day,
        holdings=tuple(holdings),
        cash=0.0,
        total_value=1000.0,
        cash_weight=0.0,
    )


def _row(*pairs: tuple[str, float], day: date = DAY) -> DailyReturn:
    return DailyReturn(
        trade_date=day,
        portfolio_return=0.0,
        contributions=tuple(
            Contribution(symbol=symbol, weight=0.1, return_rate=move, contribution=0.0)
            for symbol, move in pairs
        ),
    )


# ── 랭킹(§5.2) ────────────────────────────────────────────────────────────────
def test_비중은_제곱근으로_들어가_최대_보유_종목이_전부_가져가지_못한다() -> None:
    """선형 가중이면 20% 종목이 네 칸을 다 먹는다. 그것을 막으려고 √를 쓴다."""
    snapshot = _snapshot(
        _holding("BIG", weight=0.36, stock_weight=0.36),
        _holding("SMALL", weight=0.09, stock_weight=0.09),
    )
    # 비중은 4배 차이지만 √를 거치면 2배로 줄고, 등락 격차가 그것을 뒤집는다.
    # 두 값 모두 MOVE_THRESHOLD를 넘겨야 애초에 후보가 된다.
    top = rank(holding_moves(snapshot, _row(("BIG", 0.016), ("SMALL", 0.05))))

    assert [item.candidate.related_tickers[0] for item in top] == ["SMALL", "BIG"]


def test_등락이_임계_미만이면_항목이_되지_않는다() -> None:
    snapshot = _snapshot(_holding("A", weight=0.5))
    assert holding_moves(snapshot, _row(("A", 0.001))) == []


def test_보유_종목_등락은_이벤트가_없어도_나온다() -> None:
    snapshot = _snapshot(_holding("A", weight=0.5), _holding("B", weight=0.5))
    assert len(holding_moves(snapshot, _row(("A", 0.03), ("B", -0.02)))) == 2


def test_실적_이벤트는_같은_비중의_등락보다_앞선다() -> None:
    """importance 1.0 대 등락의 크기 비례 값. 실적이 이겨야 브리핑이다."""
    snapshot = _snapshot(_holding("A", weight=0.2, stock_weight=0.2))
    events = [
        EventRecord(
            event_id="e1",
            event_type=EventType.EARNINGS.value,
            title="A 3분기 실적 발표",
            event_date=DAY,
            ticker="A",
        )
    ]
    candidates = [
        *holding_moves(snapshot, _row(("A", 0.02))),
        *event_candidates(events, snapshot, DAY),
    ]
    top = rank(candidates)

    assert top[0].candidate.category is BriefingCategory.EARNINGS


def test_최신성은_이틀_반감기로_눌린다() -> None:
    snapshot = _snapshot(_holding("A", weight=0.2, stock_weight=0.2))
    same_day = event_candidates(
        [EventRecord("e1", EventType.FILING.value, "공시", DAY, ticker="A")],
        snapshot,
        DAY,
    )
    four_days = event_candidates(
        [EventRecord("e2", EventType.FILING.value, "공시", DAY - timedelta(days=4), ticker="A")],
        snapshot,
        DAY,
    )
    top = rank([*same_day, *four_days])

    assert top[0].relevance > top[1].relevance * 3


def test_최근_7일에_나갔던_항목은_novelty가_0_3배로_눌린다() -> None:
    snapshot = _snapshot(
        _holding("A", weight=0.2, stock_weight=0.2),
        _holding("B", weight=0.2, stock_weight=0.2),
    )
    candidates = holding_moves(snapshot, _row(("A", 0.03), ("B", 0.03)))
    seen = next(c.key for c in candidates if c.related_tickers == ("A",))

    fresh = rank(candidates)
    damped = rank(candidates, seen_keys=[seen])

    assert [i.candidate.related_tickers[0] for i in damped] == ["B", "A"]
    assert damped[1].relevance == pytest.approx(fresh[0].relevance * 0.3)


def test_미래_이벤트는_버린다() -> None:
    snapshot = _snapshot(_holding("A", weight=0.2))
    events = [
        EventRecord("e1", EventType.FILING.value, "예정 공시", DAY + timedelta(days=1), ticker="A")
    ]
    assert event_candidates(events, snapshot, DAY) == []


def test_보유하지_않은_종목의_공시는_이_사용자의_브리핑이_아니다() -> None:
    snapshot = _snapshot(_holding("A", weight=0.2))
    events = [EventRecord("e1", EventType.FILING.value, "남의 공시", DAY, ticker="ZZZ")]
    assert event_candidates(events, snapshot, DAY) == []


def test_거시_이벤트는_금리민감도_계수를_곱한다() -> None:
    snapshot = _snapshot(_holding("A", weight=0.2))
    macro = [EventRecord("e1", EventType.MACRO.value, "기준금리 인상", DAY)]

    low = event_candidates(macro, snapshot, DAY, rate_sensitivity=RateSensitivity.LOW)
    high = event_candidates(macro, snapshot, DAY, rate_sensitivity=RateSensitivity.HIGH)

    assert high[0].importance == pytest.approx(low[0].importance * 3)


def test_섹터_비중이_5퍼센트포인트_이상_움직여야_구조_변화다() -> None:
    before = _snapshot(
        _holding("A", sector="반도체", weight=0.5, stock_weight=0.5),
        _holding("B", sector="은행", weight=0.5, stock_weight=0.5),
    )
    small = _snapshot(
        _holding("A", sector="반도체", weight=0.53, stock_weight=0.53),
        _holding("B", sector="은행", weight=0.47, stock_weight=0.47),
    )
    big = _snapshot(
        _holding("A", sector="반도체", weight=0.6, stock_weight=0.6),
        _holding("B", sector="은행", weight=0.4, stock_weight=0.4),
    )

    assert sector_shifts(before, small) == []
    shifts = sector_shifts(before, big)
    assert {c.title for c in shifts} == {"반도체 비중 확대", "은행 비중 축소"}
    assert all(c.category is BriefingCategory.PORTFOLIO_SHIFT for c in shifts)


def test_같은_입력이면_같은_순서다() -> None:
    """동점은 분류 순서 → key로 끊는다. 입력 순서를 뒤집어도 결과가 같아야 한다."""
    snapshot = _snapshot(
        _holding("A", weight=0.2, stock_weight=0.2),
        _holding("B", weight=0.2, stock_weight=0.2),
        _holding("C", weight=0.2, stock_weight=0.2),
    )
    candidates = holding_moves(snapshot, _row(("A", 0.03), ("B", 0.03), ("C", 0.03)))

    forward = [i.candidate.key for i in rank(candidates)]
    backward = [i.candidate.key for i in rank(list(reversed(candidates)))]

    assert forward == backward
    assert forward == sorted(forward)


def test_동점이면_분류_순서가_key보다_먼저다() -> None:
    shared = {"weight": 0.25, "importance": 1.0, "days_ago": 0}
    late = Candidate(category=BriefingCategory.PORTFOLIO_SHIFT, title="aaa", **shared)
    early = Candidate(category=BriefingCategory.HOLDING_MOVE, title="zzz", **shared)

    assert [i.candidate.title for i in rank([late, early])] == ["zzz", "aaa"]


def test_최대_4건이다() -> None:
    snapshot = _snapshot(*(_holding(f"S{i}", weight=0.1, stock_weight=0.1) for i in range(9)))
    row = _row(*((f"S{i}", 0.03) for i in range(9)))

    top = rank(holding_moves(snapshot, row))

    assert len(top) == MAX_ITEMS
    assert [item.rank for item in top] == [1, 2, 3, 4]


# ── 라우터(§8) ────────────────────────────────────────────────────────────────
def test_원장을_못_읽으면_409가_아니라_status_empty다(client: TestClient) -> None:
    """진단과 다르다. 프런트가 empty로 섹션을 숨기기로 되어 있다."""
    response = _get(client, STRANGER)

    assert response.status_code == 200
    content = response.json()["content"]
    assert content["status"] == "empty"
    assert content["items"] == []


def test_이벤트_표가_비어도_보유_등락만으로_ready가_나온다(client: TestClient) -> None:
    response = _get(client, HOLDER)

    assert response.status_code == 200
    content = response.json()["content"]
    assert content["status"] == "ready"
    assert content["items"]
    assert {item["category"] for item in content["items"]} == {"holding_move"}


def test_항목은_최대_4건이고_rank가_1부터_이어진다(client: TestClient) -> None:
    items = _get(client, HOLDER).json()["content"]["items"]

    assert 1 <= len(items) <= MAX_ITEMS
    assert [item["rank"] for item in items] == list(range(1, len(items) + 1))


def test_항목은_정확히_2문장이다(client: TestClient) -> None:
    from app.llm.guard.output import split_sentences

    for item in _get(client, HOLDER).json()["content"]["items"]:
        assert len(split_sentences(item["text"])) == 2


def test_relevance_는_내림차순이다(client: TestClient) -> None:
    scores = [item["relevance_score"] for item in _get(client, HOLDER).json()["content"]["items"]]
    assert scores == sorted(scores, reverse=True)


def test_항목은_화면이_필요한_필드를_모두_들고_나온다(client: TestClient) -> None:
    item = _get(client, HOLDER).json()["content"]["items"][0]

    assert item["related_tickers"]
    assert item["deeplink"] == f"/stocks/{item['related_tickers'][0]}?tab=ai"
    assert item["segments"]
    assert "".join(s["value"] for s in item["segments"]) == item["text"]
    assert item["citations"] == []


def test_같은_요청이_같은_순서를_돌려준다(monkeypatch: pytest.MonkeyPatch) -> None:
    def order() -> list[str]:
        with _make_client(monkeypatch, StubSession()) as client:
            content = _get(client, HOLDER).json()["content"]
            return [item["title"] for item in content["items"]]

    assert order() == order()


def test_어제_나간_항목은_novelty에_눌려_1위를_내준다(monkeypatch: pytest.MonkeyPatch) -> None:
    """0.3배는 큰 감쇠라 자리를 내주는 정도가 아니라 4위 밖으로 밀려나기도 한다.

    같은 종목이 이틀 연속 1위를 차지해 브리핑이 어제와 똑같아지는 것을 막는 장치다.
    """
    # 시드 원장의 마지막 거래일 등락은 하이닉스(+1.57%) 하나만 기본 임계를 넘는다.
    # 이 테스트가 보는 것은 눈금이 아니라 순위·카테고리 배선이므로 눈금을 낮춰 고정한다.
    monkeypatch.setattr(briefing_engine, "MOVE_THRESHOLD", 0.002)
    with _make_client(monkeypatch, StubSession()) as client:
        before = _get(client, HOLDER).json()["content"]["items"]
    first = before[0]

    history = {"content": {"items": [first]}}
    with _make_client(monkeypatch, StubSession(payloads=[history])) as client:
        after = _get(client, HOLDER).json()["content"]["items"]

    assert after[0]["title"] != first["title"]
    # 눌린 항목이 비운 자리는 어제 2위가 이어받는다.
    assert after[0]["title"] == before[1]["title"]


def test_휴장일을_요청하면_직전_거래일로_내린다(client: TestClient) -> None:
    # 2025-09-14는 일요일이다. 시드의 마지막 거래일은 2025-09-12다.
    content = _get(client, HOLDER, date="2025-09-14").json()["content"]

    assert content["date"] == "2025-09-12"
    assert content["status"] == "ready"


def test_원장_시작_이전을_요청하면_status_empty다(client: TestClient) -> None:
    content = _get(client, HOLDER, date="2024-01-01").json()["content"]

    assert content["status"] == "empty"
    assert content["items"] == []


def test_업종이_하나뿐이면_구조_변화_없이_등락만_나온다(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 시드의 단일 보유 종목은 -0.53%라 기본 임계에 못 미친다. 여기서 보려는 것은
    # "업종이 하나면 portfolio_shift가 없다"이지 눈금이 아니므로 낮춰 고정한다.
    monkeypatch.setattr(briefing_engine, "MOVE_THRESHOLD", 0.002)
    content = _get(client, LONELY).json()["content"]

    assert content["status"] == "ready"
    assert all(item["category"] == "holding_move" for item in content["items"])


def test_응답을_로그에_남긴다_다음_날_novelty가_읽을_행이다(client: TestClient) -> None:
    _get(client, HOLDER)

    session: StubSession = client.db  # type: ignore[attr-defined]
    assert len(session.added) == 1
    assert session.added[0].endpoint == "briefing"
    assert session.added[0].payload["content"]["items"]


def test_비어도_로그는_남긴다(client: TestClient) -> None:
    _get(client, STRANGER)

    session: StubSession = client.db  # type: ignore[attr-defined]
    assert len(session.added) == 1
    assert session.added[0].endpoint == "briefing"


def test_LLM_키가_없으면_409다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.api.routes.briefing.get_llm_client", lambda: NullLlmClient()
    )
    app = create_app()
    app.dependency_overrides[get_session] = lambda: StubSession()
    with TestClient(app) as client:
        response = _get(client, HOLDER)

    assert response.status_code == 409
