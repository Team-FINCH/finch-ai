"""에러 코드와 예외.

API 명세 §2.6의 표를 그대로 옮겼다. HTTP 상태 코드는 여기서만 결정하며,
라우터가 직접 status_code를 쓰지 않는다.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    UNSUPPORTED_MARKET = "UNSUPPORTED_MARKET"
    UNAUTHORIZED = "UNAUTHORIZED"
    INSTRUMENT_NOT_FOUND = "INSTRUMENT_NOT_FOUND"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"
    RATE_LIMITED = "RATE_LIMITED"
    RETRIEVAL_FAILED = "RETRIEVAL_FAILED"
    LLM_TIMEOUT = "LLM_TIMEOUT"


STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.UNSUPPORTED_MARKET: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.INSTRUMENT_NOT_FOUND: 404,
    ErrorCode.INSUFFICIENT_DATA: 409,
    ErrorCode.GUARDRAIL_BLOCKED: 422,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.RETRIEVAL_FAILED: 502,
    ErrorCode.LLM_TIMEOUT: 504,
}


class AppError(Exception):
    """모든 도메인 예외의 기반.

    라우터는 이 예외만 던지고, HTTP 변환은 예외 핸들러가 담당한다.
    """

    code: ErrorCode = ErrorCode.INVALID_REQUEST

    def __init__(
        self,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
        code: ErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}
        if code is not None:
            self.code = code

    @property
    def status_code(self) -> int:
        return STATUS_BY_CODE[self.code]


class InvalidRequest(AppError):
    code = ErrorCode.INVALID_REQUEST


class UnsupportedMarket(AppError):
    """해외 종목 요청. 이 서비스는 국내 주식 전용이다."""

    code = ErrorCode.UNSUPPORTED_MARKET


class Unauthorized(AppError):
    code = ErrorCode.UNAUTHORIZED


class InstrumentNotFound(AppError):
    code = ErrorCode.INSTRUMENT_NOT_FOUND


class InsufficientData(AppError):
    """보유 종목 0개, 가격 히스토리 부족 등.

    정상 상황이므로 화면 전체 실패로 다루지 않는다. AI 영역만 비활성 처리한다.
    """

    code = ErrorCode.INSUFFICIENT_DATA


class GuardrailBlocked(AppError):
    """생성물이 차단되었거나 요청 자체가 허용 범위를 벗어났다."""

    code = ErrorCode.GUARDRAIL_BLOCKED


class RateLimited(AppError):
    code = ErrorCode.RATE_LIMITED


class RetrievalFailed(AppError):
    code = ErrorCode.RETRIEVAL_FAILED


class LLMTimeout(AppError):
    code = ErrorCode.LLM_TIMEOUT
