"""DART 공시목록 → events 적재.

브리핑 랭킹(§5.2)과 기여도 귀속(`/portfolio/attribution`)이 읽는 표다.
지금까지 아무도 채우지 않아 `contributors[].events`가 늘 비어 있었다.

**목록 API만 쓴다.** 원문 다운로드·청킹·임베딩이 없으므로 `app.rag.dart`의
공시 적재와는 비용 등급이 다르다. `Event.document_id`가 nullable인 덕에
저장된 원문 없이도 이벤트가 성립하고, 그래서 수천 종목을 싸게 훑을 수 있다.

    python -m ingest.events --days 30
    python -m ingest.events --days 90 --limit 3000   # 전 종목 훑기(명시적 선택)

재실행해도 중복이 생기지 않는다 — 다만 events에는 유니크 제약이 없어
DB가 막아주지 않는다. 아래 `_existing_keys()`가 유일한 방어선이다.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import SessionFactory, engine
from app.core.enums import EventType
from app.core.models import Event
from app.rag.dart import REQUEST_DELAY_S, Filing, fetch_filing_list, load_targets

logger = logging.getLogger("ingest.events")

# 분류 규칙: (report_nm에 포함되면, event_type, importance). 위에서부터 먼저 맞는 것.
#
# importance 값은 엔진 산식 §5.2가 고정한 것이라 마음대로 바꾸면 W16 브리핑
# 랭킹이 조용히 틀어진다. 등급은 손대지 말고 여기 부분문자열만 늘려라.
#
# 부분문자열이 구분자를 건너뛰지 않게 잘랐다. DART는 가운뎃점을 U+00B7(·)이
# 아니라 U+318D(ㆍ)로 쓴다 — "단일판매ㆍ공급계약체결", "임원ㆍ주요주주...".
# 설계 문서의 "공급계약 · 유상증자" 표기를 그대로 붙여넣으면 하나도 안 맞는다.
RULES: list[tuple[tuple[str, ...], EventType, float]] = [
    # 1.0 실적 발표 · 실적 정정. [기재정정] 접두사가 붙어도 본체 이름으로 걸린다.
    (
        ("사업보고서", "반기보고서", "분기보고서", "매출액또는손익구조", "영업(잠정)실적"),
        EventType.EARNINGS,
        1.0,
    ),
    # 배당은 §5.2의 0.8 목록에 없다. 없는 등급을 지어내지 않고 일반 공시로 둔다.
    (("배당",), EventType.DIVIDEND, 0.4),
    # 0.8 주요 공시
    (
        ("공급계약", "유상증자", "자기주식", "대량보유", "주요주주"),
        EventType.FILING,
        0.8,
    ),
]

DEFAULT = (EventType.FILING, 0.4)  # 0.4 그 밖의 일반 공시


def classify(report_nm: str) -> tuple[EventType, float]:
    """공시 제목에서 (종류, 중요도). 모르면 일반 공시로 떨어진다."""
    for needles, event_type, importance in RULES:
        if any(n in report_nm for n in needles):
            return event_type, importance
    return DEFAULT


def to_row(filing: Filing, today: date) -> dict | None:
    """공시 한 건 → events 행. 넣을 수 없으면 None.

    event_date가 NOT NULL이라 접수일자가 깨진 건은 버린다. 미래 일자도 버린다 —
    목록 API는 접수 완료 건만 주므로 미래 일자는 데이터 오류다.
    """
    published = filing.published_at
    if published is None:
        return None
    event_date = published.date()
    if event_date > today:
        return None

    # DART 제목은 공백으로 패딩돼 온다. 중복 판정 키에 들어가므로 여기서 눌러둔다.
    title = " ".join(filing.report_nm.split())
    if not title:
        return None

    event_type, importance = classify(title)
    return {
        "event_type": event_type.value,
        "ticker": filing.ticker,
        "title": title,
        "event_date": event_date,
        "confirmed": True,  # 접수 완료된 공시다
        "importance": importance,
        "document_id": None,  # 원문을 받지 않는다
    }


async def _existing_keys(
    session: AsyncSession, start: date, end: date
) -> set[tuple[str | None, date, str]]:
    """구간 안에 이미 있는 (ticker, event_date, title).

    events에는 유니크 제약이 없다 — __table_args__에 평범한 Index 하나뿐이다.
    제약을 추가하려면 마이그레이션이고 그건 범위 밖이라, 중복 방지를 여기서 한다.
    """
    rows = await session.execute(
        select(Event.ticker, Event.event_date, Event.title).where(
            Event.event_date >= start, Event.event_date <= end
        )
    )
    return {(t, d, title) for t, d, title in rows}


async def ingest(
    *, days: int, limit: int, api_key: str | None = None
) -> dict[str, int]:
    """공시목록을 훑어 events를 채우고 요약을 돌려준다."""
    stats = {
        "targets": 0,      # 시도한 종목 수
        "with_filings": 0, # 공시가 하나라도 나온 종목 수
        "filings": 0,      # 받은 공시 건수
        "rows": 0,         # 실제로 넣은 행
        "duplicates": 0,   # 이미 있어서 건너뛴 행
        "unusable": 0,     # 날짜가 깨져 버린 건
        "failed": 0,       # 종목 단위 실패
    }

    key = (api_key or settings.dart_api_key or "").strip()
    if not key:
        logger.error("DART_API_KEY가 없다. .env를 확인할 것")
        stats["failed"] = 1
        return stats

    targets = await load_targets(limit)
    if not targets:
        logger.warning("corp_code가 있는 종목이 없다. 먼저 python -m ingest.instruments 를 실행할 것")
        return stats
    stats["targets"] = len(targets)

    today = date.today()
    start = today - timedelta(days=days)
    logger.info("대상 %d종목 · %s ~ %s", len(targets), start, today)

    # 수집은 동기 httpx다. DB 세션을 붙든 채 돌면 커넥션을 수십 분 잡아먹는다 —
    # app.rag.dart.run과 같은 이유로 세션 밖에서 끝낸다.
    rows: list[dict] = []
    bgn_de, end_de = start.strftime("%Y%m%d"), today.strftime("%Y%m%d")
    with httpx.Client() as client:
        for i, (ticker, corp_code) in enumerate(targets, 1):
            try:
                filings = fetch_filing_list(client, key, corp_code, ticker, bgn_de, end_de)
            except Exception:
                # 한 종목이 죽어도 배치를 멈추지 않는다. 재실행하면 실패분만 다시 받는다.
                logger.exception("공시목록 실패: %s", ticker)
                stats["failed"] += 1
                continue

            if filings:
                stats["with_filings"] += 1
            stats["filings"] += len(filings)
            for filing in filings:
                row = to_row(filing, today)
                if row is None:
                    stats["unusable"] += 1
                else:
                    rows.append(row)

            if i % 100 == 0:
                logger.info("진행 %d/%d · 공시 %d건", i, len(targets), stats["filings"])
            await asyncio.sleep(REQUEST_DELAY_S)

    async with SessionFactory() as session:
        known = await _existing_keys(session, start, today)
        fresh = []
        for row in rows:
            if (row["ticker"], row["event_date"], row["title"]) in known:
                stats["duplicates"] += 1
                continue
            fresh.append(row)
        if fresh:
            await session.execute(Event.__table__.insert(), fresh)
            await session.commit()
        stats["rows"] = len(fresh)

    return stats


async def _main() -> None:
    parser = argparse.ArgumentParser(description="DART 공시목록 → events 적재")
    parser.add_argument("--days", type=int, default=30, help="오늘 기준 며칠 전부터 (기본 30)")
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="대상 종목 수 (기본 100). DART 일일 한도가 20,000회라 전 종목(약 2,600)"
        " 훑기는 --limit 3000 으로 명시할 것",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # crtfc_key가 로그에 남으면 안 된다

    try:
        s = await ingest(days=args.days, limit=args.limit)
        logger.info(
            "완료 — %d/%d종목에서 공시 %d건 · 신규 %d행 · 중복 %d · 버림 %d · 실패 %d종목",
            s["with_filings"],
            s["targets"],
            s["filings"],
            s["rows"],
            s["duplicates"],
            s["unusable"],
            s["failed"],
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
