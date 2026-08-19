"""전 계층이 공유하는 열거형.

설계 문서의 값을 그대로 옮긴 것이다. 여기 없는 값을 임의로 만들지 않는다.
API 명세 §12, 엔진 산식 §3.7, 응답 정책 §4.2 참조.
"""

from __future__ import annotations

from enum import StrEnum


# ── 시장 · 종목 ──────────────────────────────────────────
class Market(StrEnum):
    KOSPI = "KOSPI"
    KOSDAQ = "KOSDAQ"


class InstrumentStatus(StrEnum):
    LISTED = "listed"
    SUSPENDED = "suspended"
    DELISTED = "delisted"


# ── 응답 조각 ────────────────────────────────────────────
class SegmentType(StrEnum):
    TEXT = "text"
    METRIC = "metric"


class Unit(StrEnum):
    """비율은 항상 0~1 소수로 전달한다. 백분율 변환은 표시 단계의 몫이다."""

    RATIO = "ratio"
    KRW = "krw"
    COUNT = "count"
    DAYS = "days"
    SCORE = "score"


class Direction(StrEnum):
    """국내 관례에 따라 UP은 적색, DOWN은 청색으로 표시한다."""

    UP = "up"
    DOWN = "down"


class MetricSource(StrEnum):
    """수치가 어느 엔진에서 나왔는지. 출력단 대조 검증의 기준이 된다."""

    PORTFOLIO_ENGINE = "portfolio_engine"
    RISK_ENGINE = "risk_engine"
    ATTRIBUTION_ENGINE = "attribution_engine"
    PRICE = "price"
    FILING = "filing"


# ── 근거 ─────────────────────────────────────────────────
class CitationType(StrEnum):
    FILING = "filing"
    FINANCIAL = "financial"
    NEWS = "news"
    PRICE = "price"
    MACRO = "macro"
    ENGINE = "engine"
    WIKI = "wiki"


class DocumentType(StrEnum):
    FILING = "filing"
    NEWS = "news"
    FINANCIAL = "financial"
    MACRO = "macro"


# ── 위험 ─────────────────────────────────────────────────
class RiskLevel(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class Severity(StrEnum):
    INFO = "info"
    MEDIUM = "medium"
    HIGH = "high"


class FindingCategory(StrEnum):
    """국가 집중도·통화 노출은 국내 단일 시장이므로 정의하지 않는다."""

    CONCENTRATION = "concentration"
    VOLATILITY = "volatility"
    CORRELATION = "correlation"
    STYLE_TILT = "style_tilt"
    MACRO_EXPOSURE = "macro_exposure"
    LIQUIDITY = "liquidity"


class RateSensitivity(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


# ── 이벤트 ───────────────────────────────────────────────
class EventType(StrEnum):
    EARNINGS = "earnings"
    FILING = "filing"
    DIVIDEND = "dividend"
    MACRO = "macro"
    PRODUCT = "product"


class BriefingCategory(StrEnum):
    HOLDING_MOVE = "holding_move"
    EARNINGS = "earnings"
    FILING = "filing"
    MACRO_EVENT = "macro_event"
    PORTFOLIO_SHIFT = "portfolio_shift"


class BriefingStatus(StrEnum):
    READY = "ready"
    GENERATING = "generating"
    EMPTY = "empty"


# ── Wiki ─────────────────────────────────────────────────
class WikiSource(StrEnum):
    """AI_INFERRED 항목은 단정할 수 없다. 질문형 어투만 허용한다.

    응답 정책 §4.2 참조.
    """

    USER_STATED = "user_stated"
    DERIVED_FROM_TRADES = "derived_from_trades"
    AI_INFERRED = "ai_inferred"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ThesisHorizon(StrEnum):
    SHORT = "short"
    MID = "mid"
    LONG = "long"


class ThesisStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


# ── 주문 · 기간 ──────────────────────────────────────────
class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Period(StrEnum):
    D1 = "1d"
    W1 = "1w"
    M1 = "1m"
    M3 = "3m"
    YTD = "ytd"


class Screen(StrEnum):
    """대화에서 대명사 지시 대상을 해소하는 데 쓴다."""

    HOME = "home"
    PORTFOLIO = "portfolio"
    STOCK_DETAIL = "stock_detail"
    ORDER = "order"
    CHAT = "chat"


# ── 피드백 ───────────────────────────────────────────────
class FeedbackRating(StrEnum):
    UP = "up"
    DOWN = "down"


class FeedbackReason(StrEnum):
    """이 분포가 프롬프트 개선의 우선순위를 정한다. 응답 정책 §7.1 참조."""

    WRONG_NUMBER = "wrong_number"
    NOT_RELEVANT = "not_relevant"
    OUTDATED = "outdated"
    TOO_GENERIC = "too_generic"
    UNCLEAR = "unclear"
    WRONG_CITATION = "wrong_citation"
