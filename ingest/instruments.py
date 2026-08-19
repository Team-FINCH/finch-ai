"""종목 마스터 적재.

KOSPI · KOSDAQ 상장 종목의 기본 정보를 DART에서 모은다. corpCode.xml로 종목 목록과
고유번호를 얻고, 기업개황으로 시장 구분과 업종을 채운다.

KRX의 마스터 계열 엔드포인트(종목 목록·시가총액·업종분류)는 로그인 없이 빈 응답이나
오류를 돌려준다. 시세(OHLCV)만 살아 있어 마스터는 DART로 조달한다.

시가총액과 상장주식수는 DART가 주지 않으므로 비워 둔다. 엔진 산식 §3.5의
대형/중소형 판정에서 쓰이는 Phase 2 항목이다.

    python -m ingest.instruments
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import time
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from xml.etree import ElementTree

import httpx
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import Insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.core.db import SessionFactory
from app.core.enums import InstrumentStatus, Market
from app.core.models import Instrument
from ingest.sectors import resolve

log = logging.getLogger("ingest.instruments")

CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
CORP_CODE_CACHE = Path("data/corpCode.xml")
MARKETS: tuple[Market, ...] = (Market.KOSPI, Market.KOSDAQ)
UPSERT_CHUNK = 500
HTTP_TIMEOUT = 30.0

# NULL이 들어와도 기존 값을 지우지 않는 컬럼. 어느 단계든 건너뛸 수 있으므로
# (DART 키 없음, KRX 업종분류 실패) 부분 실패가 적재된 값을 덮어쓰면 안 된다.
PRESERVING_COLUMNS = ("sector", "sector_code", "corp_code", "market_cap", "listed_shares")


class CorpCodeError(RuntimeError):
    """DART corpCode.xml을 받지 못했거나 해석할 수 없을 때."""


@dataclass(slots=True)
class InstrumentRow:
    """instruments 한 행. 컬럼 이름을 그대로 쓴다."""

    ticker: str
    name: str
    market: str
    sector: str | None = None
    sector_code: str | None = None
    corp_code: str | None = None
    market_cap: int | None = None
    listed_shares: int | None = None
    status: str = InstrumentStatus.LISTED


# ── 정규화 ───────────────────────────────────────────────
def normalize_ticker(value: object) -> str:
    """종목코드를 6자리 문자열로 만든다.

    pandas는 숫자로만 이뤄진 인덱스를 int로 읽어버린다. "005930"이 5930이 되면
    모델의 char_length(ticker) = 6 제약에 걸려 적재 전체가 실패한다.
    """
    raw = str(value).strip() if value is not None else ""
    ticker = raw.zfill(6)
    # 빈 값을 zfill 하면 "000000"이 되어 조용히 통과한다. 먼저 걸러낸다.
    if not raw or len(ticker) != 6 or not ticker.isdigit():
        raise ValueError(f"종목코드가 6자리 숫자가 아니다: {value!r}")
    return ticker


def _to_int(value: object) -> int | None:
    """결측·빈 문자열을 None으로 돌린다. NaN은 int()에서 예외가 난다."""
    if value is None:
        return None
    try:
        number = int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None
    return number


def _clean(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


# ── KRX 수집 ─────────────────────────────────────────────
DART_COMPANY_URL = "https://opendart.fss.or.kr/api/company.json"

# 기업개황의 corp_cls. E(기타)와 N(코넥스)는 현재 상장이 아니다.
# corpCode.xml의 stock_code 보유 법인 3,984개에는 폐지 종목이 섞여 있어
# 이 값으로 걸러야 한다.
CORP_CLS_TO_MARKET = {"Y": Market.KOSPI, "K": Market.KOSDAQ}

# DART 일일 한도는 20,000회. 상장사 전체가 4천 건이라 한도는 넉넉하지만
# 짧은 간격으로 몰아치면 거부당한다.
COMPANY_DELAY_S = 0.12


def build_row(ticker: str, payload: dict) -> InstrumentRow | None:
    """기업개황 한 건을 행으로 만든다. 현재 상장이 아니면 None.

    market_cap과 listed_shares는 DART가 주지 않으므로 비워 둔다.
    엔진 산식 §3.5(대형/중소형 판정)에서 쓰이는 Phase 2 항목이다.
    """
    market = CORP_CLS_TO_MARKET.get(_clean(payload.get("corp_cls")) or "")
    if market is None:
        return None

    induty = _clean(payload.get("induty_code"))
    sector, _ = resolve(induty, ticker)
    name = _clean(payload.get("stock_name")) or _clean(payload.get("corp_name"))
    if not name:
        return None

    return InstrumentRow(
        ticker=ticker,
        name=name,
        market=market,
        sector=sector,
        sector_code=induty,
    )


def fetch_company(client: httpx.Client, api_key: str, corp_code: str) -> dict | None:
    """기업개황 한 건. 실패하면 None을 돌려주고 그 종목만 건너뛴다."""
    try:
        res = client.get(
            DART_COMPANY_URL,
            params={"crtfc_key": api_key, "corp_code": corp_code},
            timeout=HTTP_TIMEOUT,
        )
        payload = res.json()
    except (httpx.HTTPError, ValueError):
        # 한 종목이 실패해도 4천 건짜리 배치를 멈추지 않는다.
        # ValueError는 DART가 JSON 대신 오류 문서를 돌려줄 때 난다.
        log.warning("기업개황 조회 실패: corp_code=%s", corp_code)
        return None

    if payload.get("status") != "000":
        log.debug(
            "기업개황 응답 거절: corp_code=%s status=%s", corp_code, payload.get("status")
        )
        return None
    return payload


def collect_instruments(
    client: httpx.Client, api_key: str, mapping: dict[str, str]
) -> list[InstrumentRow]:
    """corpCode 매핑을 돌며 기업개황으로 시장·업종을 채운다.

    종목 목록 자체를 DART에서 얻는다. KRX의 마스터 계열 엔드포인트는 로그인 없이
    빈 응답을 돌려주므로 시세(OHLCV) 외에는 기대할 수 없다.
    """
    rows: list[InstrumentRow] = []
    skipped = 0
    for i, (ticker, corp_code) in enumerate(sorted(mapping.items()), 1):
        payload = fetch_company(client, api_key, corp_code)
        if payload is None:
            skipped += 1
        else:
            row = build_row(ticker, payload)
            if row is None:
                skipped += 1
            else:
                rows.append(row)
        if i % 500 == 0:
            log.info("기업개황 %d/%d · 상장 %d건", i, len(mapping), len(rows))
        time.sleep(COMPANY_DELAY_S)

    log.info("현재 상장 %d건 · 제외 %d건", len(rows), skipped)
    return rows


# ── DART 고유번호 ────────────────────────────────────────
def extract_corp_code_xml(payload: bytes) -> bytes:
    """corpCode.xml 응답(zip)에서 XML 본문을 꺼낸다.

    DART는 zip으로 내려주고, 오류일 때만 XML을 그대로 준다. 압축을 풀지 않고
    파싱하면 원인을 알 수 없는 실패가 된다.
    """
    if not zipfile.is_zipfile(io.BytesIO(payload)):
        raise CorpCodeError(f"zip이 아니다. DART 응답: {_dart_error(payload)}")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".xml")]
        if not names:
            raise CorpCodeError(f"zip 안에 XML이 없다: {archive.namelist()}")
        return archive.read(names[0])


def _dart_error(payload: bytes) -> str:
    try:
        root = ElementTree.fromstring(payload.decode("utf-8", "replace"))
    except ElementTree.ParseError:
        return payload[:120].decode("utf-8", "replace")
    status = root.findtext("status") or "?"
    message = root.findtext("message") or "?"
    return f"status={status} message={message}"


def parse_corp_codes(xml_bytes: bytes) -> dict[str, str]:
    """stock_code → corp_code 매핑.

    반드시 stock_code로만 잇는다. corp_name은 읽지도 않는다 — 같은 이름의
    법인이 실제로 존재해서 종목명으로 맞추면 엉뚱한 고유번호가 붙는다.
    """
    root = ElementTree.fromstring(xml_bytes)
    mapping: dict[str, str] = {}
    for node in root.iter("list"):
        stock_code = (node.findtext("stock_code") or "").strip()
        corp_code = (node.findtext("corp_code") or "").strip()
        if not stock_code or not corp_code:
            continue  # 비상장 법인은 stock_code가 공백이다
        try:
            mapping[normalize_ticker(stock_code)] = corp_code.zfill(8)
        except ValueError:
            log.debug("종목코드 형식이 아니라 건너뛴다: %r", stock_code)
    return mapping


def fetch_corp_codes(api_key: str, client: httpx.Client | None = None) -> dict[str, str]:
    """DART에서 corpCode.xml을 받아 매핑을 만든다."""
    owned = client is None
    client = client or httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True)
    try:
        response = client.get(CORP_CODE_URL, params={"crtfc_key": api_key})
        response.raise_for_status()
        payload = response.content
    finally:
        if owned:
            client.close()

    xml_bytes = extract_corp_code_xml(payload)
    try:
        CORP_CODE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        CORP_CODE_CACHE.write_bytes(xml_bytes)  # data/ 는 .gitignore 대상이다
    except OSError as exc:
        log.debug("corpCode.xml 캐시를 남기지 못했다: %s", exc)
    return parse_corp_codes(xml_bytes)


def load_corp_codes(client: httpx.Client | None = None) -> dict[str, str]:
    """키가 없거나 수집에 실패해도 빈 매핑을 돌려주고 적재를 계속한다."""
    api_key = (settings.dart_api_key or "").strip()
    if not api_key:
        log.warning(
            "DART_API_KEY가 없어 corp_code 단계를 건너뛴다. "
            "종목 마스터는 적재되지만 공시 조회는 키를 넣고 재실행해야 한다."
        )
        return {}
    try:
        mapping = fetch_corp_codes(api_key, client)
    except (CorpCodeError, httpx.HTTPError) as exc:
        log.error("corpCode.xml 수집 실패. corp_code 없이 진행한다: %s", exc)
        return {}
    log.info("DART 고유번호 %d건 확보", len(mapping))
    return mapping


def attach_corp_codes(rows: Sequence[InstrumentRow], mapping: dict[str, str]) -> int:
    """종목코드가 정확히 일치하는 행에만 고유번호를 붙인다."""
    matched = 0
    for row in rows:
        corp_code = mapping.get(row.ticker)
        if corp_code:
            row.corp_code = corp_code
            matched += 1
    if mapping:
        log.info("corp_code %d/%d 매칭", matched, len(rows))
    return matched


# ── 적재 ─────────────────────────────────────────────────
def _chunks(rows: Sequence[InstrumentRow], size: int) -> Iterator[Sequence[InstrumentRow]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def build_upsert_statement(rows: Sequence[InstrumentRow]) -> Insert:
    """ticker 충돌 시 갱신하는 INSERT를 만든다. 재실행해도 중복이 생기지 않는다."""
    statement = pg_insert(Instrument).values([asdict(row) for row in rows])
    updates = {
        "name": statement.excluded.name,
        "market": statement.excluded.market,
        "status": statement.excluded.status,
        "updated_at": func.now(),
    }
    for column in PRESERVING_COLUMNS:
        updates[column] = func.coalesce(
            getattr(statement.excluded, column), getattr(Instrument, column)
        )
    return statement.on_conflict_do_update(index_elements=["ticker"], set_=updates)


async def upsert(rows: Sequence[InstrumentRow]) -> int:
    if not rows:
        return 0
    async with SessionFactory() as session:
        for chunk in _chunks(rows, UPSERT_CHUNK):
            await session.execute(build_upsert_statement(chunk))
        await session.commit()
    return len(rows)


# ── 진입점 ───────────────────────────────────────────────
async def run() -> int:
    """corpCode → 기업개황 → upsert. 적재한 종목 수를 돌려준다."""
    with httpx.Client() as client:
        mapping = load_corp_codes(client)
        if not mapping:
            log.error(
                "corpCode 매핑이 비었다. DART_API_KEY 없이는 종목 목록을 얻을 수 없다."
            )
            return 0
        log.info("corpCode 상장 후보 %d건 — 기업개황으로 시장·업종을 채운다", len(mapping))
        rows = collect_instruments(client, settings.dart_api_key, mapping)

    if not rows:
        log.error("현재 상장 종목이 하나도 없다. DART 응답을 확인할 것.")
        return 0

    attach_corp_codes(rows, mapping)
    count = await upsert(rows)
    log.info("종목 마스터 %d건 적재 완료", count)
    return count


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="종목 마스터 적재기")
    parser.add_argument("--verbose", action="store_true", help="DEBUG 로그")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    return 0 if asyncio.run(run()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
