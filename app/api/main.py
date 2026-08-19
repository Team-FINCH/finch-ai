"""FastAPI 애플리케이션.

라우터는 기능별 파일로 분리한다. 병렬 트랙이 같은 파일을 건드리지 않게 하려는 것이다.
에러 → HTTP 변환은 여기서만 일어난다.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

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
from app.core.schemas import ErrorBody, ErrorResponse, new_request_id

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

    for module in (stocks, chat, portfolio, orders, briefing, wiki, feedback):
        app.include_router(module.router, prefix=API_PREFIX)

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorBody(code=exc.code, message=exc.message, detail=exc.detail),
            request_id=new_request_id(),
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=body.model_dump(mode="json"),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorBody(
                code=ErrorCode.INVALID_REQUEST,
                message="요청 형식이 올바르지 않습니다.",
                detail={"errors": exc.errors()},
            ),
            request_id=new_request_id(),
        )
        return JSONResponse(status_code=400, content=body.model_dump(mode="json"))

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.app_env, "model": settings.llm_model}

    return app


app = create_app()
