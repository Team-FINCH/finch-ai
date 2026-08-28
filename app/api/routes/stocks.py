"""종목 AI 분석. API 명세 §3.

담당 트랙: feat/rag-dart, feat/rag-search, feat/llm-pipeline

섹션은 두 부류로 나뉜다. `current`·`changes`·`attention`·`risks`·`next_events`는
사용자와 무관해 종목 단위로 캐시할 수 있고, `my_impact`·`thesis_check`만
사용자별이다(§3 비용 설계). 공통 섹션은 같은 프롬프트 버전의 최근 응답을
6시간 재사용하고, 개인화 섹션은 요청마다 사용자의 최신 원장·논지로 만든다.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Annotated, Any

from fastapi import APIRouter
from pydantic import BaseModel, Field, WithJsonSchema, field_serializer
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.core.adapters import ledger_source
from app.core.enums import EventType, MetricSource, WikiSource
from app.core.errors import InsufficientData, InvalidRequest
from app.core.models import AIResponse, Event
from app.core.response_log import record
from app.core.schemas import Citation, ContentModel, DataAsOf, Envelope, Segment, now_kst
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
from app.llm.versioning import prompt_version_for
from app.rag.search import search
from app.wiki.store import get_active_thesis

log = logging.getLogger("app.api.stocks")

router = APIRouter(prefix="/stocks", tags=["stocks"])

class AnalysisSectionKey(StrEnum):
    CURRENT = "current"
    CHANGES = "changes"
    ATTENTION = "attention"
    RISKS = "risks"
    MY_IMPACT = "my_impact"
    THESIS_CHECK = "thesis_check"
    NEXT_EVENTS = "next_events"


SECTIONS = tuple(key.value for key in AnalysisSectionKey)
AnalysisSectionName = Annotated[
    str,
    WithJsonSchema({"type": "string", "enum": list(SECTIONS)}),
]

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
COMMON_SECTIONS = frozenset(SECTIONS) - PERSONAL_SECTIONS

_TICKER_RE = re.compile(r"\d{6}")
_RAG_TOP_K = 6
_UPCOMING_DAYS = 90
_UPCOMING_LIMIT = 5
_COMMON_CACHE_TTL = timedelta(hours=6)


class AnalysisRequest(BaseModel):
    sections: list[AnalysisSectionName] | None = Field(default=None)
    personalize: bool = True


class ThesisRecord(BaseModel):
    text: str
    recorded_at: datetime
    source: str


class ThesisEvidence(ContentModel):
    citation_id: str
    title: str
    source: str
    rationale: str


class UpcomingEvent(ContentModel):
    id: str
    type: EventType
    title: str
    event_date: date
    confirmed: bool
    days_until: int


class AnalysisSection(ContentModel):
    title: str | None = None
    text: str
    segments: list[Segment]
    cached: bool = False
    cached_at: datetime | None = None
    thesis: ThesisRecord | None = None
    supporting: list[ThesisEvidence] | None = None
    challenging: list[ThesisEvidence] | None = None
    events: list[UpcomingEvent] | None = None


class AnalysisSections(ContentModel):
    current: AnalysisSection | None = None
    changes: AnalysisSection | None = None
    attention: AnalysisSection | None = None
    risks: AnalysisSection | None = None
    my_impact: AnalysisSection | None = None
    thesis_check: AnalysisSection | None = None
    next_events: AnalysisSection | None = None


class AnalysisContent(BaseModel):
    ticker: str
    name: str
    sections: AnalysisSections

    @field_serializer("sections")
    def _serialize_sections(
        self, sections: AnalysisSections
    ) -> dict[str, dict[str, Any] | None]:
        return sections.model_dump(mode="json", exclude_unset=True)


# ── 원장 ─────────────────────────────────────────────────
async def _snapshot(user_id: str) -> PortfolioSnapshot | None:
    """마지막 거래일 기준 스냅샷. 원장을 못 읽으면 None이다.

    어느 원장을 읽을지는 `ledger_source()` 가 정한다(설정 `LEDGER_SOURCE`).
    원천이 없거나(`none`) 읽기에 실패하면 없는 것을 억지로 만들지 않고 개인화
    섹션만 조용히 비운다 — §3이 정한 동작이다.
    """
    source = ledger_source()
    if source is None:
        return None
    try:
        ledger = await source.load(user_id)
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


async def _upcoming_events(
    db: DbSession, ticker: str, *, today: date | None = None
) -> list[UpcomingEvent]:
    base = today or now_kst().date()
    end = base + timedelta(days=_UPCOMING_DAYS)
    rows = (
        await db.scalars(
            select(Event)
            .where(
                Event.ticker == ticker,
                Event.confirmed.is_(True),
                Event.event_date >= base,
                Event.event_date <= end,
            )
            .order_by(Event.event_date, Event.importance.desc())
            .limit(_UPCOMING_LIMIT)
        )
    ).all()
    return [
        UpcomingEvent(
            id=str(row.id),
            type=EventType(row.event_type),
            title=row.title,
            event_date=row.event_date,
            confirmed=row.confirmed,
            days_until=(row.event_date - base).days,
        )
        for row in rows
    ]


def _schedule_block(events: list[UpcomingEvent]) -> str:
    return "\n".join(
        f"- {event.event_date.year}년 {event.event_date.month}월 {event.event_date.day}일 "
        f"{event.title} ({event.type.value}, 확정)"
        for event in events
    )


@dataclass(frozen=True, slots=True)
class CachedAnalysis:
    name: str
    sections: dict[str, dict[str, Any]]
    citations: list[Citation]
    data_as_of: DataAsOf
    cached_at: datetime


async def _cached_common_sections(
    db: DbSession,
    ticker: str,
    keys: set[str],
    *,
    now: datetime,
) -> CachedAnalysis | None:
    if not keys or db is None:
        return None
    version = prompt_version_for("stocks.analysis")
    try:
        rows = (
            await db.scalars(
                select(AIResponse)
                .where(
                    AIResponse.endpoint == "stocks.analysis",
                    AIResponse.prompt_version == version,
                    AIResponse.created_at >= now - _COMMON_CACHE_TTL,
                )
                .order_by(AIResponse.created_at.desc())
                .limit(20)
            )
        ).all()
    except Exception:  # 캐시는 최적화이므로 장애가 본래 분석을 막으면 안 된다.
        log.warning("종목 %s 공통 분석 캐시 조회 실패", ticker, exc_info=True)
        return None
    for row in rows:
        payload = row.payload
        if not isinstance(payload, dict):
            continue
        content = payload.get("content")
        if not isinstance(content, dict):
            continue
        sections = content.get("sections")
        if content.get("ticker") != ticker or not isinstance(sections, dict):
            continue
        if not keys.issubset(sections) or any(sections[key] is None for key in keys):
            continue
        cached_at = row.created_at
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=now.tzinfo)
        selected = {
            key: {
                **sections[key],
                "cached": True,
                "cached_at": cached_at.isoformat(),
            }
            for key in keys
        }
        return CachedAnalysis(
            name=str(content.get("name") or ticker),
            sections=selected,
            citations=[Citation.model_validate(item) for item in payload.get("citations", [])],
            data_as_of=DataAsOf.model_validate(payload.get("data_as_of", {})),
            cached_at=cached_at,
        )
    return None


def _hits_from_cache(cached: CachedAnalysis) -> list[dict[str, Any]]:
    return [
        {
            "text": citation.snippet or citation.title,
            "ticker": None,
            "title": citation.title,
            "published_at": citation.published_at,
            "doc_type": citation.type.value,
            "source": citation.source,
            "publisher": citation.publisher,
            "url": citation.url,
            "similarity": citation.relevance,
        }
        for citation in cached.citations
    ]


# ── 라우터 ───────────────────────────────────────────────
@router.post("/{ticker}/analysis")
async def create_analysis(
    ticker: str,
    body: AnalysisRequest,
    user_id: CurrentUser,
    db: DbSession,
) -> Envelope[AnalysisContent]:
    if not _TICKER_RE.fullmatch(ticker):
        raise InvalidRequest("종목코드는 6자리 숫자입니다.", detail={"ticker": ticker})

    requested = list(body.sections or SECTIONS)
    unknown = [key for key in requested if key not in SECTION_TITLES]
    if unknown:
        raise InvalidRequest("알 수 없는 섹션입니다.", detail={"sections": unknown})

    now = now_kst()
    common_requested = set(requested) & COMMON_SECTIONS
    cached = await _cached_common_sections(db, ticker, common_requested, now=now)
    snapshot = await _snapshot(user_id) if body.personalize else None
    holding = _find(snapshot, ticker)
    thesis = (
        await get_active_thesis(db, user_id, ticker)
        if body.personalize and "thesis_check" in requested
        else None
    )
    generation_keys = [
        key for key in requested if cached is None or key not in cached.sections
    ]
    generation_keys = [
        key
        for key in generation_keys
        if not (key in PERSONAL_SECTIONS and not body.personalize)
        and not (key == "my_impact" and (holding is None or snapshot is None))
        and not (key == "thesis_check" and thesis is None)
    ]

    client = get_llm_client()
    if generation_keys and isinstance(client, NullLlmClient):
        raise InsufficientData(
            "지금은 분석을 만들 수 없습니다.", detail={"reason": "llm_key_missing"}
        )

    hits = _hits_from_cache(cached) if cached else []
    if generation_keys and not cached:
        hits = await search(
            f"{ticker} 최근 실적 공시 위험 요인",
            top_k=_RAG_TOP_K,
            ticker=ticker,
        )
    citations = cached.citations if cached else citations_from_hits(hits)
    documents = documents_block(hits, citations)
    if generation_keys and not hits:
        log.warning("종목 %s 검색 결과 0건 — 근거 없이 생성한다", ticker)

    upcoming_events = (
        await _upcoming_events(db, ticker) if "next_events" in generation_keys else []
    )

    shared = {"citations": citations, "documents": documents, "client": client}
    tasks: dict[str, Any] = {}
    for key in generation_keys:
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
            tasks[key] = generate_section(
                key,
                title=SECTION_TITLES[key],
                schedule=_schedule_block(upcoming_events) if key == "next_events" else "",
                **shared,
            )

    outcomes: list[SectionOutcome] = list(await asyncio.gather(*tasks.values()))

    sections: dict[str, Any] = dict.fromkeys(requested)
    if cached:
        sections.update(cached.sections)
    outcomes_by_key = {outcome.key: outcome for outcome in outcomes}
    for outcome in outcomes:
        if outcome.section is None:
            log.warning("섹션 %s 생성 실패 · %s", outcome.key, "; ".join(outcome.reasons))
            continue
        sections[outcome.key] = outcome.section.model_dump(mode="json")

    if "thesis_check" in sections and sections["thesis_check"] and thesis is not None:
        evidence_by_id = {citation.id: citation for citation in citations}
        classified = outcomes_by_key["thesis_check"].thesis_evidence

        def _evidence_payload(stance: str) -> list[dict[str, str]]:
            return [
                {
                    "citation_id": item.citation_id,
                    "title": evidence_by_id[item.citation_id].title,
                    "source": evidence_by_id[item.citation_id].source,
                    "rationale": item.rationale,
                }
                for item in classified
                if item.stance == stance
            ]

        sections["thesis_check"] |= {
            "thesis": {
                "text": thesis.text,
                "recorded_at": thesis.recorded_at.isoformat(),
                "source": thesis.source,
            },
            "supporting": _evidence_payload("supporting"),
            "challenging": _evidence_payload("challenging"),
        }
    if "next_events" in generation_keys and sections.get("next_events"):
        sections["next_events"]["events"] = [
            event.model_dump(mode="json") for event in upcoming_events
        ]

    cached_data = cached.data_as_of if cached else DataAsOf()
    envelope = Envelope[AnalysisContent](
        content={
            "ticker": ticker,
            "name": (
                _display_name(snapshot, ticker)
                if snapshot
                else cached.name
                if cached
                else ticker
            ),
            "sections": sections,
        },
        citations=citations,
        cached=bool(cached),
        data_as_of=DataAsOf(
            price=cached_data.price,
            portfolio=_as_datetime(snapshot),
            filings=max(
                (h["published_at"] for h in hits if h.get("published_at")),
                default=cached_data.filings,
            ),
            news=cached_data.news,
            macro=cached_data.macro,
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
