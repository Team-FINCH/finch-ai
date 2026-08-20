"""데일리 브리핑. API 명세 §8.

배치 생성 결과 조회이므로 GET이다. 랭킹은 규칙 엔진이 하고 LLM은 상위 4건만 문장화한다.
담당 트랙: feat/event-ranking
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date as Date
from datetime import datetime, time, timedelta
from functools import lru_cache
from typing import Any

from fastapi import APIRouter
from sqlalchemy import or_, select

from app.api.deps import CurrentUser, DbSession
from app.core.adapters import Ledger, SeedLedgerSource
from app.core.config import settings
from app.core.enums import BriefingStatus, MetricSource, RateSensitivity
from app.core.errors import InsufficientData
from app.core.models import AIResponse, Event
from app.core.response_log import record
from app.core.schemas import DataAsOf, Envelope, Segment, now_kst
from app.engines.attribution import EventRecord
from app.engines.briefing import (
    Candidate,
    RankedItem,
    event_candidates,
    holding_moves,
    item_key,
    rank,
    sector_shifts,
)
from app.engines.portfolio import PortfolioEngine, PortfolioSnapshot
from app.llm.client import NullLlmClient, get_llm_client
from app.llm.generate import generate_section, ratio_segment
from app.llm.guard import Feature

log = logging.getLogger("app.api.briefing")

router = APIRouter(prefix="/briefing", tags=["briefing"])

_SOURCE = MetricSource.PORTFOLIO_ENGINE

#: §5.2 novelty가 되짚는 구간.
_NOVELTY_DAYS = 7

#: 구조 변화의 기준 시점. 하루 전과 비교하면 매매가 없는 날은 항상 0이고, 원장
#: 첫날과 비교하면 반년 전 편입이 오늘의 뉴스가 된다. 한 달이 "구조"의 눈금이다.
_SHIFT_LOOKBACK_DAYS = 30

_ENDPOINT = "briefing"


# ── 원장 ──────────────────────────────────────────────────────────────────────
@lru_cache
def _ledger_source() -> SeedLedgerSource:
    return SeedLedgerSource(settings.seed_fixture_path)


def _ledger(user_id: str) -> Ledger | None:
    """원장 스냅샷. 못 읽으면 None이다(§11).

    브리핑에서 None은 오류가 아니다 — 진단과 달리 status "empty"로 나간다.
    """
    if not settings.use_seed_adapter:
        return None
    try:
        ledger = _ledger_source().load(user_id)
    except (KeyError, FileNotFoundError, OSError):
        return None
    return ledger if ledger.trading_days else None


# ── 라우터 ────────────────────────────────────────────────────────────────────
@router.get("")
async def get_briefing(
    user_id: CurrentUser, db: DbSession, date: Date | None = None
) -> Envelope[dict]:
    """그날 알아야 할 일 최대 4건(§8).

    보유 종목이 없거나 유의미한 항목이 없으면 409가 아니라 200 + status "empty"다.
    프런트가 그 값으로 섹션을 숨기기로 되어 있어서, 여기서 끊으면 화면이 에러를 문다.

    ponytail: 배치가 없어 요청 시점에 계산한다. 스케줄러가 생기면 여기서 조회만 한다.
    그래서 status는 ready 아니면 empty다 — generating을 돌려줄 주체가 없다.
    """
    now = now_kst()
    ledger = _ledger(user_id)
    if ledger is None:
        return await _empty(db, user_id, date, now)

    day = _resolve_day(ledger, date)
    if day is None:
        return await _empty(db, user_id, date, now)

    engine = PortfolioEngine(ledger)
    snapshot = engine.snapshot(day)
    if not snapshot.holdings:
        return await _empty(db, user_id, day, now)

    rows = {row.trade_date: row for row in engine.daily_returns()}
    baseline = _baseline_snapshot(engine, ledger, day)
    candidates = [
        *holding_moves(snapshot, rows.get(day)),
        *sector_shifts(baseline, snapshot),
        *event_candidates(
            await _events(db, snapshot, day),
            snapshot,
            day,
            rate_sensitivity=_rate_sensitivity(snapshot),
        ),
    ]
    top = rank(candidates, seen_keys=await _seen_keys(db, user_id, now))
    if not top:
        return await _empty(db, user_id, day, now)

    client = get_llm_client()
    if isinstance(client, NullLlmClient):
        # 키가 없으면 네 건이 같은 이유로 실패한다. 항목마다 null로 흩뿌리지 않는다.
        raise InsufficientData("LLM 키가 없어 브리핑을 생성할 수 없습니다.")

    outcomes = await asyncio.gather(
        *(
            generate_section(
                str(item.rank),
                title=item.candidate.title,
                feature=Feature.DAILY_BRIEFING_ITEM,
                prompt="daily_briefing",
                client=client,
                engine_values=_values(item.candidate),
                request=item.candidate.request,
            )
            for item in top
        )
    )

    items: list[dict[str, Any]] = []
    for item, outcome in zip(top, outcomes, strict=True):
        if outcome.section is None:
            # 문장이 없으면 항목 자체를 내리고 뒤를 당긴다. 화면에 제목만 남은 빈
            # 카드를 띄우느니 세 건짜리 브리핑이 낫다.
            log.warning(
                "브리핑 항목 %s 생성 실패 · %s", item.candidate.key, "; ".join(outcome.reasons)
            )
            continue
        items.append(_item_payload(item, outcome.section.model_dump(mode="json"), len(items) + 1))

    envelope = Envelope[dict](
        content={
            "date": day.isoformat(),
            "status": (BriefingStatus.READY if items else BriefingStatus.EMPTY).value,
            "generated_at": now.isoformat(),
            "items": items,
        },
        data_as_of=DataAsOf(price=_as_datetime(day), portfolio=_as_datetime(day)),
    )
    # 다음 날 novelty가 읽을 행이 되고, 피드백이 참조할 행이 된다(§10).
    await record(db, envelope, user_id=user_id, endpoint=_ENDPOINT)
    return envelope


# ── 응답 조립 ─────────────────────────────────────────────────────────────────
async def _empty(
    db: DbSession, user_id: str, day: Date | None, now: datetime
) -> Envelope[dict]:
    """보유 종목이 없거나 내보낼 항목이 없을 때. 오류가 아니다."""
    envelope = Envelope[dict](
        content={
            "date": day.isoformat() if day else None,
            "status": BriefingStatus.EMPTY.value,
            "generated_at": now.isoformat(),
            "items": [],
        }
    )
    await record(db, envelope, user_id=user_id, endpoint=_ENDPOINT)
    return envelope


def _item_payload(item: RankedItem, section: dict[str, Any], rank_: int) -> dict[str, Any]:
    """§8 `items` 한 줄. rank는 생성에 실패한 항목을 뺀 뒤 다시 매긴다."""
    candidate = item.candidate
    return {
        "rank": rank_,
        "category": candidate.category.value,
        "relevance_score": round(item.relevance, 4),
        "title": candidate.title,
        "text": section["text"],
        "segments": section["segments"],
        "related_tickers": list(candidate.related_tickers),
        "deeplink": candidate.deeplink,
        # ponytail: 이 기능은 문서를 조회하지 않아 인용할 원문이 없다. events에
        # document_id가 차면 그때 Citation을 실어 보낸다.
        "citations": [],
    }


def _values(candidate: Candidate) -> dict[str, Segment]:
    """항목이 쓸 수 있는 자리표시자. 항목과 무관한 수치는 주지 않는다."""
    signed = {"return_rate", "sector_shift"}
    return {
        name: ratio_segment(value, _SOURCE, signed=name in signed, digits=2)
        for name, value in candidate.values.items()
    }


# ── 엔진 입력 ─────────────────────────────────────────────────────────────────
def _resolve_day(ledger: Ledger, requested: Date | None) -> Date | None:
    """기준 거래일. 요청일이 휴장일이면 그 이전 마지막 거래일로 내린다."""
    if requested is None:
        return ledger.trading_days[-1]
    past = [day for day in ledger.trading_days if day <= requested]
    return past[-1] if past else None


def _baseline_snapshot(
    engine: PortfolioEngine, ledger: Ledger, day: Date
) -> PortfolioSnapshot:
    """구조 변화의 비교 기준. 한 달 안에 거래일이 하나뿐이면 자기 자신이라 변화가 0이다."""
    window = day - timedelta(days=_SHIFT_LOOKBACK_DAYS)
    earlier = [d for d in ledger.trading_days if window <= d <= day]
    return engine.snapshot(earlier[0] if earlier else day)


def _rate_sensitivity(snapshot: PortfolioSnapshot) -> RateSensitivity:
    """§5.2 거시 이벤트의 계수. 금리민감도 판정은 Risk Engine이 유일한 출처다."""
    from app.engines.risk import _rate_exposure

    return _rate_exposure(snapshot).level


async def _events(
    db: DbSession, snapshot: PortfolioSnapshot, day: Date
) -> list[EventRecord]:
    """보유 종목 이벤트와 매크로 이벤트. 표가 비어 있으면 빈 목록이다.

    되짚는 구간은 novelty와 같은 7일이다 — recency가 exp(−days/2)라 그보다 오래된
    이벤트는 0.03배로 눌려 어차피 4위 안에 못 든다.
    """
    symbols = [h.symbol for h in snapshot.holdings]
    rows = (
        await db.execute(
            select(
                Event.id,
                Event.event_type,
                Event.ticker,
                Event.title,
                Event.event_date,
                Event.document_id,
                Event.importance,
            ).where(
                Event.event_date > day - timedelta(days=_NOVELTY_DAYS),
                Event.event_date <= day,
                or_(Event.ticker.in_(symbols), Event.ticker.is_(None)),
            )
        )
    ).all()
    return [
        EventRecord(
            event_id=str(event_id),
            event_type=event_type,
            title=title,
            event_date=event_date,
            ticker=ticker,
            document_id=str(document_id) if document_id else None,
            importance=float(importance),
        )
        for event_id, event_type, ticker, title, event_date, document_id, importance in rows
    ]


async def _seen_keys(db: DbSession, user_id: str, now: datetime) -> set[str]:
    """최근 7일 브리핑에 이미 나갔던 항목 key(§5.2 novelty).

    응답 로그의 payload에서 읽는다. 노출 이력 하나 때문에 스키마를 넓히는 대신
    `record()`가 이미 통째로 남기는 봉투를 되읽는다 — `last_risk_level`과 같은 수다.
    """
    stmt = select(AIResponse.payload).where(
        AIResponse.user_id == user_id,
        AIResponse.endpoint == _ENDPOINT,
        AIResponse.created_at >= now - timedelta(days=_NOVELTY_DAYS),
    )
    keys: set[str] = set()
    for payload in (await db.execute(stmt)).scalars():
        if not isinstance(payload, dict):
            continue
        content = payload.get("content")
        items = content.get("items") if isinstance(content, dict) else None
        for item in items or ():
            if not isinstance(item, dict):
                continue
            category, title = item.get("category"), item.get("title")
            if category and title:
                keys.add(item_key(category, item.get("related_tickers") or [], title))
    return keys


def _as_datetime(day: Date) -> datetime:
    """기준일을 장 마감 시각으로 본다. 종가 기준이기 때문이다."""
    return datetime.combine(day, time(15, 30))
