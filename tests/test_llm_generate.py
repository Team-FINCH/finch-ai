"""섹션 생성 파이프라인 테스트.

실제 호출은 하지 않는다. 가짜 클라이언트가 준비된 payload를 순서대로 돌려주고,
파이프라인이 조립·치환·검사·재생성을 규칙대로 하는지만 본다.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.enums import CitationType, MetricSource, SegmentType, Unit
from app.core.errors import InsufficientData
from app.core.schemas import Citation, Segment
from app.llm.client import LlmResult, NullLlmClient, _to_result
from app.llm.generate import (
    NARRATIVE_SCHEMA,
    build_system,
    build_user_turn,
    citations_from_hits,
    generate_section,
    load_prompt,
    ratio_segment,
    segments_from_narrative,
)
from app.llm.guard import CheckName, CheckResult, GuardReport, Violation


class FakeClient:
    """준비된 payload를 순서대로 내놓는다. 마지막 것은 계속 재사용한다."""

    def __init__(self, *payloads: dict[str, Any]) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []

    async def generate(self, **kwargs: Any) -> LlmResult:
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.payloads) - 1)
        return LlmResult(payload=self.payloads[index], cache_read_tokens=2100)


def _values() -> dict[str, Segment]:
    return {"weight": ratio_segment(0.4168, MetricSource.PORTFOLIO_ENGINE)}


def _citations() -> list[Citation]:
    return [Citation(id="cit_1", type=CitationType.FILING, title="3분기 보고서", source="DART")]


def _ok_payload() -> dict[str, Any]:
    return {
        "narrative": (
            "이 종목은 포트폴리오의 {{weight}}를 차지합니다[^cit_1]. "
            "동일 업황에 함께 노출되어 분산 효과가 제한적입니다. "
            "업황 지표를 함께 확인하는 편이 좋습니다."
        ),
        "used_placeholders": ["weight"],
        "used_citations": ["cit_1"],
    }


# ── 프롬프트는 파일에서 온다 ─────────────────────────────
def test_프롬프트는_파일에서_읽는다():
    assert "역할의 경계" in load_prompt("base_system")
    assert "시장이 주목하는 요인" in load_prompt("stock_analyst")


def test_고정_구간만_캐시_표시를_단다():
    system = build_system("stock_analyst")
    assert len(system) == 2
    assert "cache_control" not in system[0]
    assert system[1]["cache_control"] == {"type": "ephemeral"}


def test_기준_시각은_시스템_블록에_들어가지_않는다():
    """접두가 매 요청 바뀌면 프롬프트 캐시가 통째로 깨진다(§1.2)."""
    joined = "".join(block["text"] for block in build_system("stock_analyst"))
    assert "기준 시각" not in joined
    assert "기준 시각" in build_user_turn("current", values={})


# ── 치환과 조각 ──────────────────────────────────────────
def test_조각을_이어_붙이면_치환_결과와_같다():
    segments = segments_from_narrative("비중은 {{weight}}입니다.", _values())
    assert "".join(s.value for s in segments) == "비중은 41.7%입니다."
    assert [s.type for s in segments] == [
        SegmentType.TEXT,
        SegmentType.METRIC,
        SegmentType.TEXT,
    ]


def test_허용되지_않은_자리표시자는_남긴다():
    """조용히 지우면 미치환 검사가 통과해 버린다."""
    segments = segments_from_narrative("{{unknown}}입니다.", _values())
    assert "".join(s.value for s in segments) == "{{unknown}}입니다."


def test_부호와_방향이_붙는다():
    segment = ratio_segment(-0.1011, MetricSource.PORTFOLIO_ENGINE, signed=True)
    assert segment.value == "-10.1%"
    assert segment.unit is Unit.RATIO
    assert segment.direction is not None
    assert ratio_segment(0.1011, MetricSource.PORTFOLIO_ENGINE, signed=True).value == "+10.1%"


def test_검색_결과가_근거로_바뀐다():
    citations = citations_from_hits(
        [{"title": "반기보고서", "text": "본문", "similarity": 0.9}]
    )
    assert citations[0].id == "cit_1"
    assert citations[0].relevance == 0.9


# ── 생성 ─────────────────────────────────────────────────
async def test_통과하면_섹션이_나온다():
    client = FakeClient(_ok_payload())
    outcome = await generate_section(
        "my_impact",
        client=client,
        title="내 포트폴리오 영향",
        engine_values=_values(),
        citations=_citations(),
    )
    assert outcome.section is not None
    assert outcome.attempts == 1
    assert "41.7%" in outcome.section.text
    assert outcome.section.title == "내 포트폴리오 영향"
    # Section이 강제하는 불변식이 실제로 성립하는지 다시 확인한다.
    assert "".join(s.value for s in outcome.section.segments) == outcome.section.text


async def test_스키마와_호출_파라미터가_넘어간다():
    client = FakeClient(_ok_payload())
    await generate_section("current", client=client, effort="high")
    call = client.calls[0]
    assert call["schema"] == NARRATIVE_SCHEMA
    assert call["effort"] == "high"
    assert "current 섹션을 작성하십시오." in call["user"]


async def test_위반하면_사유를_붙여_재생성한다():
    bad = {
        "narrative": "비중은 {{unknown}}입니다. 두 번째 문장입니다. 세 번째 문장입니다.",
        "used_placeholders": ["unknown"],
        "used_citations": [],
    }
    client = FakeClient(bad, _ok_payload())
    outcome = await generate_section(
        "my_impact", client=client, engine_values=_values(), citations=_citations()
    )
    assert outcome.section is not None
    assert outcome.attempts == 2
    assert "직전 응답 검사 결과" in client.calls[1]["user"]
    assert "unknown" in client.calls[1]["user"]


async def test_두_번_재생성해도_안_되면_포기한다():
    bad = {
        "narrative": "비중은 {{unknown}}입니다. 두 번째 문장입니다. 세 번째 문장입니다.",
        "used_placeholders": ["unknown"],
        "used_citations": [],
    }
    client = FakeClient(bad)
    outcome = await generate_section("my_impact", client=client, engine_values=_values())
    assert outcome.section is None
    assert outcome.blocked
    assert outcome.attempts == 3
    assert len(client.calls) == 3


async def test_차단_판정이면_재생성하지_않는다(monkeypatch):
    """값 불일치는 표현 문제가 아니라 파이프라인 고장이다(§5.2 5번 · §7).

    조립과 치환이 같은 매핑을 쓰므로 정상 경로에서는 값이 어긋날 수 없다.
    그래도 어긋났다면 재시도로 우연히 맞기를 기대하면 안 되므로, 검사기가
    차단을 내렸을 때 루프가 즉시 멈추는지만 본다.
    """
    blocked = GuardReport(
        results=(
            CheckResult(
                check=CheckName.ENGINE_VALUE,
                violations=(
                    Violation(check=CheckName.ENGINE_VALUE, reason="치환 값이 엔진 출력과 다르다"),
                ),
            ),
        )
    )
    monkeypatch.setattr("app.llm.generate.run_output_guard", lambda *a, **k: blocked)

    client = FakeClient(_ok_payload())
    outcome = await generate_section("my_impact", client=client, engine_values=_values())
    assert outcome.section is None
    assert outcome.blocked
    assert len(client.calls) == 1


async def test_원시_수치를_쓰면_통과하지_못한다():
    raw = {
        "narrative": "비중은 41.7%입니다. 두 번째 문장입니다. 세 번째 문장입니다.",
        "used_placeholders": [],
        "used_citations": [],
    }
    outcome = await generate_section(
        "my_impact", client=FakeClient(raw), engine_values=_values()
    )
    assert outcome.section is None


async def test_존재하지_않는_근거를_참조하면_통과하지_못한다():
    payload = _ok_payload() | {"used_citations": ["cit_9"]}
    outcome = await generate_section(
        "my_impact", client=FakeClient(payload), engine_values=_values(), citations=_citations()
    )
    assert outcome.section is None


# ── 근거·클라이언트 경계 ─────────────────────────────────
def test_근거가_없으면_사용자_턴이_한계를_명시한다():
    turn = build_user_turn("risks", values={})
    assert "관련 자료를 찾지 못했다" in turn
    assert "수치를 언급하지 마십시오" in turn


async def test_키가_없으면_명시적으로_실패한다():
    with pytest.raises(InsufficientData):
        await NullLlmClient().generate()


def test_JSON이_아니면_빈_payload로_넘긴다():
    """스키마 검사가 사유를 만들어 재생성 프롬프트에 싣게 한다."""

    class Block:
        type = "text"
        text = "이건 JSON이 아니다"

    class Message:
        content = [Block()]
        usage = None

    assert _to_result(Message()).payload == {}


def test_thinking_블록은_본문에서_제외한다():
    class Thinking:
        type = "thinking"
        text = "속으로 하는 생각"

    class Text:
        type = "text"
        text = '{"narrative": "본문", "used_placeholders": [], "used_citations": []}'

    class Message:
        content = [Thinking(), Text()]
        usage = None

    assert _to_result(Message()).payload["narrative"] == "본문"
