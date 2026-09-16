"""FastAPI 애플리케이션.

라우터는 기능별 파일로 분리한다. 병렬 트랙이 같은 파일을 건드리지 않게 하려는 것이다.
에러 → HTTP 변환은 여기서만 일어난다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select

from app import chat_jobs
from app.api.routes import (
    briefing,
    chat,
    feedback,
    orders,
    portfolio,
    stocks,
    wiki,
)
from app.core.config import settings
from app.core.db import SessionFactory, engine
from app.core.enums import InstrumentStatus
from app.core.errors import AppError, ErrorCode
from app.core.models import Document, DocumentChunk, Instrument, PriceDaily
from app.core.schemas import (
    ErrorResponse,
    HealthResponse,
    IngestState,
    new_request_id,
)
from app.core.usage_limits import default_guard

logger = logging.getLogger(__name__)

API_PREFIX = "/api/ai/v1"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # 채팅 비동기 작업 워커 (#90). 로컬(APP_ENV=local)도 켠다 — 동기 /chat 이 이 워커에
    # 기대므로 워커 없이는 55초 뒤 504 다.
    stop = asyncio.Event()
    task = asyncio.create_task(chat_jobs.worker(app.state.usage_guard, stop=stop))
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await engine.dispose()


# ── 적재 상태 점검 ──────────────────────────────────────────────────────────────
#: 이름 → (존재 여부 질의, 지표 설명). /health 와 /metrics 가 같은 정의를 쓴다.
#: 전부 EXISTS 한 건이라 count(*) 처럼 테이블을 훑지 않는다 — 행이 늘어도 비용이 같다.
_INGEST_PROBES = {
    "documents": (
        select(1).select_from(Document).limit(1),
        "RAG 문서가 한 건이라도 적재되었으면 1.",
    ),
    "embeddings": (
        select(1).select_from(DocumentChunk).where(DocumentChunk.embedding.is_not(None)).limit(1),
        "임베딩이 채워진 청크가 한 건이라도 있으면 1.",
    ),
    "price_daily": (
        select(1).select_from(PriceDaily).limit(1),
        "일별 시세가 한 건이라도 적재되었으면 1.",
    ),
    # price_daily 만 보면 종목 하나만 들어와도 1이라 중단된 백필을 놓친다.
    # 상장 종목 중 시세가 한 건도 없는 것이 남아 있는지를 따로 본다.
    #: embeddings 는 청크 한 건만 임베딩되어도 1 이라 부분 백필을 못 잡는다.
    #: 적재는 됐는데 --backfill 이 덜 돌면 검색 품질만 조용히 무너진다 (GitLab #62).
    "embedding_backfill_pending": (
        select(1).select_from(DocumentChunk).where(DocumentChunk.embedding.is_(None)).limit(1),
        "임베딩이 비어 있는 청크가 남아 있으면 1 — 백필이 덜 끝났다는 뜻이다.",
    ),
    "price_backfill_pending": (
        select(1)
        .select_from(Instrument)
        .where(
            Instrument.status == InstrumentStatus.LISTED,
            ~select(1)
            .select_from(PriceDaily)
            .where(PriceDaily.ticker == Instrument.ticker)
            .exists(),
        )
        .limit(1),
        "시세가 한 건도 없는 상장 종목이 남아 있으면 1 — 백필이 덜 끝났다는 뜻이다.",
    ),
}

#: /health 가 보고하는 항목. IngestState 필드와 짝이다.
_HEALTH_PROBES = ("documents", "embeddings", "price_daily")


async def _probe_ingest(
    names: tuple[str, ...],
) -> tuple[dict[str, bool], list[str]]:
    """적재 상태를 한 세션에서 확인한다.

    DB 가 죽어도 예외를 밖으로 내보내지 않는다. 점검 엔드포인트가 500이면
    로드밸런서도 스크레이퍼도 진단 본문을 못 받는다. 그때는 확인 못 한 항목이 False 다.
    """
    state = dict.fromkeys(names, False)
    pending = list(names)
    try:
        async with SessionFactory() as session:
            for name in names:
                stmt, _ = _INGEST_PROBES[name]
                state[name] = (await session.execute(stmt)).first() is not None
                pending.remove(name)
    except Exception:
        logger.warning("ingest-state query failed")
    return state, pending


def create_app() -> FastAPI:
    app = FastAPI(
        title="AI 투자 비서 — AI 파트",
        version="0.1.0",
        description="국내 주식 전용. 수치는 엔진에서만 나오고 LLM은 설명만 생성한다.",
        lifespan=lifespan,
    )
    # 배치도 같은 팩터리로 장부를 고른다. DB 통합 테스트는 UsageGuard(SessionFactory)를
    # 직접 쓴다.
    app.state.usage_guard = default_guard()

    for module in (stocks, chat, portfolio, orders, briefing, wiki, feedback):
        app.include_router(module.router, prefix=API_PREFIX)

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        body = ErrorResponse(
            code=exc.code,
            message=exc.message,
            detail=exc.detail,
            request_id=new_request_id(),
        )
        headers = None
        retry_after = exc.detail.get("retry_after_seconds")
        if exc.code == ErrorCode.RATE_LIMITED and retry_after is not None:
            headers = {"Retry-After": str(retry_after)}
        return JSONResponse(
            status_code=exc.status_code,
            content=body.model_dump(mode="json"),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        body = ErrorResponse(
            code=ErrorCode.INVALID_REQUEST,
            message="요청 형식이 올바르지 않습니다.",
            detail={"errors": exc.errors()},
            request_id=new_request_id(),
        )
        return JSONResponse(status_code=400, content=body.model_dump(mode="json"))

    @app.get("/health", tags=["ops"], response_model=HealthResponse)
    async def health() -> HealthResponse:
        """RAG 문서·임베딩과 시세 적재 상태를 HTTP 200으로 보고한다."""
        state, errors = await _probe_ingest(_HEALTH_PROBES)
        ingest = IngestState(**state)
        return HealthResponse(
            status="ok" if all(ingest.model_dump().values()) else "degraded",
            env=settings.app_env,
            model=settings.llm_model,
            ingest=ingest,
            ingest_probe_errors=errors,
        )

    # docs/openapi.json 에는 넣지 않는다. 그 파일은 프론트가 Postman 으로 읽는 계약이고
    # 이 경로는 Prometheus 가 긁는 text 라 낄 자리가 아니다.
    @app.get("/metrics", tags=["ops"], include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        """적재 상태를 Prometheus 게이지로 노출한다.

        DB 가 죽으면 backfill_pending 까지 0이라 겉보기에는 정상이지만, 나머지 셋이
        동시에 0으로 떨어지므로 경보는 그쪽에서 잡힌다.
        """
        state, _ = await _probe_ingest(tuple(_INGEST_PROBES))
        lines: list[str] = []
        for name, ok in state.items():
            metric = f"ingest_{name}"
            _, help_text = _INGEST_PROBES[name]
            lines += [
                f"# HELP {metric} {help_text}",
                f"# TYPE {metric} gauge",
                f"{metric} {int(ok)}",
            ]
        # 스크레이퍼는 마지막 줄바꿈이 없으면 마지막 샘플을 버린다.
        return PlainTextResponse(
            "\n".join(lines) + "\n",
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    return app


app = create_app()
