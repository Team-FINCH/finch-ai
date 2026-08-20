"""데일리 브리핑 랭킹. 산식 §5.2.

    relevance = ŵ_i^0.5 × importance × recency × novelty

제곱근은 의도된 것이다. 비중을 선형으로 쓰면 최대 보유 종목이 네 칸을 전부 가져간다
— 그날 1.5% 움직인 8% 종목이 0.1% 움직인 20% 종목에 밀린다.

이 모듈에는 LLM도 HTTP도 없다. 순위를 모델에 맡기면 비용이 사용자×이벤트로 늘고,
같은 입력이 같은 순서를 준다는 보장이 사라진다. 모델은 확정된 상위 4건의 문장만 쓴다.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from app.core.enums import BriefingCategory, EventType, RateSensitivity
from app.engines.attribution import EventRecord
from app.engines.portfolio import DailyReturn, PortfolioSnapshot

#: §8 — 브리핑은 최대 4건이다.
MAX_ITEMS = 4

#: 등락이 이보다 작으면 항목으로 만들지 않는다.
#: 0.5%로 두면 -0.52% 같은 움직임이 네 칸을 다 채워, 브리핑이 "비중만큼 반영됩니다"
#: 수준의 동어반복으로 메워진다. 국내 대형주 일간 변동성이 대략 1.5~2%이므로 1σ를
#: 문턱으로 잡아 그 아래는 아예 후보로 세우지 않는다 — 알릴 것이 없으면 §8의
#: `empty`가 정답이지, 소음 네 건이 정답이 아니다.
#: ponytail: 시장 변동성이 달라지면 조정할 값이다. 산식이 아니라 눈금이다.
MOVE_THRESHOLD = 0.015

#: 이 등락이면 importance가 1.0으로 포화한다.
#: ponytail: §5.2 표는 이벤트만 다루고 등락은 다루지 않는다. 등락의 중요도는 크기로
#: 본다 — 고정값을 주면 같은 importance끼리 ŵ만 남아 최대 보유 종목이 늘 이긴다.
MOVE_FULL_SCALE = 0.05

#: 섹터 비중이 이만큼(%p) 움직이면 구조 변화로 본다. §5.2.
SECTOR_SHIFT_THRESHOLD = 0.05

#: §5.2 importance 표. 배당·신제품은 "일반 뉴스" 칸에 해당한다.
_TYPE_IMPORTANCE: dict[EventType, float] = {
    EventType.EARNINGS: 1.0,
    EventType.FILING: 0.8,
    EventType.MACRO: 0.6,
    EventType.DIVIDEND: 0.4,
    EventType.PRODUCT: 0.4,
}

#: §5.2의 "거시 이벤트 × rate_sensitivity 계수". moderate가 중립(계수 1.0)이다.
_RATE_COEFFICIENT: dict[RateSensitivity, float] = {
    RateSensitivity.LOW: 0.5,
    RateSensitivity.MODERATE: 1.0,
    RateSensitivity.HIGH: 1.5,
}

#: 화면 분류. BriefingCategory에 뉴스 칸이 없어 배당·신제품도 공시로 묶인다.
_TYPE_CATEGORY: dict[EventType, BriefingCategory] = {
    EventType.EARNINGS: BriefingCategory.EARNINGS,
    EventType.FILING: BriefingCategory.FILING,
    EventType.MACRO: BriefingCategory.MACRO_EVENT,
    EventType.DIVIDEND: BriefingCategory.FILING,
    EventType.PRODUCT: BriefingCategory.FILING,
}

#: 동점일 때의 순서. §8이 나열한 순서를 그대로 쓴다 — 사전순으로 흘려보내면
#: earnings가 holding_move 앞에 오는 이유를 아무도 설명할 수 없다.
_CATEGORY_ORDER: dict[BriefingCategory, int] = {
    category: index
    for index, category in enumerate(
        (
            BriefingCategory.HOLDING_MOVE,
            BriefingCategory.EARNINGS,
            BriefingCategory.FILING,
            BriefingCategory.MACRO_EVENT,
            BriefingCategory.PORTFOLIO_SHIFT,
        )
    )
}

#: 재현성 눈금. 부동소수 꼬리로 순서가 뒤집히는 것을 막는다 — 같은 입력이 같은
#: 순서를 준다는 약속이 랭킹을 코드로 둔 이유의 절반이다.
_TIE_EPSILON = 12

_NOVELTY_SEEN = 0.3
_NOVELTY_NEW = 1.0


@dataclass(frozen=True, slots=True)
class Candidate:
    """후보 한 건. 랭킹에 쓰는 네 값과 화면·문장에 쓰는 부속으로 나뉜다."""

    category: BriefingCategory
    title: str
    #: ŵ_i — 현금을 뺀 주식 합계 대비 비중. 이벤트가 보유와 무관하면 0이 아니라
    #: 아래 `stock_weight` 규칙을 따른다(매크로는 포트폴리오 전체이므로 1.0).
    weight: float
    importance: float
    days_ago: int
    related_tickers: tuple[str, ...] = ()
    deeplink: str | None = None
    #: 문장이 쓸 수 있는 엔진 수치. key는 프롬프트의 자리표시자 이름이 된다.
    values: Mapping[str, float] = field(default_factory=dict)
    #: 모델에게 넘길 엔진 판정 한 줄.
    request: str = ""

    @property
    def key(self) -> str:
        """노출 이력 대조용 항목 식별자.

        응답 payload에 이미 담기는 필드(분류·종목·제목)만으로 만든다. 별도 key를
        응답에 얹으면 §8 계약이 늘어나는데, 그만한 값어치가 없다.
        """
        return item_key(self.category, self.related_tickers, self.title)


@dataclass(frozen=True, slots=True)
class RankedItem:
    rank: int
    relevance: float
    candidate: Candidate


def item_key(
    category: BriefingCategory | str, tickers: Sequence[str], title: str
) -> str:
    """§5.2 novelty가 대조하는 항목 식별자. 응답에서도 같은 규칙으로 복원된다."""
    label = category.value if isinstance(category, BriefingCategory) else category
    return f"{label}|{','.join(tickers)}|{title}"


def relevance(candidate: Candidate, *, seen: bool) -> float:
    """§5.2. 비중은 제곱근, 최신성은 반감기 2일의 지수감쇠."""
    weight = max(candidate.weight, 0.0)
    recency = math.exp(-max(candidate.days_ago, 0) / 2)
    novelty = _NOVELTY_SEEN if seen else _NOVELTY_NEW
    return math.sqrt(weight) * candidate.importance * recency * novelty


def rank(
    candidates: Iterable[Candidate],
    *,
    seen_keys: Iterable[str] = (),
    limit: int = MAX_ITEMS,
) -> list[RankedItem]:
    """상위 `limit`건. 같은 입력이면 같은 순서다.

    동점은 분류 순서 → 항목 key로 끊는다. 둘 다 결정적이라 딕셔너리 순회 순서나
    이벤트 적재 순서가 바뀌어도 결과는 그대로다.
    """
    seen = set(seen_keys)
    scored = [
        (candidate, relevance(candidate, seen=candidate.key in seen))
        for candidate in candidates
    ]
    scored.sort(
        key=lambda pair: (
            -round(pair[1], _TIE_EPSILON),
            _CATEGORY_ORDER[pair[0].category],
            pair[0].key,
        )
    )
    return [
        RankedItem(rank=index, relevance=score, candidate=candidate)
        for index, (candidate, score) in enumerate(scored[:limit], start=1)
        if score > 0.0
    ]


# ── 후보 만들기 ───────────────────────────────────────────────────────────────
def holding_moves(
    snapshot: PortfolioSnapshot,
    row: DailyReturn | None,
    *,
    threshold: float | None = None,
) -> list[Candidate]:
    """보유 종목의 당일 등락(§8 `holding_move`). 이벤트가 없어도 나온다.

    등락은 `daily_returns()` 행에서, ŵ_i는 스냅샷에서 온다. 행의 `weight`는 전일
    총자산 대비라 랭킹의 ŵ와 다르다 — 섞으면 현금 비중만큼 순위가 흔들린다.
    """
    # 기본값을 인자 자리에 박아 두면 def 시점에 굳어 눈금을 바꿀 수 없다. 테스트가
    # 상수를 갈아 끼울 수 있도록 여기서 푼다.
    threshold = MOVE_THRESHOLD if threshold is None else threshold
    if row is None:
        return []
    returns = {c.symbol: c.return_rate for c in row.contributions}

    candidates: list[Candidate] = []
    for holding in snapshot.holdings:
        move = returns.get(holding.symbol)
        if move is None or abs(move) < threshold:
            continue
        direction = "강세" if move > 0 else "약세"
        candidates.append(
            Candidate(
                category=BriefingCategory.HOLDING_MOVE,
                title=f"{holding.name} {direction}",
                weight=holding.stock_weight,
                importance=min(1.0, abs(move) / MOVE_FULL_SCALE),
                days_ago=0,
                related_tickers=(holding.symbol,),
                deeplink=f"/stocks/{holding.symbol}?tab=ai",
                values={"weight": holding.weight, "return_rate": move},
                request=(
                    f"{holding.name}({holding.symbol}) 보유 종목의 당일 등락을 쓰십시오. "
                    f"엔진 판정: {direction}. 업종은 {holding.sector}입니다."
                ),
            )
        )
    return candidates


def sector_shifts(
    before: PortfolioSnapshot,
    after: PortfolioSnapshot,
    *,
    threshold: float = SECTOR_SHIFT_THRESHOLD,
) -> list[Candidate]:
    """업종 비중이 `threshold`(%p) 이상 움직인 구조 변화(§8 `portfolio_shift`)."""
    old, new = _sector_weights(before), _sector_weights(after)

    candidates: list[Candidate] = []
    for sector in sorted(set(old) | set(new)):
        delta = new.get(sector, 0.0) - old.get(sector, 0.0)
        if abs(delta) < threshold:
            continue
        direction = "확대" if delta > 0 else "축소"
        candidates.append(
            Candidate(
                category=BriefingCategory.PORTFOLIO_SHIFT,
                title=f"{sector} 비중 {direction}",
                # 구조 변화는 종목 하나가 아니라 그 업종 전체가 걸린 사건이다.
                weight=new.get(sector, 0.0),
                importance=0.7,
                days_ago=0,
                related_tickers=tuple(
                    h.symbol for h in after.holdings if h.sector == sector
                ),
                deeplink="/portfolio",
                values={"sector_weight": new.get(sector, 0.0), "sector_shift": delta},
                request=(
                    f"{sector} 업종 비중이 {direction}된 포트폴리오 구조 변화를 쓰십시오. "
                    "왜 움직였는지는 확인된 바가 없으니 원인을 지어내지 마십시오."
                ),
            )
        )
    return candidates


def event_candidates(
    events: Iterable[EventRecord],
    snapshot: PortfolioSnapshot,
    as_of: date,
    *,
    rate_sensitivity: RateSensitivity = RateSensitivity.MODERATE,
) -> list[Candidate]:
    """`events` 표에서 오는 실적·공시·거시 항목.

    표가 비어 있으면 빈 목록이다 — 앞의 두 분류만으로도 브리핑은 완성된다.
    """
    holdings = {h.symbol: h for h in snapshot.holdings}

    candidates: list[Candidate] = []
    for event in events:
        try:
            event_type = EventType(event.event_type)
        except ValueError:
            # 모르는 유형은 순위에 끼우지 않는다. 0.4로 뭉뚱그리면 새 유형이
            # 조용히 "일반 뉴스"가 되어 아무도 알아채지 못한다.
            continue
        days_ago = (as_of - event.event_date).days
        if days_ago < 0:
            continue

        holding = holdings.get(event.ticker) if event.ticker else None
        if event.ticker and holding is None:
            continue  # 보유하지 않은 종목의 공시는 이 사용자의 브리핑이 아니다.

        base = _TYPE_IMPORTANCE[event_type]
        if event_type is EventType.MACRO:
            base *= _RATE_COEFFICIENT[rate_sensitivity]
        # 적재기가 매긴 event.importance는 감쇠 계수로만 쓴다. 1.0이면 §5.2 표 그대로다.
        importance = base * min(max(event.importance, 0.0), 1.0)
        if importance <= 0.0:
            continue

        candidates.append(
            Candidate(
                category=_TYPE_CATEGORY[event_type],
                title=event.title,
                # 매크로는 특정 종목이 아니라 포트폴리오 전체에 걸린다.
                weight=holding.stock_weight if holding is not None else 1.0,
                importance=importance,
                days_ago=days_ago,
                related_tickers=(event.ticker,) if event.ticker else (),
                deeplink=(
                    f"/stocks/{event.ticker}?tab=ai" if event.ticker else "/portfolio"
                ),
                values=(
                    {"weight": holding.weight} if holding is not None else {}
                ),
                request=(
                    f"{event.event_date.isoformat()}자 {event_type.value} 이벤트 "
                    f"'{event.title}'를 쓰십시오. 주가와의 인과로 단정하지 마십시오."
                ),
            )
        )
    return candidates


def _sector_weights(snapshot: PortfolioSnapshot) -> dict[str, float]:
    """업종별 ŵ 합계. 현금은 업종이 아니므로 빠진다."""
    weights: dict[str, float] = defaultdict(float)
    for holding in snapshot.holdings:
        weights[holding.sector or "기타"] += holding.stock_weight
    return dict(weights)
