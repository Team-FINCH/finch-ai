"""FastAPI 애플리케이션.

라우터는 기능별 파일로 분리한다. 병렬 트랙이 같은 파일을 건드리지 않게 하려는 것이다.
에러 → HTTP 변환은 여기서만 일어난다.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

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
from app.core.db import engine
from app.core.errors import AppError, ErrorCode
from app.core.schemas import ErrorResponse, new_request_id
from app.core.usage_limits import default_guard

logger = logging.getLogger(__name__)

API_PREFIX = "/api/ai/v1"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await engine.dispose()


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

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.app_env, "model": settings.llm_model}

    return app


app = create_app()
