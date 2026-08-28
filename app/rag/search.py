"""공시 조각 검색과 임베딩·tsvector 백필.

    python -m app.rag.search --backfill          비어 있는 조각을 채운다
    python -m app.rag.search --tsv-backfill      어휘 인덱스(text_tsv)를 채운다
    python -m app.rag.search --query "HBM 투자"   확인용 검색

검색은 하이브리드다 — pgvector 코사인(밀집)과 bigram tsvector 랭킹(어휘)을
RRF로 융합한다. 코퍼스가 커질수록 밀집 단독으로는 대형 문서 조각에 묻히므로
둘을 결합한다(2026-08-25 실측 기록은 eval/retrieval.yaml baseline 참조).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
from datetime import UTC, datetime

from sqlalchemy import Float, bindparam, case, cast, func, literal_column, or_, select
from sqlalchemy.exc import SQLAlchemyError

from app.core.db import SessionFactory, engine
from app.core.enums import DocumentType
from app.core.errors import RetrievalFailed
from app.core.models import Document, DocumentChunk
from app.rag.embedding import NullEmbedder, get_embedder
from app.rag.lexical import bigrams, lexical_tsquery, to_tsv_text

log = logging.getLogger("app.rag.search")

BACKFILL_CHUNK = 256

# 융합 전 각 경로가 확보하는 후보 수. 최종 top_k 보다 넉넉해야 RRF가
# 한쪽에서만 발견된 근거를 살릴 수 있다.
CANDIDATE_POOL = 60
# RRF 완화 계수. 표준값 60 — 순위 차이를 너무 과하게 벌리지 않는다.
RRF_K = 60

# RRF 점수에 곱하는 출처 신뢰도. 검색 결과를 배제하는 필터가 아니라 같은 관련도일 때
# 공식 원천을 먼저 보여 주는 작은 우선순위다. 값 차이를 크게 두면 최신 뉴스가 오래된
# 공시에 항상 밀리므로 15% 안에서만 조정한다.
SOURCE_WEIGHT = {
    "DART": 1.15,
    "ECOS": 1.15,
    "KRX": 1.15,
    "KIS": 1.10,
    "NAVER_API_HUB": 1.00,
}

# 문서 유형마다 낡는 속도가 다르다. 뉴스는 빠르게, 정기 공시는 천천히 감쇠한다.
FRESHNESS_HALF_LIFE_DAYS = {
    DocumentType.NEWS.value: 14,
    DocumentType.FILING.value: 90,
    DocumentType.FINANCIAL.value: 180,
    DocumentType.MACRO.value: 30,
}


def _source_weight(source: str | None) -> float:
    return SOURCE_WEIGHT.get((source or "").upper(), 0.95)


def _freshness_score(
    published_at: datetime | None,
    doc_type: str | DocumentType | None,
    *,
    now: datetime,
) -> float:
    """오래돼도 0.5 아래로 버리지 않는다. 관련성이 최신성보다 항상 우선이다."""
    if published_at is None:
        return 0.5
    point = published_at
    if point.tzinfo is None:
        point = point.replace(tzinfo=UTC)
    reference = now if now.tzinfo else now.replace(tzinfo=UTC)
    age_days = max(0.0, (reference - point).total_seconds() / 86_400)
    kind = doc_type.value if isinstance(doc_type, DocumentType) else str(doc_type or "")
    half_life = FRESHNESS_HALF_LIFE_DAYS.get(kind, 90)
    return 0.5 + 0.5 * math.exp(-math.log(2) * age_days / half_life)


async def backfill(limit: int | None = None) -> tuple[int, int]:
    """embedding이 NULL인 조각을 채운다. (시도, 성공)을 돌려준다.

    증분이다. 중단해도 다시 실행하면 남은 것부터 이어서 한다.
    """
    embedder = get_embedder()
    if isinstance(embedder, NullEmbedder):
        log.error("임베딩 제공자가 없다. GMS_KEY를 설정하라")
        return 0, 0

    tried = done = 0
    async with SessionFactory() as session:
        while True:
            take = BACKFILL_CHUNK if limit is None else min(BACKFILL_CHUNK, limit - tried)
            if take <= 0:
                break
            rows = (
                await session.scalars(
                    select(DocumentChunk)
                    .where(DocumentChunk.embedding.is_(None))
                    .order_by(DocumentChunk.id)
                    .limit(take)
                )
            ).all()
            if not rows:
                break

            vectors = await asyncio.to_thread(embedder.embed, [r.text for r in rows])
            filled = 0
            for row, vec in zip(rows, vectors, strict=True):
                tried += 1
                if vec is not None:
                    row.embedding = vec
                    done += 1
                    filled += 1
            await session.commit()
            log.info("백필 %d건 시도 · %d건 성공", tried, done)

            if filled == 0:
                # 한 묶음도 못 채웠다는 건 임베딩 쪽이 계속 실패한다는 뜻이다.
                # 다음 회차는 embedding이 NULL인 같은 행을 다시 집으므로
                # 여기서 끊지 않으면 무한 루프가 된다. 실제로 한 번 돌았고,
                # 2,280건짜리 백필이 20,000건 시도를 찍으며 일당 요청 한도를
                # 전부 태웠다.
                log.error("이번 묶음을 하나도 채우지 못해 중단한다")
                break

    return tried, done


def fuse(
    dense_rows,
    lexical_rows,
    title_rows=(),
    *,
    top_k: int = 5,
    k: int = RRF_K,
    now: datetime | None = None,
) -> list[dict]:
    """두 랭킹을 Reciprocal Rank Fusion 으로 묶되 어휘 상위를 보장한다.

    score = Σ 1/(k+rank). 순위만 보고 점수 스케일을 무시하므로 코사인 거리와
    ts_rank_cd 처럼 단위가 다른 근거를 섞을 때 안전하다.

    순수 RRF 만 쓰면 희석이 생긴다 — 밀집 상위 조각들이 우연히 어휘도 일부
    맞아 작은 가점을 받으면, 제목이 정확히 맞는 어휘 전문가(융합 점수는 낮음)
   를 밀어낸다. 그래서 어휘 상위 몇 개는 융합 점수와 무관하게 슬롯을 보장한다.
    """
    scores: dict = {}
    best: dict = {}
    for rows in (dense_rows, lexical_rows, title_rows):
        for rank, row in enumerate(rows, start=1):
            cid = row[0]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            if cid not in best:
                best[cid] = row

    reference = now or datetime.now(UTC)
    adjusted = {
        cid: score
        * _source_weight(best[cid].source)
        * _freshness_score(best[cid].published_at, best[cid].doc_type, now=reference)
        for cid, score in scores.items()
    }
    ordered = sorted(adjusted.items(), key=lambda kv: kv[1], reverse=True)
    used: set = set()
    final: list = []
    # 어휘 상위 보장 슬롯 — 제목이 정확히 맞는 근거가 묻히지 않게 한다.
    quota = max(1, round(top_k * 0.4))
    for row in lexical_rows[:quota]:
        cid = row[0]
        if cid not in used:
            final.append(row)
            used.add(cid)
    # 질의 핵심 어휘가 제목에 직접 들어간 문서는 본문 길이에 묻히지 않게 한 자리 보장한다.
    for row in title_rows[:1]:
        cid = row[0]
        if cid not in used and len(final) < top_k:
            final.append(row)
            used.add(cid)
    for cid, _score in ordered:
        if len(final) >= top_k:
            break
        if cid not in used:
            final.append(best[cid])
            used.add(cid)

    # 어휘 보장 슬롯도 최종 출력에서는 품질 점수순으로 놓는다. 정확한 제목 매치는
    # 빠지지 않되, 같은 후보 집합 안에서는 더 최신이고 신뢰할 수 있는 자료가 앞선다.
    final.sort(key=lambda row: adjusted[row[0]], reverse=True)

    return [
        {
            "text": r.text,
            "ticker": r.ticker,
            "title": r.title,
            "published_at": r.published_at,
            "doc_type": r.doc_type,
            "source": r.source,
            "publisher": r.publisher,
            "url": r.url,
            "similarity": round(adjusted[r[0]], 6),
            "rrf_score": round(scores[r[0]], 6),
            "freshness_score": round(
                _freshness_score(r.published_at, r.doc_type, now=reference), 6
            ),
            "source_weight": _source_weight(r.source),
        }
        for r in final[:top_k]
    ]


async def _dense_candidates(
    query_vec, *, ticker: str | None, doc_type: DocumentType | None, limit: int
):
    """밀집(벡터) 경로 후보."""
    distance = DocumentChunk.embedding.cosine_distance(query_vec)
    stmt = (
        select(
            DocumentChunk.id,
            DocumentChunk.text,
            Document.ticker,
            Document.title,
            Document.published_at,
            Document.doc_type,
            Document.source,
            Document.publisher,
            Document.url,
            distance.label("distance"),
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(DocumentChunk.embedding.is_not(None))
    )
    if ticker:
        stmt = stmt.where(Document.ticker == ticker)
    if doc_type:
        stmt = stmt.where(Document.doc_type == doc_type)
    stmt = stmt.order_by(distance).limit(limit)

    try:
        async with SessionFactory() as session:
            return (await session.execute(stmt)).all()
    except SQLAlchemyError as exc:
        # 벡터 저장소가 흔들린 것도 근거 검색 실패다. 500으로 새어 나가면
        # 프런트가 재시도 가능한 실패인지 알 수 없다(§2.6).
        log.exception("근거 검색 질의에 실패했다")
        raise RetrievalFailed("근거 검색에 실패했습니다.") from exc


async def _run(stmt):
    async with SessionFactory() as session:
        return (await session.execute(stmt)).all()


async def _title_candidates(query: str, *, ticker: str | None, limit: int):
    """질의 바이그램이 제목에서 차지하는 비율로 문서 후보를 찾는다."""
    grams = list(dict.fromkeys(bigrams(query)))
    if not grams:
        return []
    matches = [case((Document.title.contains(gram), 1), else_=0) for gram in grams]
    matched = sum(matches[1:], matches[0])
    title_length = func.greatest(func.length(Document.title) - 1, 1)
    rank = (cast(matched, Float) / title_length).label("rank")
    stmt = (
        select(
            DocumentChunk.id,
            DocumentChunk.text,
            Document.ticker,
            Document.title,
            Document.published_at,
            Document.doc_type,
            Document.source,
            Document.publisher,
            Document.url,
            rank,
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            DocumentChunk.chunk_index == 0,
            or_(*(Document.title.contains(gram) for gram in grams)),
        )
    )
    if ticker:
        stmt = stmt.where(Document.ticker == ticker)
    return await _run(stmt.order_by(rank.desc(), Document.published_at.desc()).limit(limit))


async def search(
    query: str,
    *,
    top_k: int = 5,
    ticker: str | None = None,
    doc_type: DocumentType | None = None,
) -> list[dict]:
    """질의와 가까운 조각을 돌려준다 — 하이브리드(밀집 + 어휘 RRF).

    조각과 함께 원문 제목·공시일을 붙여, 호출부가 근거를 구성할 때 문서를
    다시 조회하지 않아도 되게 한다.

    **빈 리스트는 "맞는 자료가 없다"는 뜻이지 "검색이 고장났다"가 아니다.**
    둘을 같은 값으로 돌려주면 호출부가 구분할 수 없어, 검색이 죽은 동안
    "관련 자료를 찾지 못했습니다"(프롬프트 정책 §7)라는 태연한 답이 나간다.
    그래서 밀집 검색 자체가 불가능한 경우는 RetrievalFailed로 올린다 — 재시도
    하면 풀릴 수 있는 실패라 API 명세 §2.6의 502에 해당한다. 어휘 경로는
    실패해도 밀집 결과만으로 진행한다(성능 저하지 고장이 아니다).
    """
    embedder = get_embedder()
    if isinstance(embedder, NullEmbedder):
        log.error("임베딩 제공자가 없다. 검색을 수행할 수 없다")
        raise RetrievalFailed("근거 검색을 사용할 수 없습니다.")

    vec = (await asyncio.to_thread(embedder.embed, [query]))[0]
    if vec is None:
        log.error("질의 임베딩에 실패했다")
        raise RetrievalFailed("근거 검색에 실패했습니다.")

    return await search_with_vector(
        query, vec, top_k=top_k, ticker=ticker, doc_type=doc_type
    )


async def search_with_vector(
    query: str,
    query_vec,
    *,
    top_k: int = 5,
    ticker: str | None = None,
    doc_type: DocumentType | None = None,
) -> list[dict]:
    """이미 만든 질의 벡터로 검색한다. 평가 배치가 임베딩을 한 번에 묶을 때 쓴다."""
    dense_rows = await _dense_candidates(
        query_vec, ticker=ticker, doc_type=doc_type, limit=CANDIDATE_POOL
    )

    lexical_rows: list = []
    title_rows: list = []
    try:
        title_rows = await _title_candidates(query, ticker=ticker, limit=CANDIDATE_POOL)
    except SQLAlchemyError:
        log.exception("제목 검색이 실패해 다른 검색 결과만 사용한다")
    tsq = lexical_tsquery(query)
    if tsq:
        try:
            q = func.to_tsquery("simple", tsq)
            rank = func.ts_rank_cd(DocumentChunk.text_tsv, q).label("rank")
            stmt = (
                select(
                    DocumentChunk.id,
                    DocumentChunk.text,
                    Document.ticker,
                    Document.title,
                    Document.published_at,
                    Document.doc_type,
                    Document.source,
                    Document.publisher,
                    Document.url,
                    rank,
                )
                .join(Document, Document.id == DocumentChunk.document_id)
                .where(DocumentChunk.text_tsv.op("@@")(q))
            )
            if ticker:
                stmt = stmt.where(Document.ticker == ticker)
            if doc_type:
                stmt = stmt.where(Document.doc_type == doc_type)
            stmt = stmt.order_by(rank.desc()).limit(CANDIDATE_POOL)
            lexical_rows = await _run(stmt)
        except SQLAlchemyError:
            # 어휘 경로 실패는 열화 상태다. 전체를 죽이지 않고 진행한다.
            log.exception("어휘 검색이 실패해 밀집 결과만으로 진행한다")

    return fuse(dense_rows, lexical_rows, title_rows, top_k=top_k)


async def tsv_backfill(
    limit: int | None = None, *, rebuild: bool = False
) -> tuple[int, int]:
    """text_tsv 가 비어 있는 기존 조각을 채운다. (시도, 성공).

    저장 경로와 같게 제목을 섞는다(dart.save 참조). 증분이다. 중단해도
    다시 실행하면 남은 것부터 이어서 한다.
    """
    tried = done = 0
    async with SessionFactory() as session:
        while True:
            take = BACKFILL_CHUNK if limit is None else min(BACKFILL_CHUNK, limit - tried)
            if take <= 0:
                break
            stmt = (
                select(DocumentChunk, Document.title)
                .join(Document, Document.id == DocumentChunk.document_id)
                .order_by(DocumentChunk.id)
                .limit(take)
            )
            if not rebuild:
                stmt = stmt.where(DocumentChunk.text_tsv.is_(None))
            elif tried:
                stmt = stmt.offset(tried)
            rows = (await session.execute(stmt)).all()
            if not rows:
                break
            # 문자열을 그대로 캐스팅하면 위치가 빠져 ts_rank_cd 가 죽는다.
            # 반드시 to_tsvector('simple', …) 로 감싸 넣는다. Core 테이블
            # 업데이트를 쓴다 — ORM 벌크 모드는 PK 파라미터를 강요한다.
            await session.execute(
                DocumentChunk.__table__.update()
                .where(DocumentChunk.__table__.c.id == bindparam("bid"))
                .values(
                    text_tsv=func.setweight(
                        func.to_tsvector("simple", bindparam("title_tsv")),
                        literal_column("'A'"),
                    ).op("||")(
                        func.setweight(
                            func.to_tsvector("simple", bindparam("body_tsv")),
                            literal_column("'D'"),
                        )
                    )
                ),
                [
                    {
                        "bid": chunk_row.id,
                        "title_tsv": to_tsv_text(title),
                        "body_tsv": to_tsv_text(chunk_row.text),
                    }
                    for chunk_row, title in rows
                ],
            )
            tried += len(rows)
            done += len(rows)
            await session.commit()
            log.info("tsv 백필 %d건 누적", tried)
    return tried, done


async def coverage() -> tuple[int, int]:
    """(전체 조각, 임베딩 보유 조각)."""
    async with SessionFactory() as session:
        total = await session.scalar(select(func.count()).select_from(DocumentChunk))
        filled = await session.scalar(
            select(func.count()).select_from(DocumentChunk).where(DocumentChunk.embedding.is_not(None))
        )
    return int(total or 0), int(filled or 0)


async def _main() -> int:
    parser = argparse.ArgumentParser(description="공시 조각 임베딩·검색")
    parser.add_argument("--backfill", action="store_true", help="비어 있는 조각을 채운다")
    parser.add_argument("--tsv-backfill", action="store_true", help="어휘 인덱스(text_tsv)가 비어 있는 조각을 채운다")
    parser.add_argument(
        "--tsv-rebuild",
        action="store_true",
        help="기존 조각까지 제목 가중치가 적용된 어휘 인덱스로 다시 만든다",
    )
    parser.add_argument("--limit", type=int, help="백필 최대 건수")
    parser.add_argument("--query", help="확인용 검색")
    parser.add_argument("--ticker", help="검색을 한 종목으로 좁힌다")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")

    try:
        total, filled = await coverage()
        log.info("조각 %d개 중 %d개에 임베딩이 있다", total, filled)

        if args.backfill:
            tried, done = await backfill(args.limit)
            log.info("백필 완료 — 시도 %d · 성공 %d", tried, done)

        if args.tsv_backfill:
            tried, done = await tsv_backfill(args.limit)
            log.info("tsv 백필 완료 — 시도 %d · 성공 %d", tried, done)

        if args.tsv_rebuild:
            tried, done = await tsv_backfill(args.limit, rebuild=True)
            log.info("tsv 재구축 완료 — 시도 %d · 성공 %d", tried, done)

        if args.query:
            hits = await search(args.query, top_k=args.top_k, ticker=args.ticker)
            if not hits:
                log.warning("결과가 없다")
            for h in hits:
                print(f"  {h['similarity']:.4f}  [{h['ticker']}] {h['title'][:34]}")
                print(f"          {h['text'][:88]}")
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
