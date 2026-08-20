"""포트폴리오 진단 엔드포인트 테스트.

숫자는 엔진이, 문장은 LLM이 만든다는 경계가 요점이다. 그래서 통과 경로보다 실패
경로 — 보유 0종목, 짧은 히스토리, LLM 없음 — 를 더 촘촘히 고정한다. 시드 픽스처는
10거래일뿐이라 `min_history_days`(60)에 못 미치고, 따라서 *기본* 경로가 이미
"변동성은 못 구하지만 진단은 나온다"는 경로다.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.core.adapters import Instrument, Ledger
from app.core.db import get_session
from app.llm.client import LlmResult, NullLlmClient

URL = "/api/ai/v1/portfolio/diagnosis"
#: 8종목·현금 4.2%. 집중도와 유동성 항목이 함께 잡힌다.
HOLDER = "mixed_with_cash"
#: 1종목. 상관계수가 정의되지 않는 N=1 경로다.
LONELY = "golden_1_single"
STRANGER = "no_such_user"

_PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")
_SUMMARY_MARK = "포트폴리오 진단 요약"


class FakeClient:
    """사용자 턴에 실린 자리표시자만 골라 쓰는, 검사를 통과하는 응답.

    항목별로 허용 목록이 다르므로 자리표시자를 하드코딩하면 못 쓰는 key를 써서
    차단된다. 턴에서 첫 key를 뽑아 쓰는 편이 실제 모델이 하는 일에 더 가깝다.
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
        # summary는 2~3문장, finding은 정확히 3문장이다(응답 정책 §2).
        body = (
            "같은 국면에 함께 노출되어 대응 여지가 좁습니다. "
            if _SUMMARY_MARK in turn
            else "같은 국면에 함께 노출되어 분산 효과가 제한적입니다. 다음 진단에서 같은 지표를 함께 보십시오. "
        )
        return LlmResult(
            payload={
                "narrative": fact + body.strip(),
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
    """`index_daily`·`instruments` 조회와 응답 로그에 답하는 최소 세션.

    쿼리 문자열로 갈라 낸다 — 호출 순서에 기대면 인자 평가 순서가 바뀌는 순간
    조용히 엉뚱한 값을 넘긴다.
    """

    def __init__(
        self,
        *,
        benchmark: list[tuple] | None = None,
        ranks: list[tuple] | None = None,
        last_payload: dict | None = None,
    ):
        self.benchmark = benchmark if benchmark is not None else []
        self.ranks = ranks if ranks is not None else []
        # 직전 진단의 봉투. None이면 첫 진단이라 히스테리시스 기준이 없다.
        self.last_payload = last_payload
        self.seen: list[str] = []
        self.added: list[Any] = []
        self.commits = 0

    async def execute(self, statement: Any) -> StubResult:
        sql = str(statement)
        self.seen.append(sql)
        if "index_daily" in sql:
            return StubResult(self.benchmark)
        return StubResult(self.ranks)

    async def scalar(self, statement: Any) -> Any:
        sql = str(statement)
        self.seen.append(sql)
        if "ai_responses" in sql:
            return self.last_payload
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


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


def _get(client: TestClient, user: str) -> Any:
    return client.post(URL, headers={"Authorization": f"Bearer {user}"})


# ── 통과 경로 ─────────────────────────────────────────────────────────────────
def test_진단이_엔진_지표와_생성된_문장을_함께_돌려준다(client: TestClient) -> None:
    response = _get(client, HOLDER)
    assert response.status_code == 200
    content = response.json()["content"]

    indicators = content["indicators"]
    assert 0.0 < indicators["hhi"] <= 1.0
    assert indicators["top1_weight"] <= indicators["top3_weight"]
    assert indicators["rate_sensitivity"] in {"low", "moderate", "high"}
    assert indicators["cash_ratio"] == pytest.approx(0.042, abs=0.01)

    assert content["summary"]["text"]
    assert content["findings"], "8종목·현금 4%면 항목이 하나는 잡혀야 한다"


def test_findings_순서가_심각도_순서다(client: TestClient) -> None:
    """§3.7 정렬은 엔진이 한다. 라우터가 순서를 흔들지 않는지만 본다."""
    findings = _get(client, HOLDER).json()["content"]["findings"]
    rank = {"high": 2, "medium": 1, "info": 0}
    severities = [rank[f["severity"]] for f in findings]
    assert severities == sorted(severities, reverse=True)


def test_각_항목이_계약대로_생긴다(client: TestClient) -> None:
    categories = {
        "concentration", "volatility", "correlation",
        "style_tilt", "macro_exposure", "liquidity",
    }
    for finding in _get(client, HOLDER).json()["content"]["findings"]:
        assert finding["category"] in categories
        assert finding["severity"] in {"info", "medium", "high"}
        assert finding["title"] and finding["title"] != finding["id"], "제목 상수가 빠졌다"
        assert finding["text"], "문장 생성이 실패했다"
        assert finding["segments"]
        # evidence는 디버깅·평가용 원시 지표다(§5 표).
        assert finding["evidence"]["metric"]
        assert finding["evidence"]["tickers"]
        assert isinstance(finding["evidence"]["threshold"], float)


def test_모델이_숫자를_직접_쓰지_않는다(client: TestClient) -> None:
    """치환은 서버가 한다. metric 조각에만 숫자가 있고 text 조각에는 없다."""
    content = _get(client, HOLDER).json()["content"]
    sections = [content["summary"], *(f for f in content["findings"])]
    for section in sections:
        for segment in section["segments"]:
            if segment["type"] == "text":
                assert not re.search(r"\d", segment["value"]), segment
            else:
                assert segment["source"] == "risk_engine"
                assert segment["raw"] is not None


def test_엔진_값이_그대로_문장에_들어간다(client: TestClient) -> None:
    """자리표시자가 치환되어 text와 segments가 어긋나지 않는다."""
    content = _get(client, HOLDER).json()["content"]
    for section in [content["summary"], *content["findings"]]:
        assert section["text"] == "".join(s["value"] for s in section["segments"])
        assert "{{" not in section["text"]


# ── 짧은 히스토리 ─────────────────────────────────────────────────────────────
def test_히스토리가_짧으면_점수는_null이지만_409는_아니다(client: TestClient) -> None:
    """시드는 10거래일 — `min_history_days` 미만이다. 절반은 답해야 한다."""
    content = _get(client, HOLDER).json()["content"]
    assert content["risk_score"] is None
    assert content["risk_level"] is None
    assert content["indicators"]["annualized_volatility"] is None
    assert content["indicators"]["diversification_ratio"] is None
    assert content["insufficient_history"], "왜 null인지 알려주지 않으면 클라이언트가 못 읽는다"
    # 그래도 집중도·현금·금리민감도는 유효하다.
    assert content["indicators"]["hhi"] > 0.0
    assert content["findings"]


def test_한_종목이어도_터지지_않는다(client: TestClient) -> None:
    """N=1이면 상관계수가 정의되지 않는다. 엔진이 correlation 항목을 아예 안 낸다."""
    content = _get(client, LONELY).json()["content"]
    assert content["indicators"]["top1_weight"] == pytest.approx(1.0)
    assert content["indicators"]["diversification_ratio"] is None
    assert not [f for f in content["findings"] if f["id"] == "correlation"]
    assert content["summary"]["text"]


# ── 실패 경로 ─────────────────────────────────────────────────────────────────
def test_원장이_없으면_409(client: TestClient) -> None:
    response = _get(client, STRANGER)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INSUFFICIENT_DATA"


def test_보유_0종목이면_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """거래가 없는 원장. 상관은 몰라도 진단할 대상 자체가 없다."""
    days = (date(2026, 8, 17), date(2026, 8, 18))
    empty = Ledger(
        user_id="drained",
        trading_days=days,
        instruments={"005930": Instrument("005930", "삼성전자", "반도체")},
        prices={"005930": dict.fromkeys(days, 70000.0)},
    )
    monkeypatch.setattr("app.api.routes.portfolio._ledger", lambda _user: empty)
    with _make_client(monkeypatch, StubSession()) as client:
        response = _get(client, "drained")
    assert response.status_code == 409
    assert "보유 종목" in response.json()["error"]["message"]


def test_LLM_키가_없으면_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """항목마다 null로 흩뿌리지 않고 한 번에 알린다."""
    monkeypatch.setattr(
        "app.api.routes.portfolio.get_llm_client", lambda: NullLlmClient()
    )
    app = create_app()
    app.dependency_overrides[get_session] = lambda: StubSession()
    with TestClient(app) as client:
        response = _get(client, HOLDER)
    assert response.status_code == 409
    assert "LLM 키" in response.json()["error"]["message"]


def test_문장_생성이_실패해도_지표는_나온다(monkeypatch: pytest.MonkeyPatch) -> None:
    """검사에 걸린 항목은 text가 null이고, 지표·근거는 그대로 나간다."""

    class Blocked:
        async def generate(self, **_: Any) -> LlmResult:
            # 수치를 직접 쓴 응답 — 검사에서 차단된다.
            return LlmResult(
                payload={
                    "narrative": "반도체 비중이 42.3%입니다.",
                    "used_placeholders": [],
                    "used_citations": [],
                }
            )

    monkeypatch.setattr("app.api.routes.portfolio.get_llm_client", lambda: Blocked())
    app = create_app()
    app.dependency_overrides[get_session] = lambda: StubSession()
    with TestClient(app) as client:
        response = _get(client, HOLDER)
    assert response.status_code == 200
    content = response.json()["content"]
    assert content["summary"] is None
    assert content["findings"], "문장이 없어도 항목 목록은 남는다"
    assert all(f["text"] is None for f in content["findings"])
    assert all(f["evidence"]["metric"] for f in content["findings"])
    assert content["indicators"]["hhi"] > 0.0


# ── 엔진 입력 조립 ────────────────────────────────────────────────────────────
def test_벤치마크와_시가총액_순위를_DB에서_읽어_넘긴다(monkeypatch: pytest.MonkeyPatch) -> None:
    """두 쿼리가 실제로 나가고 매핑이 맞는지. 순위는 시장 전체 기준이어야 한다."""
    session = StubSession(
        benchmark=[(date(2026, 8, 17), 2500.0), (date(2026, 8, 18), 2510.0)],
        ranks=[("005930", 1), ("000660", 2)],
    )
    with _make_client(monkeypatch, session) as client:
        assert _get(client, HOLDER).status_code == 200

    joined = " ".join(session.seen)
    assert "index_daily" in joined
    assert "row_number()" in joined.lower(), "보유분만 정렬하면 8종목 중 1위가 대형주가 된다"
    assert "instruments" in joined


def test_지표가_적재_전이면_해당_지표만_빠진다(client: TestClient) -> None:
    """StubSession 기본값은 빈 결과다. 베타와 대형주 비중만 null이고 진단은 나온다."""
    indicators = _get(client, HOLDER).json()["content"]["indicators"]
    assert indicators["beta"] is None
    assert indicators["large_cap_weight"] is None
    assert indicators["hhi"] > 0.0
