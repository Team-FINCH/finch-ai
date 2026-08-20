"""새 매수를 골라내고 물어볼 문장을 만든다.

순수 함수다. DB도 스케줄러도 모른다 — 원장과 워터마크를 주면 결과를 돌려줄 뿐이라
테스트가 픽스처 하나로 끝난다. 실제 폴링을 붙이는 건 다른 트랙 몫이다.

논지는 매수 직후에만 솔직하게 나온다. 한 달 뒤에 물으면 사용자는 그 사이 오른 주가를
보고 이유를 지어낸다. 그래서 수집기가 존재한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.core.adapters import Ledger, Trade
from app.core.enums import OrderSide

# 원장이 이미 정리한 소수점 오차를 매도로 오해하지 않을 정도의 여유.
_DUST = 1e-9


def trade_id(trade: Trade, seq: int) -> str:
    """원장 거래에 없는 식별자를 만들어 붙인다.

    워터마크는 문자열 하나만 들고 있으니 사전순 비교만으로 신규 여부가 갈려야 한다.
    ISO 날짜 + 0으로 채운 일련번호면 문자열 순서가 곧 시간 순서가 된다.

    ponytail: 전체 인덱스 기반이라 과거 날짜 거래가 나중에 끼어들면 뒤 id가 밀린다.
    백엔드 거래 조회 API가 열려 진짜 체결 id가 생기면 그걸 그대로 쓰고 이 함수는 지운다.
    """
    return f"{trade.trade_date.isoformat()}#{seq:04d}"


@dataclass(frozen=True, slots=True)
class NewBuy:
    """물어볼 거리 하나."""

    trade_id: str
    trade: Trade
    question: str
    first_position: bool  # 신규 편입이면 True, 추가 매수면 False


@dataclass(frozen=True, slots=True)
class Poll:
    """한 번 훑은 결과."""

    buys: tuple[NewBuy, ...]
    watermark: str | None
    """다음 폴링의 기준선. 매도까지 포함한 마지막 거래 id다.

    매수만 기준으로 잡으면 매수 뒤에 낀 매도를 매번 다시 훑는다.
    """


def question_for(ledger: Ledger, trade: Trade, *, first_position: bool) -> str:
    """매수 성격에 맞는 질문 한 문장.

    신규 편입과 추가 매수는 물어볼 게 다르다. 세 번째 물타기에 "새로 담으셨네요"라고
    물으면 사용자는 이 서비스가 자기 계좌를 안 본다고 결론 내린다.
    """
    name = ledger.instrument(trade.symbol).name
    label = f"{name}({trade.symbol})"
    if first_position:
        return f"{label} 새로 담으셨네요. 어떤 점을 보고 사셨나요?"
    return f"{label} 추가로 사셨네요. 처음 사실 때 생각과 달라진 부분이 있나요?"


def poll(ledger: Ledger, last_trade_id: str | None = None) -> Poll:
    """워터마크 이후의 매수만 골라 질문을 붙인다.

    보유 여부는 워터마크와 무관하게 원장 전체를 훑어 계산한다. 신규 편입인지 추가
    매수인지는 이번에 새로 본 거래가 아니라 그 종목의 전체 역사가 결정하기 때문이다.
    """
    held: dict[str, float] = {}
    buys: list[NewBuy] = []
    watermark = last_trade_id

    for seq, trade in enumerate(_in_order(ledger.trades)):
        tid = trade_id(trade, seq)
        fresh = last_trade_id is None or tid > last_trade_id

        if trade.side is OrderSide.BUY:
            first_position = held.get(trade.symbol, 0.0) <= _DUST
            if fresh:
                buys.append(
                    NewBuy(
                        trade_id=tid,
                        trade=trade,
                        question=question_for(ledger, trade, first_position=first_position),
                        first_position=first_position,
                    )
                )
            held[trade.symbol] = held.get(trade.symbol, 0.0) + trade.quantity
        else:
            held[trade.symbol] = held.get(trade.symbol, 0.0) - trade.quantity

        if fresh:
            watermark = tid

    return Poll(buys=tuple(buys), watermark=watermark)


def _in_order(trades: Sequence[Trade]) -> list[Trade]:
    """날짜순. 같은 날짜는 원장에 적힌 순서를 지킨다(파이썬 정렬은 안정적이다)."""
    return sorted(trades, key=lambda t: t.trade_date)
