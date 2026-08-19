"""도구 계층 테스트.

에이전트가 무엇을 고르든 도구 하나하나는 혼자서도 정직해야 한다. 자료가 없으면
없다고 돌려주고, 원천이 터져도 예외를 밖으로 던지지 않는 것이 요점이다.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from app.core.enums import MetricSource, Screen, WikiSource
from app.llm import tools as tool_mod
from app.llm.tools import TOOL_NAMES, TOOLS, ToolContext, dispatch

#: 시드 픽스처에서 005930을 보유한 사용자.
HOLDER = "golden_1_single"
STRANGER = "no_such_user"


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(user_id=HOLDER)


def run(coro):
    return asyncio.run(coro)


# ── 도구 정의 ────────────────────────────────────────────────────────────────
def test_도구_정의가_Anthropic_규격을_지킨다():
    for tool in TOOLS:
        assert set(tool) >= {"name", "description", "input_schema"}
        assert tool["input_schema"]["type"] == "object"
        assert tool["description"].strip()


def test_1단계_도구_여섯_개만_노출한다():
    """calc_risk_metrics 등은 엔진이 없다. 없는 것을 부르게 두지 않는다."""
    assert TOOL_NAMES == (
        "get_portfolio",
        "get_price_history",
        "search_filings",
        "search_news",
        "get_financials",
        "get_wiki",
    )
    assert len(set(TOOL_NAMES)) == len(TOOL_NAMES)


# ── 포트폴리오 ───────────────────────────────────────────────────────────────
def test_포트폴리오는_자리표시자와_표시값을_함께_돌려준다(ctx):
    result = run(dispatch("get_portfolio", {}, ctx))

    assert result["holdings"][0]["ticker"] == "005930"
    key, display = next(iter(result["metrics"].items()))
    # 도구가 알려준 key가 곧 치환 가능한 자리표시자다.
    assert ctx.values[key].value == display


def test_포트폴리오_수치는_엔진_출처를_달고_나온다(ctx):
    run(dispatch("get_portfolio", {}, ctx))
    assert ctx.values
    assert all(s.source is MetricSource.PORTFOLIO_ENGINE for s in ctx.values.values())
    assert ctx.portfolio_as_of is not None


def test_원장이_없는_사용자는_사유를_돌려준다():
    ctx = ToolContext(user_id=STRANGER)
    result = run(dispatch("get_portfolio", {}, ctx))
    assert "unavailable" in result
    assert ctx.values == {}


# ── 시세 ─────────────────────────────────────────────────────────────────────
def test_시세는_화면_종목으로_대명사를_푼다():
    """종목 인자가 없으면 사용자가 보던 종목이다."""
    ctx = ToolContext(user_id=HOLDER, screen=Screen.STOCK_DETAIL, ticker="005930")
    result = run(dispatch("get_price_history", {}, ctx))
    assert result["ticker"] == "005930"
    assert "change_005930" in ctx.values


def test_시세는_구간_등락률에_부호를_붙인다(ctx):
    run(dispatch("get_price_history", {"ticker": "005930", "days": 5}, ctx))
    change = ctx.values["change_005930"]
    assert change.source is MetricSource.PRICE
    assert change.value.startswith(("+", "-", "0"))


def test_모르는_종목은_지어내지_않는다(ctx):
    result = run(dispatch("get_price_history", {"ticker": "999999"}, ctx))
    assert "unavailable" in result
    assert ctx.values == {}


# ── 검색 ─────────────────────────────────────────────────────────────────────
def _hit(title: str) -> dict[str, Any]:
    return {
        "text": f"{title} 본문",
        "ticker": "005930",
        "title": title,
        "published_at": datetime(2026, 8, 14, 16, 12),
        "similarity": 0.8,
    }


def test_검색_결과에_근거_id가_순서대로_붙는다(ctx, monkeypatch):
    async def _hits(*_: Any, **__: Any):
        return [_hit("분기보고서")]

    monkeypatch.setattr(tool_mod, "search", _hits)

    first = run(dispatch("search_filings", {"query": "HBM"}, ctx))
    second = run(dispatch("search_news", {"query": "HBM"}, ctx))

    assert first["hits"][0]["citation"] == "cit_1"
    assert second["hits"][0]["citation"] == "cit_2"
    # 누적 순서가 곧 citations_from_hits가 매길 번호다.
    assert len(ctx.hits) == 2


def test_임베딩이_없으면_없다고_말한다(ctx, monkeypatch):
    async def _none(*_: Any, **__: Any):
        return []

    monkeypatch.setattr(tool_mod, "search", _none)
    result = run(dispatch("get_financials", {"query": "매출"}, ctx))
    assert result["hits"] == []
    assert result["note"]


def test_검색어가_비면_거부한다(ctx):
    assert "unavailable" in run(dispatch("search_news", {"query": "  "}, ctx))


# ── 위키 ─────────────────────────────────────────────────────────────────────
class _Thesis:
    def __init__(self, ticker: str, source: str) -> None:
        self.ticker = ticker
        self.text = f"{ticker} 구조적 성장에 베팅"
        self.source = source
        self.recorded_at = datetime(2026, 3, 11, 10, 22)


class _Fact:
    text = "장기 보유 성향"
    source = "ai_inferred"


def test_위키가_없으면_사유를_돌려준다(ctx):
    assert "unavailable" in run(dispatch("get_wiki", {}, ctx))


def test_위키는_논지를_생성용_본문으로_모은다(monkeypatch):
    ctx = ToolContext(user_id=HOLDER, db=object())

    async def _theses(*_: Any, **__: Any):
        return [_Thesis("005930", "user_stated")]

    async def _facts(*_: Any, **__: Any):
        return []

    monkeypatch.setattr(tool_mod, "list_theses", _theses)
    monkeypatch.setattr(tool_mod, "list_facts", _facts)

    result = run(dispatch("get_wiki", {}, ctx))
    assert result["theses"][0]["ticker"] == "005930"
    assert "구조적 성장에 베팅" in ctx.wiki
    assert ctx.wiki_source is WikiSource.USER_STATED


def test_추론_항목이_섞이면_어투_검사를_엄한_쪽으로_맞춘다(monkeypatch):
    """§4.2 — ai_inferred는 단정할 수 없다. 하나라도 있으면 그쪽을 따른다."""
    ctx = ToolContext(user_id=HOLDER, db=object())

    async def _theses(*_: Any, **__: Any):
        return [_Thesis("005930", "user_stated")]

    async def _facts(*_: Any, **__: Any):
        return [_Fact()]

    monkeypatch.setattr(tool_mod, "list_theses", _theses)
    monkeypatch.setattr(tool_mod, "list_facts", _facts)

    run(dispatch("get_wiki", {}, ctx))
    assert ctx.wiki_source is WikiSource.AI_INFERRED


def test_종목을_주면_그_종목_논지만_남는다(monkeypatch):
    ctx = ToolContext(user_id=HOLDER, db=object())

    async def _theses(*_: Any, **__: Any):
        return [_Thesis("005930", "user_stated"), _Thesis("000660", "user_stated")]

    async def _facts(*_: Any, **__: Any):
        return []

    monkeypatch.setattr(tool_mod, "list_theses", _theses)
    monkeypatch.setattr(tool_mod, "list_facts", _facts)

    result = run(dispatch("get_wiki", {"ticker": "000660"}, ctx))
    assert [t["ticker"] for t in result["theses"]] == ["000660"]


# ── 실패 경로 ────────────────────────────────────────────────────────────────
def test_없는_도구를_불러도_터지지_않는다(ctx):
    assert "unavailable" in run(dispatch("simulate_order", {}, ctx))


def test_원천이_터져도_예외를_밖으로_던지지_않는다(ctx, monkeypatch):
    async def _boom(*_: Any, **__: Any):
        raise RuntimeError("pgvector 연결 끊김")

    monkeypatch.setattr(tool_mod, "search", _boom)
    assert "unavailable" in run(dispatch("search_filings", {"query": "HBM"}, ctx))


def test_부른_도구를_기록한다(ctx):
    run(dispatch("get_portfolio", {}, ctx))
    run(dispatch("get_portfolio", {}, ctx))
    assert ctx.used == ["get_portfolio", "get_portfolio"]
