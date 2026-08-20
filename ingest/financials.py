"""DART 재무제표 적재 — 사업보고서 자본총계.

§3.5 성장 판정이 PBR = 시가총액 / 자본총계인데, 시가총액은 `instruments`에 이미
있고 자본총계만 없었다. 그 하나를 `financial_annual`에 채운다.

    python -m ingest.financials                  # 기본 회계연도 전 종목
    python -m ingest.financials --year 2024
    python -m ingest.financials --limit 20       # 연결 확인용 맛보기

조회 축은 종목이다. `fnlttSinglAcnt.json`은 corp_code 하나당 한 번 호출해야 하므로
`ingest/krx.py`처럼 하루에 전 종목이 딸려오지 않는다. 2,596종목이면 2,596번이다.

## 연결(CFS) → 별도(OFS) 폴백

같은 보고서에 자본총계가 두 번 나온다. 삼성전자 2025년은 연결 436조, 별도 254조로
1.7배 차이다. **연결을 먼저 쓰고, 연결이 없을 때만 별도를 쓴다.** PBR의 시장 관행이
연결이고, 지주회사처럼 별도만 제출하는 곳이 있어 연결만 고집하면 통째로 빠진다.
어느 쪽을 썼는지는 `financial_annual.fs_div`에 남는다 — 기준이 섞인 채 백분위를
매기면 순위가 무의미해지므로, 사후에 확인할 수 있어야 한다.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import SessionFactory, engine
from app.core.models import FinancialAnnual, Instrument

logger = logging.getLogger("ingest.financials")

URL = "https://opendart.fss.or.kr/api/fnlttSinglAcnt.json"
REPRT_CODE = "11011"  # 사업보고서. 분기·반기 보고서는 자본총계 기준이 달라 섞지 않는다.
ACCOUNT_NM = "자본총계"

# 연결을 먼저 본다. 이 순서가 곧 위 docstring의 폴백 규칙이다.
FS_PRIORITY = ("CFS", "OFS")

HTTP_TIMEOUT = 30.0
# 호출 간 간격. `ingest/instruments.py`가 기업개황 4천 건을 0.12초 간격으로 도는데
# 같은 DART 호스트라 같은 값을 쓴다. 2,596종목이면 5분 남짓이다.
DELAY_S = 0.12
RETRY_MAX = 3
RETRY_CAP_S = 60.0
# 사용한도 초과. HTTP는 200인 채 status만 바뀌어 오므로 status_code로는 안 잡힌다.
STATUS_THROTTLED = "020"
STATUS_OK = "000"
UPSERT_CHUNK = 500


# ── 파싱 ──────────────────────────────────────────────────────────────────────
def parse_amount(value: object) -> int | None:
    """`thstrm_amount`를 정수로. 자리수 쉼표가 붙고, 결측은 ""나 "-"로 온다.

    적자·자본잠식이면 "-1,234"나 "△1,234"(회계 관행)로 오므로 부호를 살린다.
    한 종목의 이상한 값 때문에 2,596건짜리 적재가 통째로 죽지 않도록 None으로 흘린다.
    """
    text = str(value or "").replace(",", "").replace("△", "-").strip()
    if not text or text == "-":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def pick_equity(rows: list[dict]) -> tuple[int, str] | None:
    """자본총계 행들에서 `(자본총계, fs_div)` 하나를 고른다. 없으면 None.

    연결이 있으면 연결, 없으면 별도다(모듈 docstring의 폴백 규칙). 같은 fs_div가
    여러 번 오면 먼저 온 것을 쓴다 — 사업보고서 한 건에서 자본총계가 재무상태표
    말고 다른 재무제표(sj_div)로 중복되어 실려 오는 경우가 있다.
    """
    found: dict[str, int] = {}
    for row in rows:
        if str(row.get("account_nm", "")).strip() != ACCOUNT_NM:
            continue
        amount = parse_amount(row.get("thstrm_amount"))
        fs_div = str(row.get("fs_div", "")).strip().upper()
        if amount is None or fs_div not in FS_PRIORITY:
            continue
        found.setdefault(fs_div, amount)

    for fs_div in FS_PRIORITY:
        if fs_div in found:
            return found[fs_div], fs_div
    return None


def default_year(today: date) -> int:
    """기본 회계연도. 사업보고서는 결산 후 90일 안에 제출되므로 3월 말에 몰린다.

    4월 전에는 작년 보고서가 아직 없어 재작년을 본다. 4월 이후에도 제출이 늦는
    회사가 있지만 그건 "없는 종목"으로 세면 되고, 기본값까지 한 해 물릴 일은 아니다.
    """
    return today.year - 1 if today.month >= 4 else today.year - 2


def _retry_after(headers: httpx.Headers) -> float:
    """Retry-After(초). 없거나 해석 불가면 1초, 상한 60초."""
    try:
        seconds = float(str(headers.get("retry-after", "")).strip())
    except ValueError:
        seconds = 1.0
    return min(max(seconds, 1.0), RETRY_CAP_S)


# ── HTTP ─────────────────────────────────────────────────────────────────────
async def fetch(
    client: httpx.AsyncClient, corp_code: str, year: int
) -> tuple[list[dict], str]:
    """한 종목의 사업보고서 재무제표를 받는다. 반환은 `(행들, status)`.

    DART는 사용한도를 넘겨도 HTTP 200에 `status: "020"`으로 답한다. 그래서 재시도
    판정을 status_code만으로 하면 429를 한 번도 못 보고 한도만 계속 깎는다
    (`app/rag/embedding.py`가 429 폭주로 하루치 쿼터를 태운 것과 같은 사정이다).
    두 신호를 함께 본다.

    조회 결과가 없는 종목("013")은 오류가 아니다. 지주회사·리츠·신규상장·관리종목이
    여기 걸리며, 세기만 하고 넘어간다.
    """
    for attempt in range(1, RETRY_MAX + 1):
        res = await client.get(
            URL,
            params={
                "crtfc_key": settings.dart_api_key,
                "corp_code": corp_code,
                "bsns_year": str(year),
                "reprt_code": REPRT_CODE,
            },
        )
        payload = res.json() if res.status_code == 200 else {}
        status = str(payload.get("status", res.status_code))
        if res.status_code != 429 and status != STATUS_THROTTLED:
            return (payload.get("list") or []), status
        if attempt == RETRY_MAX:
            return [], status
        wait = _retry_after(res.headers)
        logger.warning(
            "한도 초과(status=%s) — %.0f초 대기 후 재시도 (%d/%d)",
            status,
            wait,
            attempt,
            RETRY_MAX,
        )
        await asyncio.sleep(wait)
    return [], ""


# ── 적재 ─────────────────────────────────────────────────────────────────────
async def _targets(session: AsyncSession, limit: int | None) -> list[tuple[str, str]]:
    """`(ticker, corp_code)` 목록. corp_code가 없는 종목은 DART에 물어볼 수 없다.

    시가총액이 큰 순으로 돈다. 중간에 끊겨도 §3.5 백분위에서 비중이 큰 쪽이
    먼저 채워지고, `--limit` 맛보기도 아는 종목부터 나온다.
    """
    stmt = (
        select(Instrument.ticker, Instrument.corp_code)
        .where(Instrument.corp_code.isnot(None))
        .order_by(Instrument.market_cap.desc().nullslast())
    )
    if limit:
        stmt = stmt.limit(limit)
    return [(t, c) for t, c in (await session.execute(stmt)).all()]


async def _upsert(session: AsyncSession, rows: list[dict]) -> int:
    """같은 (ticker, fiscal_year)를 다시 받으면 덮어쓴다. 재실행이 안전해야 한다."""
    written = 0
    for start in range(0, len(rows), UPSERT_CHUNK):
        chunk = rows[start : start + UPSERT_CHUNK]
        stmt = pg_insert(FinancialAnnual).values(chunk)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["ticker", "fiscal_year"],
                set_={
                    "total_equity": stmt.excluded.total_equity,
                    "fs_div": stmt.excluded.fs_div,
                    "updated_at": func.now(),
                },
            )
        )
        written += len(chunk)
    return written


async def ingest(*, year: int, limit: int | None = None) -> dict[str, int]:
    """사업보고서 자본총계를 전 종목 적재하고 요약을 돌려준다.

    한 종목이 실패해도 멈추지 않는다. 미제출·비상장전환 등으로 응답이 없는 종목이
    정상적으로 존재하고, 그중 하나 때문에 나머지 2천여 건을 다시 받는 것은 한도
    낭비다. 실패는 세어서 마지막에 보고한다.
    """
    stats = {"targets": 0, "rows": 0, "no_data": 0, "no_equity": 0, "failed": 0}

    async with SessionFactory() as session:
        targets = await _targets(session, limit)
        stats["targets"] = len(targets)
        if not targets:
            logger.warning(
                "corp_code를 가진 종목이 없다. 먼저 적재할 것: python -m ingest.instruments"
            )
            return stats

        logger.info("대상 %d종목 · %d년 사업보고서", len(targets), year)

        rows: list[dict] = []
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            for i, (ticker, corp_code) in enumerate(targets, 1):
                try:
                    payload, status = await fetch(client, corp_code, year)
                except Exception:
                    stats["failed"] += 1
                    logger.exception("재무제표 조회 실패: %s (%s)", ticker, corp_code)
                    continue

                if status != STATUS_OK or not payload:
                    # "013"(조회 데이터 없음)이 대부분이다. 오류가 아니라 미제출이다.
                    stats["no_data"] += 1
                elif (picked := pick_equity(payload)) is None:
                    # 응답은 왔는데 자본총계 행이 없다. 금융업 등 계정 구성이 다른 경우.
                    stats["no_equity"] += 1
                    logger.debug("자본총계 없음: %s (%s)", ticker, corp_code)
                else:
                    equity, fs_div = picked
                    rows.append(
                        {
                            "ticker": ticker,
                            "fiscal_year": year,
                            "total_equity": equity,
                            "fs_div": fs_div,
                        }
                    )

                if i % 200 == 0:
                    logger.info("진행 %d/%d · 누적 %d행", i, len(targets), len(rows))
                await asyncio.sleep(DELAY_S)

        stats["rows"] = await _upsert(session, rows)
        await session.commit()

    return stats


# ── CLI ──────────────────────────────────────────────────────────────────────
async def _main() -> None:
    parser = argparse.ArgumentParser(description="DART 사업보고서 자본총계 적재")
    parser.add_argument(
        "--year", type=int, default=None, help="회계연도 (기본: 직전 사업연도)"
    )
    parser.add_argument("--limit", type=int, default=None, help="상위 N종목만 (연결 확인용)")
    parser.add_argument("--verbose", action="store_true", help="DEBUG 로그")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
    )
    # httpx는 요청 URL을 통째로 INFO로 남긴다. DART는 키를 쿼리로만 받으므로
    # (KRX처럼 헤더에 실을 수 없다) 그대로 두면 crtfc_key가 로그에 찍힌다.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if not settings.dart_api_key:
        raise SystemExit("DART_API_KEY가 비어 있다. .env를 확인할 것")

    year = args.year or default_year(date.today())
    try:
        stats = await ingest(year=year, limit=args.limit)
        logger.info(
            "완료 — 대상 %d종목 / %d행 적재 · 미제출 %d · 자본총계 없음 %d · 실패 %d",
            stats["targets"],
            stats["rows"],
            stats["no_data"],
            stats["no_equity"],
            stats["failed"],
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
