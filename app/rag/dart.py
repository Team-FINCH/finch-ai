"""DART 공시 수집·적재.

공시목록을 기간으로 훑고, 원문 zip을 풀어 본문을 뽑아 documents에 넣는다.
본문은 청킹해 document_chunks에 넣되 embedding은 NULL로 둔다 (제공자 미정).

증분이다. 이미 있는 rcept_no는 원문을 받지 않는다. 한 종목이 실패해도 나머지는 계속한다.

    python -m app.rag.dart --days 30 --limit 5
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import re
import time
import zipfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from xml.etree import ElementTree

import httpx
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.core.db import SessionFactory
from app.core.enums import DocumentType
from app.core.models import Document, DocumentChunk, Instrument
from app.rag.chunking import chunk
from app.rag.lexical import weighted_tsvector
from ingest.universe import report_resolution, target_tickers

log = logging.getLogger("app.rag.dart")

LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DOCUMENT_URL = "https://opendart.fss.or.kr/api/document.xml"
VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

SOURCE = "DART"
HTTP_TIMEOUT = 30.0
PAGE_COUNT = 100
PAGE_LIMIT = 50  # 페이지 무한 순회 방지. 100건씩 5,000건이면 한 종목으로 충분하다.
KST = timezone(timedelta(hours=9))

# DART 일일 한도는 20,000회. 목록 1회 + 원문 N회라 종목당 수십 건씩 나간다.
REQUEST_DELAY_S = 0.15

_TAGS = re.compile(r"<[^>]+>")
_NOISE = re.compile(r"<(STYLE|SCRIPT)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)


class DartError(RuntimeError):
    """DART 응답을 해석할 수 없을 때."""


class DartStatusError(DartError):
    """A target was rejected; retain its status for batch-level diagnosis."""

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(f"DART status={status}")


class SystemicDartError(DartError):
    """Every attempted target received the same rejection."""


def check_systemic_failure(failures: Counter[str], attempted: int) -> None:
    if attempted and len(failures) == 1 and sum(failures.values()) == attempted:
        status = next(iter(failures))
        raise SystemicDartError(
            f"DART systemic failure: {attempted}/{attempted} targets rejected "
            f"with status={status}; check API credentials or quota"
        )


@dataclass(slots=True)
class Filing:
    """공시목록 한 건."""

    rcept_no: str
    ticker: str
    corp_name: str
    report_nm: str
    rcept_dt: str
    #: DART 공시 유형 코드. A 정기 · B 주요사항 · I 거래소 · D 지분 … (목록 API `pblntf_ty`)
    pblntf_ty: str = ""

    @property
    def published_at(self) -> datetime | None:
        try:
            return datetime.strptime(self.rcept_dt, "%Y%m%d").replace(tzinfo=KST)
        except ValueError:
            # 접수일자가 비었거나 형식이 다르면 날짜 없이 저장한다. 원문은 여전히 근거가 된다.
            return None

    @property
    def url(self) -> str:
        return VIEWER_URL.format(rcept_no=self.rcept_no)


#: DART 공시 유형 (목록 API `pblntf_ty`). 이벤트 표와 원문이 다른 범위를 받는다 (#98).
#:   A 정기공시 · B 주요사항보고 · C 발행 · D 지분 · E 기타 · F 외부감사 · G 펀드 · H 유동화
#:   I 거래소공시 · J 공정위
#: 이벤트 표: 정기·주요사항·거래소·지분·감사. 원문: 정기·주요사항·거래소만. C·E·G·H·J 는
#: 대기업의 공정위 계열회사 변동·임원 소유상황 같은 노이즈라 받지 않는다.
EVENT_TYPES: tuple[str, ...] = ("A", "B", "I", "D", "F")
DOCUMENT_TYPES: tuple[str, ...] = ("A", "B", "I")
#: 정기공시 원문은 종류별 최신 1건만 받는다. 지난 분기 보고서는 이번 것에 포함돼 겹치고,
#: 1년치 전문은 종목 37개 기준 청크가 수십만 개라 임베딩이 며칠 걸린다.
PERIODIC_KINDS: tuple[str, ...] = ("사업보고서", "반기보고서", "분기보고서")


# ── 수집 ───────────────────────────────────────────────────────


def fetch_filing_list(
    client: httpx.Client,
    api_key: str,
    corp_code: str,
    ticker: str,
    bgn_de: str,
    end_de: str,
    types: Sequence[str] | None = None,
) -> list[Filing]:
    """한 종목의 공시목록. 페이징을 끝까지 따라간다.

    `types` 를 주면 유형마다 따로 조회한다 — 목록 API 는 `pblntf_ty` 를 하나만 받는다.
    API 거절은 DartStatusError로 전달해 호출자가 전 종목 실패를 판별한다.
    """
    filings: list[Filing] = []
    for pblntf_ty in types or (None,):
        filings.extend(
            _fetch_filing_pages(client, api_key, corp_code, ticker, bgn_de, end_de, pblntf_ty)
        )
    return filings


def _fetch_filing_pages(
    client: httpx.Client,
    api_key: str,
    corp_code: str,
    ticker: str,
    bgn_de: str,
    end_de: str,
    pblntf_ty: str | None,
) -> list[Filing]:
    filings: list[Filing] = []
    for page_no in range(1, PAGE_LIMIT + 1):
        try:
            res = client.get(
                LIST_URL,
                params={
                    "crtfc_key": api_key,
                    "corp_code": corp_code,
                    "bgn_de": bgn_de,
                    "end_de": end_de,
                    "page_count": PAGE_COUNT,
                    "page_no": page_no,
                    # 정정공시는 최종본만. 원본·정정본이 둘 다 오면 같은 내용이 두 번 적재된다.
                    "last_reprt_at": "Y",
                    **({"pblntf_ty": pblntf_ty} if pblntf_ty else {}),
                },
                timeout=HTTP_TIMEOUT,
            )
            payload = res.json()
        except (httpx.HTTPError, ValueError):
            # ValueError는 DART가 JSON 대신 오류 문서를 돌려줄 때 난다.
            log.warning("공시목록 조회 실패: ticker=%s page=%d", ticker, page_no)
            break

        status = payload.get("status")
        if status == "013":  # 조회된 데이터 없음. 오류가 아니다.
            break
        if status != "000":
            log.warning("공시목록 응답 거절: ticker=%s status=%s", ticker, status)
            raise DartStatusError(str(status))

        for item in payload.get("list") or []:
            rcept_no = (item.get("rcept_no") or "").strip()
            if not rcept_no:
                continue
            filings.append(
                Filing(
                    rcept_no=rcept_no,
                    ticker=ticker,
                    corp_name=(item.get("corp_name") or "").strip(),
                    report_nm=(item.get("report_nm") or "").strip() or rcept_no,
                    rcept_dt=(item.get("rcept_dt") or "").strip(),
                    pblntf_ty=pblntf_ty or "",
                )
            )

        total_page = int(payload.get("total_page") or 1)
        if page_no >= total_page:
            break
        time.sleep(REQUEST_DELAY_S)

    return filings


def extract_body(payload: bytes, rcept_no: str) -> str:
    """원문 zip에서 본문 텍스트를 뽑는다.

    DART는 zip으로 내려주고 오류일 때만 XML을 그대로 준다. 원문은 SGML에 가까워
    파서가 거부하는 경우가 있어, 그때는 태그만 걷어내고 텍스트를 살린다.
    """
    if not zipfile.is_zipfile(io.BytesIO(payload)):
        raise DartError(f"zip이 아니다. DART 응답: {_dart_error(payload)}")

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        target = next((n for n in names if n.startswith(rcept_no)), None) or next(
            (n for n in names if n.lower().endswith(".xml")), None
        )
        if target is None:
            raise DartError(f"zip 안에 원문이 없다: {names}")
        raw = archive.read(target)

    text = _NOISE.sub(" ", _decode(raw))
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return _TAGS.sub(" ", text)
    return " ".join(root.itertext())


def _decode(raw: bytes) -> str:
    """DART 원문은 UTF-8이지만 옛 문서는 EUC-KR로 온다."""
    for encoding in ("utf-8", "cp949"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _dart_error(payload: bytes) -> str:
    try:
        root = ElementTree.fromstring(payload.decode("utf-8", "replace"))
    except ElementTree.ParseError:
        return payload[:120].decode("utf-8", "replace")
    status = root.findtext("status") or "?"
    message = root.findtext("message") or "?"
    return f"status={status} message={message}"


def fetch_document(client: httpx.Client, api_key: str, rcept_no: str) -> str | None:
    """공시 원문 본문. 실패하면 None을 돌려주고 그 건만 건너뛴다."""
    try:
        res = client.get(
            DOCUMENT_URL,
            params={"crtfc_key": api_key, "rcept_no": rcept_no},
            timeout=HTTP_TIMEOUT,
        )
        return extract_body(res.content, rcept_no)
    except (httpx.HTTPError, DartError, zipfile.BadZipFile) as exc:
        log.warning("공시 원문 실패: rcept_no=%s (%s)", rcept_no, exc)
        return None


# ── 적재 ───────────────────────────────────────────────────────


async def load_targets(
    limit: int | None = None, tickers: Sequence[str] | None = None
) -> list[tuple[str, str]]:
    """Return (ticker, corp_code) in config order; explicit tickers bypass limit."""
    targets = target_tickers(tickers, limit)
    async with SessionFactory() as session:
        stmt = select(Instrument.ticker, Instrument.corp_code).where(
            Instrument.corp_code.is_not(None), Instrument.ticker.in_(targets)
        )
        if not tickers:
            stmt = stmt.where(Instrument.status == "listed")
        codes = {row.ticker: row.corp_code for row in await session.execute(stmt)}
        report_resolution(log, targets, list(codes), explicit=bool(tickers))
        return [(ticker, codes[ticker]) for ticker in targets if ticker in codes]


def keep_latest_periodic(filings: Sequence[Filing]) -> list[Filing]:
    """정기공시(사업·반기·분기보고서)는 종류별 최신 1건만 남긴다. 나머지는 그대로."""
    latest: dict[str, Filing] = {}
    others: list[Filing] = []
    for filing in filings:
        kind = next((k for k in PERIODIC_KINDS if k in filing.report_nm), None)
        if kind is None:
            others.append(filing)
        elif kind not in latest or filing.rcept_dt > latest[kind].rcept_dt:
            latest[kind] = filing
    return others + list(latest.values())


async def tickers_without_documents(tickers: Sequence[str]) -> set[str]:
    """공시 원문이 한 건도 없는 종목. 첫 적재라 증분 창 대신 백필 창을 준다.

    시세(`ingest.prices --days 400`)와 같은 구조다. 백엔드에서 새로 산 종목이
    증분 7일만 받으면 3월에 나온 사업보고서가 영영 안 들어온다 (#98).
    """
    if not tickers:
        return set()
    async with SessionFactory() as session:
        result = await session.execute(
            select(Document.ticker)
            .where(Document.source == SOURCE, Document.ticker.in_(tickers))
            .distinct()
        )
        return set(tickers) - set(result.scalars())


async def existing_rcept_nos(rcept_nos: Sequence[str]) -> set[str]:
    """이미 적재한 접수번호. 원문을 다시 받지 않기 위한 증분 판정이다."""
    if not rcept_nos:
        return set()
    async with SessionFactory() as session:
        result = await session.execute(
            select(Document.external_id).where(
                Document.source == SOURCE, Document.external_id.in_(rcept_nos)
            )
        )
        return set(result.scalars())


async def save(filing: Filing, body: str) -> int:
    """공시 한 건과 청크를 저장한다. 재실행해도 중복이 생기지 않는다."""
    statement = pg_insert(Document).values(
        doc_type=DocumentType.FILING,
        source=SOURCE,
        external_id=filing.rcept_no,
        ticker=filing.ticker,
        title=filing.report_nm,
        body=body,
        url=filing.url,
        publisher=filing.corp_name or None,
        published_at=filing.published_at,
    )
    statement = statement.on_conflict_do_update(
        constraint="uq_documents_source_external",
        set_={
            "title": statement.excluded.title,
            "body": statement.excluded.body,
            "url": statement.excluded.url,
            "publisher": statement.excluded.publisher,
            "published_at": statement.excluded.published_at,
        },
    ).returning(Document.id)

    pieces = chunk(body)
    async with SessionFactory() as session:
        document_id = (await session.execute(statement)).scalar_one()
        # 재적재면 청킹 결과가 달라질 수 있다. 남은 조각이 근거로 딸려 나오지 않게 지운다.
        await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
        if pieces:
            await session.execute(
                pg_insert(DocumentChunk).values(
                    [
                        # embedding은 NULL. 제공자가 정해지면 별도 배치로 채운다.
                        # text_tsv는 반드시 to_tsvector() 로 넣는다 — 문자열을
                        # 그대로 캐스팅하면 위치 정보가 빠져 ts_rank_cd 가 전부 0이 된다.
                        {
                            "document_id": document_id,
                            "chunk_index": i,
                            "text": text,
                            "text_tsv": weighted_tsvector(filing.report_nm, text),
                        }
                        for i, text in enumerate(pieces)
                    ]
                )
            )
        await session.commit()
    return len(pieces)


# ── 실행 ───────────────────────────────────────────────────────


async def run(
    days: int,
    limit: int | None,
    max_docs: int,
    tickers: Sequence[str] | None = None,
    backfill_days: int | None = None,
) -> tuple[int, int, int]:
    """(적재 공시 수, 청크 수, 적재 실패 종목 수).

    개별 API 거절은 경고로 격리하고, 전 종목의 동일 거절은 예외로 보고한다.
    `backfill_days` 를 주면 원문이 한 건도 없는 종목만 그만큼 거슬러 받는다.
    """
    api_key = (settings.dart_api_key or "").strip()
    if not api_key:
        log.error("DART_API_KEY가 없다. .env를 확인하라")
        return 0, 0, 1

    targets = await load_targets(limit, tickers)
    if not targets:
        # 대상 0건은 조용한 날이 아니라 instruments 가 비었다는 뜻이다. cron 이 알아야 한다.
        log.error("corp_code가 있는 종목이 없다. 먼저 `python -m ingest.instruments`를 돌려라")
        return 0, 0, 1

    today = date.today()
    bgn_de = (today - timedelta(days=days)).strftime("%Y%m%d")
    end_de = today.strftime("%Y%m%d")
    backfill_de = (
        (today - timedelta(days=backfill_days)).strftime("%Y%m%d") if backfill_days else None
    )
    fresh = await tickers_without_documents([t for t, _ in targets]) if backfill_days else set()
    log.info("대상 %d종목 · 기간 %s~%s · 종목당 최대 %d건", len(targets), bgn_de, end_de, max_docs)
    if fresh:
        log.info(
            "첫 적재 %d종목은 %s 부터 받는다: %s", len(fresh), backfill_de, ",".join(sorted(fresh))
        )

    rejections: Counter[str] = Counter()
    saved = chunks = failed = 0
    with httpx.Client() as client:
        for i, (ticker, corp_code) in enumerate(targets, start=1):
            try:
                since = backfill_de if ticker in fresh else bgn_de
                filings = keep_latest_periodic(
                    fetch_filing_list(
                        client, api_key, corp_code, ticker, since, end_de, DOCUMENT_TYPES
                    )
                )
                known = await existing_rcept_nos([f.rcept_no for f in filings])
                pending = [f for f in filings if f.rcept_no not in known][:max_docs]
                log.info(
                    "[%d/%d] %s 공시 %d건 · 신규 %d건",
                    i,
                    len(targets),
                    ticker,
                    len(filings),
                    len(pending),
                )

                for filing in pending:
                    time.sleep(REQUEST_DELAY_S)
                    body = fetch_document(client, api_key, filing.rcept_no)
                    if not body or not body.strip():
                        continue
                    chunks += await save(filing, body)
                    saved += 1
            except DartStatusError as exc:
                rejections[exc.status] += 1
            except Exception:
                # 한 종목이 죽어도 배치를 멈추지 않는다. 재실행하면 실패분만 다시 받는다.
                log.exception("종목 적재 실패: ticker=%s", ticker)
                failed += 1

    check_systemic_failure(rejections, len(targets))
    log.info(
        "공시 %d건 · 청크 %d개 적재 · 실패 %d종목 · API 거절 %d종목",
        saved,
        chunks,
        failed,
        sum(rejections.values()),
    )
    return saved, chunks, failed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DART 공시 적재기")
    parser.add_argument("--days", type=int, default=30, help="조회 기간(일). 기본 30")
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=None,
        help="공시가 한 건도 없는 종목만 이만큼 거슬러 받는다 (예: 365)",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="서비스 목록의 앞 N종목만 (기본 전체)"
    )
    parser.add_argument(
        "--tickers", help="쉼표 구분 종목코드. 주어지면 limit 을 무시하고 그 종목만"
    )
    parser.add_argument("--max-docs", type=int, default=20, help="종목당 원문 수. 기본 20")
    parser.add_argument("--verbose", action="store_true", help="DEBUG 로그")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    # httpx는 요청 URL을 통째로 찍는다. crtfc_key가 로그에 남으면 안 된다.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # 증분의 0건은 정상이다. 시스템 전체 API 거절이나 적재 예외는 비정상 종료한다.
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()] if args.tickers else None
    try:
        _, _, failed = asyncio.run(
            run(args.days, args.limit, args.max_docs, tickers, args.backfill_days)
        )
    except SystemicDartError as exc:
        log.error("%s", exc)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
