"""Ask My Portfolio가 부르는 도구.

여기에는 계산이 없다. 이미 도는 파이프라인 — 포트폴리오 엔진, RAG 검색, 위키
저장소 — 을 Anthropic 도구 규격으로 감싸기만 한다. 로직을 옮겨 오면 두 벌이 되고,
두 벌은 언젠가 서로 다른 숫자를 낸다.

세 가지 규칙이 이 모듈의 형태를 정한다.

- **수치는 도구 결과에 자리표시자 이름과 함께 담긴다**(§3). 모델은 `metrics`의
  key를 `{{key}}`로만 쓰고, 실제 치환은 `app.llm.generate`가 한다. 표시 문자열을
  같이 보내는 것은 모델이 "이 값이 큰가 작은가"를 판단할 수 있어야 하기 때문이다.
- **근거 id는 검색한 순서로 확정된다.** `citations_from_hits`가 `cit_1`부터 붙이므로
  누적 목록에 넣은 직후의 길이가 곧 그 조각의 id다.
- **없으면 없다고 돌려준다.** 임베딩이 없거나 원장을 못 읽으면 빈 결과가 아니라
  `unavailable` 사유를 실어 보낸다. 모델이 "자료가 없다"와 "0건이다"를 구분해야 한다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time
from functools import lru_cache
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.adapters import SeedLedgerSource
from app.core.config import settings
from app.core.enums import MetricSource, Screen, WikiSource
from app.core.schemas import Segment
from app.engines.portfolio import PortfolioEngine, PortfolioSnapshot
from app.llm.generate import count_segment, krw_segment, ratio_segment
from app.rag.search import search
from app.wiki.store import list_facts, list_theses

log = logging.getLogger("app.llm.tools")

__all__ = [
    "TOOLS",
    "TOOL_NAMES",
    "ToolContext",
    "dispatch",
]

#: 한 번의 검색이 가져오는 조각 수. 종목 분석(§3)과 같은 값을 쓴다.
_TOP_K = 5

#: 가격 이력의 기본 조회 구간. 원장 시드가 10거래일이라 그보다 넉넉히 잡는다.
_DEFAULT_DAYS = 20


# ── 도구 정의 ────────────────────────────────────────────────────────────────
def _search_tool(name: str, description: str) -> dict[str, Any]:
    """세 검색 도구는 질의 성격만 다르고 입력 모양이 같다."""
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "찾고 싶은 내용을 한국어 자연어로. 종목명만 넣지 말고 무엇을 알고 싶은지 함께 쓴다.",
                },
                "ticker": {
                    "type": "string",
                    "description": "6자리 종목코드. 특정 종목으로 좁힐 때만 넣는다.",
                },
            },
            "required": ["query"],
        },
    }


TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_portfolio",
        "description": (
            "사용자의 현재 보유 종목, 비중, 평가손익, 현금 비중, 집중도를 돌려준다. "
            "'내 비중', '얼마나 벌었나', '분산이 잘 됐나' 같은 질문은 먼저 이것부터 부른다."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_price_history",
        "description": (
            "한 종목의 최근 종가 흐름과 구간 등락률을 돌려준다. "
            "'요즘 얼마야', '얼마나 빠졌어' 같은 질문에 쓴다. 미래 가격은 알 수 없다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "6자리 종목코드."},
                "days": {
                    "type": "integer",
                    "description": f"최근 몇 거래일을 볼지. 기본 {_DEFAULT_DAYS}.",
                },
            },
            "required": ["ticker"],
        },
    },
    _search_tool(
        "search_filings",
        "공시·사업보고서에서 근거 조각을 찾는다. 회사가 직접 밝힌 사실이 필요할 때 쓴다.",
    ),
    _search_tool(
        "search_news",
        "뉴스에서 근거 조각을 찾는다. 최근 무슨 일이 있었는지 물을 때 쓴다.",
    ),
    _search_tool(
        "get_financials",
        "매출·이익·부채 같은 재무 수치가 적힌 자료 조각을 찾는다. 실적을 물을 때 쓴다.",
    ),
    {
        "name": "get_wiki",
        "description": (
            "사용자가 남긴 투자 논지와 성향 기록을 돌려준다. "
            "'내가 왜 샀더라', '내 생각이 맞았나' 같은 질문에 쓴다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "6자리 종목코드. 한 종목의 논지만 볼 때 넣는다.",
                }
            },
        },
    },
]

TOOL_NAMES: tuple[str, ...] = tuple(tool["name"] for tool in TOOLS)

#: 같은 색인을 질의 성격만 바꿔 훑는다. `Document.doc_type`이 있지만
#: `app.rag.search.search()`가 아직 노출하지 않는다.
# ponytail: 검색이 doc_type 필터를 열면 세 도구를 그 인자로 갈라 붙인다.
#           지금은 질의 보강이 유일한 구분이고, 근거 제목은 원문 그대로 나간다.
_QUERY_HINT: dict[str, str] = {
    "search_filings": "공시 사업보고서",
    "search_news": "뉴스 보도",
    "get_financials": "매출 영업이익 재무제표",
}


# ── 실행 문맥 ────────────────────────────────────────────────────────────────
@dataclass
class ToolContext:
    """한 번의 대화가 쌓아 올리는 것.

    도구가 돌려준 값은 모델의 기억이 아니라 여기를 거쳐 생성 단계로 간다.
    수치가 자리표시자 규약을 우회할 길을 아예 없애기 위해서다.
    """

    user_id: str
    db: AsyncSession | None = None
    screen: Screen = Screen.CHAT
    ticker: str | None = None

    values: dict[str, Segment] = field(default_factory=dict)
    hits: list[dict[str, Any]] = field(default_factory=list)
    wiki: str = ""
    wiki_source: WikiSource | None = None
    used: list[str] = field(default_factory=list)
    portfolio_as_of: datetime | None = None

    #: 세션은 동시 사용이 안전하지 않다. DB를 만지는 도구만 줄 세운다.
    db_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


@lru_cache
def _ledger_source() -> SeedLedgerSource:
    return SeedLedgerSource(settings.seed_fixture_path)


def _snapshot(user_id: str) -> PortfolioSnapshot | None:
    """마지막 거래일 기준 스냅샷. 원장을 못 읽으면 None이다."""
    if not settings.use_seed_adapter:
        return None
    try:
        ledger = _ledger_source().load(user_id)
    except (KeyError, FileNotFoundError, OSError):
        return None
    if not ledger.trading_days:
        return None
    return PortfolioEngine(ledger).snapshot(ledger.trading_days[-1])


def _as_datetime(day: date) -> datetime:
    """기준일을 장 마감 시각으로 본다. 종가 기준이기 때문이다."""
    return datetime.combine(day, time(15, 30))


def _put(ctx: ToolContext, key: str, segment: Segment) -> tuple[str, str]:
    """엔진 값을 등록하고 (자리표시자 key, 표시 문자열)을 돌려준다."""
    ctx.values[key] = segment
    return key, segment.value


# ── 도구 본체 ────────────────────────────────────────────────────────────────
async def _get_portfolio(ctx: ToolContext, _: dict[str, Any]) -> dict[str, Any]:
    snapshot = _snapshot(ctx.user_id)
    if snapshot is None:
        return {"unavailable": "이 사용자의 원장을 읽을 수 없습니다."}
    if not snapshot.holdings:
        return {"as_of": snapshot.trade_date.isoformat(), "holdings": [], "metrics": {}}

    ctx.portfolio_as_of = _as_datetime(snapshot.trade_date)
    engine = MetricSource.PORTFOLIO_ENGINE
    concentration = snapshot.concentration()

    metrics = dict(
        [
            _put(ctx, "total_value", krw_segment(snapshot.total_value, engine)),
            _put(ctx, "cash_weight", ratio_segment(snapshot.cash_weight, engine)),
            _put(ctx, "top1_weight", ratio_segment(concentration.top1, engine)),
            _put(ctx, "top3_weight", ratio_segment(concentration.top3, engine)),
            _put(ctx, "n_eff", count_segment(concentration.n_eff, engine)),
        ]
    )

    holdings = []
    for holding in snapshot.holdings:
        holdings.append(
            {
                "ticker": holding.symbol,
                "name": holding.name,
                "sector": holding.sector,
                "metrics": dict(
                    [
                        _put(
                            ctx,
                            f"weight_{holding.symbol}",
                            ratio_segment(holding.weight, engine),
                        ),
                        _put(
                            ctx,
                            f"return_{holding.symbol}",
                            ratio_segment(holding.return_rate, engine, signed=True),
                        ),
                        _put(
                            ctx,
                            f"pnl_{holding.symbol}",
                            krw_segment(holding.unrealized_pnl, engine, signed=True),
                        ),
                    ]
                ),
            }
        )

    return {
        "as_of": snapshot.trade_date.isoformat(),
        "holdings": holdings,
        "metrics": metrics,
    }


async def _get_price_history(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ticker = str(args.get("ticker") or ctx.ticker or "").strip()
    if not ticker:
        return {"unavailable": "종목코드가 필요합니다."}
    if not settings.use_seed_adapter:
        return {"unavailable": "시세 원천이 연결되지 않았습니다."}

    try:
        ledger = _ledger_source().load(ctx.user_id)
    except (KeyError, FileNotFoundError, OSError):
        return {"unavailable": "이 사용자의 원장을 읽을 수 없습니다."}

    series = ledger.prices.get(ticker)
    if not series:
        return {"unavailable": f"{ticker}의 종가 이력이 원장에 없습니다."}

    days = max(2, int(args.get("days") or _DEFAULT_DAYS))
    window = sorted(series.items())[-days:]
    first, last = window[0][1], window[-1][1]
    closes = [price for _, price in window]

    price = MetricSource.PRICE
    metrics = dict(
        [
            _put(ctx, f"price_{ticker}", krw_segment(last, price)),
            _put(ctx, f"high_{ticker}", krw_segment(max(closes), price)),
            _put(ctx, f"low_{ticker}", krw_segment(min(closes), price)),
            _put(
                ctx,
                f"change_{ticker}",
                ratio_segment(last / first - 1 if first else 0.0, price, signed=True),
            ),
        ]
    )
    return {
        "ticker": ticker,
        "from": window[0][0].isoformat(),
        "to": window[-1][0].isoformat(),
        "sessions": len(window),
        "metrics": metrics,
    }


async def _search(ctx: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"unavailable": "검색어가 필요합니다."}

    ticker = str(args.get("ticker") or "").strip() or None
    hits = await search(f"{query} {_QUERY_HINT[name]}", top_k=_TOP_K, ticker=ticker)
    if not hits:
        # 임베딩이 없어도 `search`는 빈 목록을 돌려준다. 모델이 이 둘을 구분할
        # 필요는 없다 — 어느 쪽이든 "근거를 못 찾았다"가 정확한 사실이다.
        return {"hits": [], "note": "관련 자료를 찾지 못했습니다."}

    found = []
    for hit in hits:
        ctx.hits.append(hit)
        found.append(
            {
                "citation": f"cit_{len(ctx.hits)}",
                "title": hit.get("title") or "제목 없음",
                "ticker": hit.get("ticker"),
                "published_at": (
                    published.isoformat()
                    if isinstance(published := hit.get("published_at"), datetime)
                    else None
                ),
                "text": hit.get("text") or "",
            }
        )
    return {"hits": found}


async def _get_wiki(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.db is None:
        return {"unavailable": "위키 저장소가 연결되지 않았습니다."}

    ticker = str(args.get("ticker") or "").strip() or None
    async with ctx.db_lock:
        theses = await list_theses(ctx.db, ctx.user_id)
        facts = await list_facts(ctx.db, ctx.user_id)

    if ticker:
        theses = [t for t in theses if t.ticker == ticker]
    if not theses and not facts:
        return {"theses": [], "facts": [], "note": "기록된 논지나 성향이 없습니다."}

    lines: list[str] = []
    sources: list[WikiSource] = []
    payload_theses = []
    for thesis in theses:
        sources.append(WikiSource(thesis.source))
        lines.append(f"[{thesis.ticker}] {thesis.text} (기록: {thesis.recorded_at:%Y-%m-%d})")
        payload_theses.append(
            {
                "ticker": thesis.ticker,
                "text": thesis.text,
                "source": str(thesis.source),
                "recorded_at": thesis.recorded_at.isoformat(),
            }
        )

    payload_facts = []
    for fact in facts:
        sources.append(WikiSource(fact.source))
        lines.append(f"[성향] {fact.text}")
        payload_facts.append({"text": fact.text, "source": str(fact.source)})

    ctx.wiki = "\n".join(lines)
    # 추론 항목이 하나라도 섞이면 어투 검사(§4.2)를 엄한 쪽으로 맞춘다.
    ctx.wiki_source = (
        WikiSource.AI_INFERRED
        if WikiSource.AI_INFERRED in sources
        else (sources[0] if sources else None)
    )
    return {"theses": payload_theses, "facts": payload_facts}


# ── 디스패치 ─────────────────────────────────────────────────────────────────
async def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """도구 하나를 실행한다. 실패해도 예외를 던지지 않는다.

    도구가 터지면 대화 전체가 끝나는 것이 아니라 모델이 다른 수단을 고를 수 있어야
    한다. 사유를 결과에 실어 보내고 판단을 넘긴다.
    """
    ctx.used.append(name)
    try:
        if name == "get_portfolio":
            return await _get_portfolio(ctx, args)
        if name == "get_price_history":
            return await _get_price_history(ctx, args)
        if name in _QUERY_HINT:
            return await _search(ctx, name, args)
        if name == "get_wiki":
            return await _get_wiki(ctx, args)
    except Exception:  # noqa: BLE001 — 어떤 원천이 터져도 대화는 이어져야 한다
        log.exception("도구 %s 실행 실패", name)
        return {"unavailable": "자료를 가져오지 못했습니다."}

    log.warning("정의되지 않은 도구 호출 · %s", name)
    return {"unavailable": f"{name}은(는) 없는 도구입니다."}
