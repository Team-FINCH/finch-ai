"""공통 응답 타입.

API 명세 §2.2 · §2.3 · §12를 코드로 옮긴 것이다.

핵심 불변식: `Section.segments`를 이어 붙이면 `Section.text`와 정확히 일치한다.
클라이언트가 어느 쪽을 쓰든 같은 내용을 보게 하려는 것이며,
이 규칙을 타입 수준에서 강제해 조립 단계의 실수를 막는다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import settings
from app.core.enums import (
    CitationType,
    Direction,
    MetricSource,
    SegmentType,
    Unit,
)
from app.core.errors import ErrorCode

KST_OFFSET_HOURS = 9


def now_kst() -> datetime:
    """모든 시각은 KST 오프셋을 명시해 직렬화한다."""
    from datetime import timedelta

    return datetime.now(timezone(timedelta(hours=KST_OFFSET_HOURS)))


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:16]}"


# ── content 모델 공통 ────────────────────────────────────
class ContentModel(BaseModel):
    """기능별 `content` 모델의 바탕.

    `extra="forbid"` 가 핵심이다. 라우터가 넣는 키를 모델이 빠뜨리면 pydantic 은
    조용히 지우고, 응답에서 필드 하나가 사라져도 아무 로그가 남지 않는다. 여기서
    막으면 그 실수가 500 으로 즉시 드러난다 — 프런트가 필드를 잃는 것보다 낫다.
    """

    model_config = ConfigDict(extra="forbid")


# ── 응답 조각 ────────────────────────────────────────────
class Segment(BaseModel):
    """문장을 텍스트와 수치로 쪼갠 조각.

    `type=TEXT`인 조각은 `value`만 가진다. 나머지 필드는 `METRIC`에서만 채운다.
    """

    model_config = ConfigDict(use_enum_values=False)

    type: SegmentType
    value: str
    raw: float | int | None = None
    unit: Unit | None = None
    source: MetricSource | None = None
    direction: Direction | None = None

    @model_validator(mode="after")
    def _check_metric_fields(self) -> Segment:
        if self.type is SegmentType.METRIC:
            if self.raw is None:
                raise ValueError("metric segment에는 raw가 필요하다")
            if self.source is None:
                raise ValueError("metric segment에는 source가 필요하다")
        return self

    @classmethod
    def text(cls, value: str) -> Segment:
        return cls(type=SegmentType.TEXT, value=value)

    @classmethod
    def metric(
        cls,
        value: str,
        raw: float | int,
        source: MetricSource,
        *,
        unit: Unit | None = None,
        direction: Direction | None = None,
    ) -> Segment:
        return cls(
            type=SegmentType.METRIC,
            value=value,
            raw=raw,
            unit=unit,
            source=source,
            direction=direction,
        )


class Section(BaseModel):
    """서술 한 덩어리.

    `text`만 출력해도 정상 동작한다. `segments`는 수치 스타일링이 필요할 때만 쓴다.
    """

    title: str | None = None
    text: str
    segments: list[Segment] = Field(default_factory=list)
    cached: bool = False
    cached_at: datetime | None = None

    @model_validator(mode="after")
    def _segments_reconstruct_text(self) -> Section:
        if not self.segments:
            return self
        joined = "".join(s.value for s in self.segments)
        if joined != self.text:
            raise ValueError(
                f"segments를 이어 붙인 결과가 text와 다르다. text={self.text!r} joined={joined!r}"
            )
        return self

    @classmethod
    def from_segments(
        cls,
        segments: list[Segment],
        *,
        title: str | None = None,
        cached: bool = False,
        cached_at: datetime | None = None,
    ) -> Section:
        """조각에서 Section을 만든다. text는 조각을 이어 붙여 생성하므로 어긋날 수 없다."""
        return cls(
            title=title,
            text="".join(s.value for s in segments),
            segments=segments,
            cached=cached,
            cached_at=cached_at,
        )

    def metrics(self) -> list[Segment]:
        return [s for s in self.segments if s.type is SegmentType.METRIC]


# ── 근거 ─────────────────────────────────────────────────
class Citation(BaseModel):
    id: str
    type: CitationType
    title: str
    source: str
    publisher: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    snippet: str | None = None
    relevance: float | None = None


# ── 응답 봉투 ────────────────────────────────────────────
class DataAsOf(BaseModel):
    """데이터 원천별 기준 시각. UI에 반드시 노출한다.

    시세는 지연될 수 있으므로 생성 시각과 별도로 관리한다.
    """

    price: datetime | None = None
    portfolio: datetime | None = None
    filings: datetime | None = None
    news: datetime | None = None
    macro: datetime | None = None


class FreshnessWarning(BaseModel):
    """사용자가 오래된 원천을 최신 정보로 오해하지 않게 하는 표시."""

    source: Literal["price", "portfolio", "filings", "news", "macro"]
    data_as_of: datetime
    age_seconds: int
    threshold_seconds: int
    message: str


_FRESHNESS_LABELS = {
    "price": "시세",
    "portfolio": "포트폴리오",
    "filings": "공시",
    "news": "뉴스",
    "macro": "거시지표",
}


def _freshness_warnings(data: DataAsOf, generated_at: datetime) -> list[FreshnessWarning]:
    limits = {
        "price": settings.freshness_price_s,
        "portfolio": settings.freshness_portfolio_s,
        "filings": settings.freshness_filings_s,
        "news": settings.freshness_news_s,
        "macro": settings.freshness_macro_s,
    }
    warnings: list[FreshnessWarning] = []
    for source, threshold in limits.items():
        value = getattr(data, source)
        if value is None or threshold <= 0:
            continue
        reference = generated_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=reference.tzinfo)
        age = max(round((reference - value).total_seconds()), 0)
        if age > threshold:
            warnings.append(
                FreshnessWarning(
                    source=source,
                    data_as_of=value,
                    age_seconds=age,
                    threshold_seconds=threshold,
                    message=f"{_FRESHNESS_LABELS[source]} 정보가 평소보다 오래되었습니다.",
                )
            )
    return warnings


class Envelope[ContentT](BaseModel):
    request_id: str = Field(default_factory=new_request_id)
    generated_at: datetime = Field(default_factory=now_kst)
    data_as_of: DataAsOf = Field(default_factory=DataAsOf)
    model: str = Field(default_factory=lambda: settings.llm_model)
    cached: bool = False
    content: ContentT
    citations: list[Citation] = Field(default_factory=list)
    freshness_warnings: list[FreshnessWarning] = Field(default_factory=list)
    disclaimer: str = Field(default_factory=lambda: settings.disclaimer)

    @model_validator(mode="after")
    def derive_freshness_warnings(self) -> Envelope[ContentT]:
        self.freshness_warnings = _freshness_warnings(self.data_as_of, self.generated_at)
        return self


# ── 에러 응답 ────────────────────────────────────────────
class ErrorResponse(BaseModel):
    """실패 응답. 백엔드 API 명세 §1.3 의 형태를 그대로 따른다.

    전에는 `{"error": {...}, "request_id"}` 로 한 겹 감쌌는데, 백엔드는 최상위
    `code`·`message`·`detail` 을 읽어 프런트로 넘긴다. 봉투를 벗기는 쪽이
    백엔드에 매핑 코드를 만들게 하는 것보다 싸므로 우리가 맞춘다.

    `message` 는 **사용자에게 그대로 보여도 되는 한국어 문구**여야 한다.
    백엔드가 화면마다 문구를 다시 만들지 않는 전제다(§1.3).

    `request_id` 는 §1.3 에 없지만 남긴다. `POST /feedback` 이 이 값으로 응답을
    찾으므로, 에러에서만 빠지면 "이 답변 이상해요" 를 응답에 맞출 수 없다.
    성공 봉투와 같은 이름을 쓴다 — 한 개념을 두 이름으로 부르지 않는다.
    """

    code: ErrorCode
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)
    request_id: str = Field(default_factory=new_request_id)
