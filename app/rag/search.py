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

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.core.db import SessionFactory, engine
from app.core.errors import RetrievalFailed
from app.core.models import Document, DocumentChunk
from app.rag.embedding import NullEmbedder, get_embedder
from app.rag.lexical import lexical_tsquery, to_tsv_text

log = logging.getLogger("app.rag.search")

BACKFILL_CHUNK = 256

# 융합 전 각 경로가 확보하는 후보 수. 최종 top_k 보다 넉넉해야 RRF가
# 한쪽에서만 발견된 근거를 살릴 수 있다.
CANDIDATE_POOL = 60
# RRF 완화 계수. 표준값 60 — 순위 차이를 너무 과하게 벌리지 않는다.
RRF_K = 60


async def backfill(limit: int | None = None) -> tuple[int, int]:
    """embedding이 NULL인 조각을 채운다. (시도, 성공)을 돌려준다.

    증분이다. 중단해도 다시 실행하면 남은 것부터 이어서 한다.
    """
    embedder = get_embedder()
    if isinstance(embedder, NullEmbedder):
        log.error("임베딩 제공자가 없다. OPENAI_API_KEY를 설정하라")
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
    dense_rows, lexical_rows, *, top_k: int = 5, k: int = RRF_K
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
    for rows in (dense_rows, lexical_rows):
        for rank, row in enumerate(rows, start=1):
            cid = row[0]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            if cid not in best:
                best[cid] = row

    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    used: set = set()
    final: list = []
    # 어휘 상위 보장 슬롯 — 제목이 정확히 맞는 근거가 묻히지 않게 한다.
    quota = max(1, round(top_k * 0.4))
    for row in lexical_rows[:quota]:
        cid = row[0]
        if cid not in used:
            final.append(row)
            used.add(cid)
    for cid, _score in ordered:
        if len(final) >= top_k:
            break
        if cid not in used:
            final.append(best[cid])
            used.add(cid)

    return [
        {
            "text": r.text,
            "ticker": r.ticker,
            "title": r.title,
            "published_at": r.published_at,
            "similarity": round(scores[r[0]], 6),
        }
        for r in final[:top_k]
    ]


async def _dense_candidates(query_vec, *, ticker: str | None, limit: int):
    """밀집(벡터) 경로 후보."""
    distance = DocumentChunk.embedding.cosine_distance(query_vec)
    stmt = (
        select(
            DocumentChunk.id,
            DocumentChunk.text,
            Document.ticker,
            Document.title,
            Document.published_at,
            distance.label("distance"),
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(DocumentChunk.embedding.is_not(None))
    )
    if ticker:
        stmt = stmt.where(Document.ticker == ticker)
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


async def search(query: str, *, top_k: int = 5, ticker: str | None = None) -> list[dict]:
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

    dense_rows = await _dense_candidates(vec, ticker=ticker, limit=CANDIDATE_POOL)

    lexical_rows: list = []
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
                    rank,
                )
                .join(Document, Document.id == DocumentChunk.document_id)
                .where(DocumentChunk.text_tsv.op("@@")(q))
            )
            if ticker:
                stmt = stmt.where(Document.ticker == ticker)
            stmt = stmt.order_by(rank.desc()).limit(CANDIDATE_POOL)
            lexical_rows = await _run(stmt)
        except SQLAlchemyError:
            # 어휘 경로 실패는 열화 상태다. 전체를 죽이지 않고 진행한다.
            log.exception("어휘 검색이 실패해 밀집 결과만으로 진행한다")

    return fuse(dense_rows, lexical_rows, top_k=top_k)


async def tsv_backfill(limit: int | None = None) -> tuple[int, int]:
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
            rows = (
                await session.execute(
                    select(DocumentChunk, Document.title)
                    .join(Document, Document.id == DocumentChunk.document_id)
                    .where(DocumentChunk.text_tsv.is_(None))
                    .order_by(DocumentChunk.id)
                    .limit(take)
                )
            ).all()
            if not rows:
                break
            for chunk_row, title in rows:
                chunk_row.text_tsv = to_tsv_text(f"{title}\n{chunk_row.text}")
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
