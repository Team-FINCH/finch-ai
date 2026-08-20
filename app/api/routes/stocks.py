"""종목 AI 분석. API 명세 §3.

담당 트랙: feat/rag-dart, feat/rag-search, feat/llm-pipeline

섹션은 두 부류로 나뉜다. `current`·`changes`·`attention`·`risks`·`next_events`는
사용자와 무관해 종목 단위로 캐시할 수 있고, `my_impact`·`thesis_check`만
사용자별이다(§3 비용 설계). 캐시 저장소는 아직 없지만 두 부류가 서로 다른
입력만 보도록 갈라 두어, 캐시를 붙일 때 이 파일 밖은 건드리지 않게 했다.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, time
from functools import lru_cache
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.core.adapters import SeedLedgerSource
from app.core.config import settings
from app.core.enums import MetricSource, WikiSource
from app.core.errors import InsufficientData, InvalidRequest
from app.core.response_log import record
from app.core.schemas import DataAsOf, Envelope, Segment
from app.engines.portfolio import Holding, PortfolioEngine, PortfolioSnapshot
from app.llm.client import NullLlmClient, get_llm_client
from app.llm.generate import (
    SectionOutcome,
    citations_from_hits,
    documents_block,
    generate_section,
    ratio_segment,
)
from app.llm.guard import Feature
from app.rag.search import search
from app.wiki.store import get_active_thesis

log = logging.getLogger("app.api.stocks")

router = APIRouter(prefix="/stocks", tags=["stocks"])

SECTIONS = (
    "current", "changes", "attention", "risks",
    "my_impact", "thesis_check", "next_events",
)

#: 명칭은 규제 대응이다. "긍정/부정 요인"은 의견 제시로 읽히므로 출처 귀속형으로 고정한다.
SECTION_TITLES: dict[str, str] = {
    "current": "현재 상황",
    "changes": "최근 변화",
    "attention": "시장이 주목하는 요인",
    "risks": "확인된 위험 요인",
    "my_impact": "내 포트폴리오 영향",
    "thesis_check": "투자 논지 점검",
    "next_events": "다음에 확인할 일정",
}

#: 사용자별 섹션. 나머지는 종목 단위 캐시 대상이다.
PERSONAL_SECTIONS = frozenset({"my_impact", "thesis_check"})

_TICKER_RE = re.compile(r"\d{6}")
_RAG_TOP_K = 6


class AnalysisRequest(BaseModel):
    sections: list[str] | None = Field(default=None)
    personalize: bool = True


# ── 원장 ─────────────────────────────────────────────────
@lru_cache
def _ledger_source() -> SeedLedgerSource:
    return SeedLedgerSource(settings.seed_fixture_path)


def _snapshot(user_id: str) -> PortfolioSnapshot | None:
    """마지막 거래일 기준 스냅샷. 원장을 못 읽으면 None이다.

    백엔드 원장 읽기가 열리기 전까지는 시드 어댑터만 있다(§11). 없는 것을
    억지로 만들지 않고 개인화 섹션만 조용히 비운다 — §3이 정한 동작이다.
    """
    if not settings.use_seed_adapter:
        return None
    try:
        ledger = _ledger_source().load(user_id)
    except (KeyError, FileNotFoundError, OSError):
        return None
    if not ledger.trading_days:
        return None
    return PortfolioEngine(ledger).snapshot(ledger.trading_days[-1])


def _find(snapshot: PortfolioSnapshot | None, ticker: str) -> Holding | None:
    if snapshot is None:
        return None
    return next((h for h in snapshot.holdings if h.symbol == ticker), None)


def _impact_values(holding: Holding, snapshot: PortfolioSnapshot) -> dict[str, Segment]:
    source = MetricSource.PORTFOLIO_ENGINE
    return {
        "weight": ratio_segment(holding.weight, source),
        "stock_weight": ratio_segment(holding.stock_weight, source),
        "return_rate": ratio_segment(holding.return_rate, source, signed=True),
        "top1_weight": ratio_segment(snapshot.concentration().top1, source),
    }


# ── 라우터 ───────────────────────────────────────────────
@router.post("/{ticker}/analysis")
async def create_analysis(
    ticker: str,
    body: AnalysisRequest,
    user_id: CurrentUser,
    db: DbSession,
) -> Envelope[dict]:
    if not _TICKER_RE.fullmatch(ticker):
        raise InvalidRequest("종목코드는 6자리 숫자입니다.", detail={"ticker": ticker})

    requested = list(body.sections or SECTIONS)
    unknown = [key for key in requested if key not in SECTION_TITLES]
    if unknown:
        raise InvalidRequest("알 수 없는 섹션입니다.", detail={"sections": unknown})

    client = get_llm_client()
    if isinstance(client, NullLlmClient):
        # 키가 없으면 전 섹션이 같은 이유로 실패한다. 섹션별 null로 흩뿌리지 않고
        # 한 번에 알린다. `app.rag.search`가 NullEmbedder를 다루는 방식과 같다.
        raise InsufficientData("LLM 키가 없어 분석을 생성할 수 없습니다.")

    hits = await search(f"{ticker} 최근 실적 공시 위험 요인", top_k=_RAG_TOP_K, ticker=ticker)
    citations = citations_from_hits(hits)
    documents = documents_block(hits, citations)
    if not hits:
        log.warning("종목 %s 검색 결과 0건 — 근거 없이 생성한다", ticker)

    snapshot = _snapshot(user_id) if body.personalize else None
    holding = _find(snapshot, ticker)
    thesis = (
        await get_active_thesis(db, user_id, ticker)
        if body.personalize and "thesis_check" in requested
        else None
    )

    shared = {"citations": citations, "documents": documents, "client": client}
    tasks: dict[str, Any] = {}
    for key in requested:
        if key in PERSONAL_SECTIONS and not body.personalize:
            continue
        if key == "my_impact":
            if holding is None or snapshot is None:
                continue  # 비보유 종목은 null. 에러가 아니다(§3).
            tasks[key] = generate_section(
                key,
                title=SECTION_TITLES[key],
                engine_values=_impact_values(holding, snapshot),
                **shared,
            )
        elif key == "thesis_check":
            if thesis is None:
                continue
            tasks[key] = generate_section(
                key,
                title=SECTION_TITLES[key],
                feature=Feature.THESIS_CHECK,
                wiki=f"{thesis.text} (기록: {thesis.recorded_at:%Y-%m-%d})",
                wiki_source=WikiSource(thesis.source),
                **shared,
            )
        else:
            tasks[key] = generate_section(key, title=SECTION_TITLES[key], **shared)

    outcomes: list[SectionOutcome] = list(await asyncio.gather(*tasks.values()))

    sections: dict[str, Any] = dict.fromkeys(requested)
    for outcome in outcomes:
        if outcome.section is None:
            log.warning("섹션 %s 생성 실패 · %s", outcome.key, "; ".join(outcome.reasons))
            continue
        sections[outcome.key] = outcome.section.model_dump(mode="json")

    if "thesis_check" in sections and sections["thesis_check"] and thesis is not None:
        sections["thesis_check"] |= {
            "thesis": {
                "text": thesis.text,
                "recorded_at": thesis.recorded_at.isoformat(),
                "source": thesis.source,
            },
            # ponytail: supporting/challenging은 별도 추출 패스가 필요하다.
            # 서술만으로도 화면이 성립하므로, 근거 분류 엔진이 붙을 때 채운다.
            "supporting": [],
            "challenging": [],
        }
    if "next_events" in sections and sections["next_events"]:
        # ponytail: 일정 캘린더 원천이 아직 없다. 수집기가 붙으면 여기를 채운다.
        sections["next_events"]["events"] = []

    envelope = Envelope[dict](
        content={
            "ticker": ticker,
            "name": _display_name(snapshot, ticker),
            "sections": sections,
        },
        citations=citations,
        data_as_of=DataAsOf(
            portfolio=_as_datetime(snapshot),
            filings=max((h["published_at"] for h in hits if h.get("published_at")), default=None),
        ),
    )
    await record(db, envelope, user_id=user_id, endpoint="stocks.analysis")
    return envelope


def _display_name(snapshot: PortfolioSnapshot | None, ticker: str) -> str:
    holding = _find(snapshot, ticker)
    return holding.name if holding else ticker


def _as_datetime(snapshot: PortfolioSnapshot | None) -> datetime | None:
    """스냅샷 기준일을 장 마감 시각으로 본다. 종가 기준이기 때문이다."""
    if snapshot is None:
        return None
    return datetime.combine(snapshot.trade_date, time(15, 30))
