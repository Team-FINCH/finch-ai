"""KRX OpenAPI 적재 — 지수 일별 시세와 시가총액 스냅샷.

리스크 엔진의 베타 계산에 벤치마크 지수 시계열이, §3.5 스타일 분류에
종목별 시가총액·상장주식수가 필요하다. 둘 다 이 API 하나에서 나온다.

    python -m ingest.krx index                # 지수 시계열 (증분)
    python -m ingest.krx index --days 730 --full
    python -m ingest.krx marketcap            # 시가총액 스냅샷

pykrx와 달리 이 API는 **날짜로 조회한다**. 한 번 호출하면 그날의 전 종목
(또는 전 지수)이 한꺼번에 온다. 그래서 루프가 도는 축은 종목이 아니라 개장일이다.
ingest/prices.py의 종목 루프를 그대로 베끼면 안 된다.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import SessionFactory, engine
from app.core.models import IndexDaily, Instrument, PriceDaily

logger = logging.getLogger("ingest.krx")

BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"
INDEX_PATH = "/idx/kospi_dd_trd"
# 코스피 942종목 + 코스닥 1,821종목. 한쪽만 받으면 절반이 비어 있는다.
STOCK_PATHS = ("/sto/stk_bydd_trd", "/sto/ksq_bydd_trd")

HTTP_TIMEOUT = 30.0
# 호출당 0.07초에 연속 호출로도 조이지 않는다. 2년치 485영업일이면 35초라
# 평시 페이싱은 두지 않는다. 다만 429가 오면 Retry-After를 지킨다 —
# 재시도가 곧 한도를 다시 깎는다는 사정은 app/rag/embedding.py와 같다.
RETRY_MAX = 3
RETRY_CAP_S = 60.0

# 지수 응답에는 코드가 없고 IDX_NM만 온다. 같은 페이로드에 "코스피 200",
# "코스피 100", 업종지수, 그리고 "코스피"로 시작하는 변형이 함께 실려 오므로
# 접두사 매칭은 반드시 엉뚱한 행을 집는다. 정확히 일치할 때만 받는다.
# 여기서 정한 코드로 리스크 엔진이 벤치마크를 찾는다. index_daily 모델 주석의
# 예시("KOSPI, KOSDAQ")를 그대로 따른다.
INDEX_CODES = {"코스피": "KOSPI"}


# ── 파싱 ──────────────────────────────────────────────────────────────────────
def _yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _parse_date(value: str) -> date:
    return datetime.strptime(str(value).strip(), "%Y%m%d").date()


def _num(value: object) -> float | None:
    """KRX는 숫자를 문자열로 준다. 자리수 쉼표가 붙고, 결측은 "-"나 빈 문자열이다."""
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _int(value: object) -> int | None:
    """시가총액은 1.5경까지 간다. float를 거치면 정밀도가 위태로우니 바로 정수로."""
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _retry_after(headers: httpx.Headers) -> float:
    """Retry-After(초). 없거나 해석 불가면 1초, 상한 60초."""
    return min(max(_num(headers.get("retry-after")) or 1.0, 1.0), RETRY_CAP_S)


def _index_rows(payload: list[dict]) -> list[dict]:
    """지수 응답에서 벤치마크만 골라 upsert 행으로 바꾼다.

    index_daily는 종가만 저장한다. 나머지 컬럼은 버린다.
    같은 이름이 두 번 오더라도 (index_code, trade_date)는 한 행만 남긴다 —
    한 statement 안에 중복 키가 들어가면 upsert가 통째로 실패한다.
    """
    rows: dict[tuple[str, date], dict] = {}
    for r in payload:
        code = INDEX_CODES.get(str(r.get("IDX_NM", "")).strip())
        close = _num(r.get("CLSPRC_IDX"))
        if code is None or close is None or close <= 0:
            continue
        key = (code, _parse_date(r["BAS_DD"]))
        rows.setdefault(
            key, {"index_code": key[0], "trade_date": key[1], "close": close}
        )
    return list(rows.values())


def _marketcap_updates(payload: list[dict]) -> dict[str, dict]:
    """종목 응답을 ticker → UPDATE 값으로 바꾼다. ISU_CD가 종목코드다."""
    out: dict[str, dict] = {}
    for r in payload:
        ticker = str(r.get("ISU_CD", "")).strip()
        cap = _int(r.get("MKTCAP"))
        if len(ticker) != 6 or cap is None:
            continue
        out[ticker] = {
            "ticker": ticker,
            "market_cap": cap,
            "listed_shares": _int(r.get("LIST_SHRS")),
        }
    return out


# ── HTTP ─────────────────────────────────────────────────────────────────────
async def _fetch(client: httpx.AsyncClient, path: str, day: date) -> list[dict]:
    """하루치를 받는다.

    휴장일은 오류가 아니라 HTTP 200 + 빈 OutBlock_1로 온다. 빈 리스트로 넘긴다.
    키는 AUTH_KEY 헤더로만 실는다. 쿼리에 넣으면 httpx 로그에 URL이 통째로 남는다.
    """
    for attempt in range(1, RETRY_MAX + 1):
        res = await client.get(
            f"{BASE_URL}{path}",
            params={"basDd": _yyyymmdd(day)},
            headers={"AUTH_KEY": settings.krx_api_key},
        )
        if res.status_code != 429 or attempt == RETRY_MAX:
            break
        wait = _retry_after(res.headers)
        logger.warning("429 — %.0f초 대기 후 재시도 (%d/%d)", wait, attempt, RETRY_MAX)
        await asyncio.sleep(wait)

    res.raise_for_status()
    return res.json().get("OutBlock_1") or []


# ── 지수 시계열 ────────────────────────────────────────────────────────────────
async def _trading_days(
    session: AsyncSession, start: date, end: date
) -> list[date]:
    """구간의 개장일을 price_daily에서 가져온다.

    개장일 달력이 없으니 달력 전체를 훑고 휴장일을 버려도 되지만, price_daily에
    바로 이 구간의 실제 개장일이 이미 들어 있다. 2년이면 731일 대신 485번만
    호출하면 되고, 개장일 판정도 추측이 아니라 데이터다.
    """
    stmt = (
        select(PriceDaily.trade_date)
        .where(PriceDaily.trade_date.between(start, end))
        .distinct()
        .order_by(PriceDaily.trade_date)
    )
    return list((await session.scalars(stmt)).all())


async def _upsert_index(session: AsyncSession, rows: list[dict]) -> int:
    if not rows:
        return 0
    stmt = pg_insert(IndexDaily).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["index_code", "trade_date"],
        set_={"close": stmt.excluded.close},
    )
    await session.execute(stmt)
    return len(rows)


async def ingest_index(*, days: int, full: bool = False) -> dict[str, int]:
    """벤치마크 지수 시계열을 적재하고 요약을 돌려준다.

    full=False면 index_daily에 이미 있는 날짜는 건너뛴다. 매일 cron으로 돌려도
    호출이 하루치뿐이다.
    """
    end = date.today()
    start = end - timedelta(days=days)
    stats = {"days": 0, "rows": 0, "holidays": 0, "failed": 0}

    async with SessionFactory() as session:
        targets = await _trading_days(session, start, end)
        if not targets:
            logger.warning(
                "구간에 개장일이 없다. 시세를 먼저 적재할 것: python -m ingest.prices"
            )
            return stats

        if not full:
            done = set(
                (
                    await session.scalars(
                        select(IndexDaily.trade_date).where(
                            IndexDaily.index_code.in_(INDEX_CODES.values()),
                            IndexDaily.trade_date.between(start, end),
                        )
                    )
                ).all()
            )
            targets = [d for d in targets if d not in done]

        if not targets:
            logger.info("이미 최신이다 — 받을 날짜가 없다")
            return stats

        logger.info("대상 %d영업일 · %s ~ %s", len(targets), targets[0], targets[-1])

        rows: list[dict] = []
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            for i, day in enumerate(targets, 1):
                try:
                    day_rows = _index_rows(await _fetch(client, INDEX_PATH, day))
                except Exception:
                    # 하루가 실패해도 멈추지 않는다. 재실행하면 그날만 다시 받는다.
                    stats["failed"] += 1
                    logger.exception("지수 조회 실패: %s", day)
                    continue

                if not day_rows:
                    stats["holidays"] += 1
                    continue
                rows += day_rows
                stats["days"] += 1

                if i % 100 == 0:
                    logger.info("진행 %d/%d · 누적 %d행", i, len(targets), len(rows))

        stats["rows"] = await _upsert_index(session, rows)
        await session.commit()

    return stats


# ── 시가총액 스냅샷 ─────────────────────────────────────────────────────────────
async def ingest_marketcap() -> dict[str, int]:
    """최신 개장일의 시가총액·상장주식수로 instruments를 채운다.

    시가총액은 시계열이 아니라 스냅샷이다. §3.5는 현재값만 쓰므로 485일이 아니라
    두 번(코스피·코스닥) 호출하면 끝난다.

    기준일은 date.today()가 아니라 데이터에서 가져온다. 주말이나 휴장일에 돌리면
    빈 응답을 받고 아무것도 쓰지 않기 때문이다. price_daily의 마지막 개장일부터
    거슬러 올라가며 응답이 있는 날을 찾는다.
    """
    stats = {"fetched": 0, "updated": 0, "missing": 0}

    async with SessionFactory() as session:
        candidates = list(reversed(await _trading_days(session, date.min, date.today())))
        if not candidates:
            logger.warning(
                "개장일을 모른다. 시세를 먼저 적재할 것: python -m ingest.prices"
            )
            return stats

        updates: dict[str, dict] = {}
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            # 마지막 개장일이 아직 마감 전일 수 있다. 최대 5일까지 거슬러 본다.
            for day in candidates[:5]:
                for path in STOCK_PATHS:
                    updates |= _marketcap_updates(await _fetch(client, path, day))
                if updates:
                    logger.info("기준일 %s · 응답 %d종목", day, len(updates))
                    break

        if not updates:
            logger.error("최근 개장일 어디에도 응답이 없다 — 적재를 중단한다")
            return stats

        stats["fetched"] = len(updates)
        # ticker가 PK이므로 마스터에 없는 종목은 0행 갱신으로 조용히 지나간다.
        await session.execute(update(Instrument), list(updates.values()))
        await session.commit()

        stats["updated"] = await session.scalar(
            select(func.count())
            .select_from(Instrument)
            .where(Instrument.market_cap.isnot(None))
        )
        stats["missing"] = await session.scalar(
            select(func.count())
            .select_from(Instrument)
            .where(Instrument.market_cap.is_(None))
        )

    return stats


# ── CLI ──────────────────────────────────────────────────────────────────────
async def _main() -> None:
    parser = argparse.ArgumentParser(description="KRX OpenAPI 적재")
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="벤치마크 지수 일별 시세")
    p_index.add_argument(
        "--days", type=int, default=730, help="오늘 기준 며칠 전부터 (기본 2년)"
    )
    p_index.add_argument(
        "--full", action="store_true", help="증분 무시하고 구간 전체 재적재"
    )

    sub.add_parser("marketcap", help="최신 개장일 시가총액 · 상장주식수 스냅샷")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s"
    )

    if not settings.krx_api_key:
        raise SystemExit("KRX_API_KEY가 비어 있다. .env를 확인할 것")

    try:
        if args.command == "index":
            stats = await ingest_index(days=args.days, full=args.full)
            logger.info(
                "완료 — %d영업일 / %d행 적재 · 휴장 %d · 실패 %d",
                stats["days"],
                stats["rows"],
                stats["holidays"],
                stats["failed"],
            )
        else:
            stats = await ingest_marketcap()
            logger.info(
                "완료 — 응답 %d종목 · market_cap 보유 %d종목 · 여전히 NULL %d종목",
                stats["fetched"],
                stats["updated"],
                stats["missing"],
            )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
