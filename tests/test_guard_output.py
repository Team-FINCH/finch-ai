"""출력단 Guardrail 검사 테스트.

응답 정책 §5.2의 9개 검사가 각각 통과해야 할 것을 통과시키고, 걸러야 할 것을
걸러내는지 확인한다. 특히 4번(원시 수치)은 한국어 수 표현이 다양해 경계가
흐려지기 쉬우므로, 금지/허용 사례를 넓게 깔아 두었다.
"""

from __future__ import annotations

import pytest

from app.core.enums import MetricSource, Unit, WikiSource
from app.core.schemas import Segment
from app.llm.guard import (
    CheckName,
    Disposition,
    Feature,
    GuardContext,
    NullSemanticClassifier,
    SemanticFinding,
    check_citation_integrity,
    check_engine_values,
    check_forbidden_expression,
    check_length,
    check_raw_number,
    check_schema,
    check_unknown_placeholder,
    check_unsubstituted_placeholder,
    check_wiki_tone,
    render_placeholders,
    run_output_guard,
    split_sentences,
)


def _valid_payload() -> dict[str, object]:
    return {
        "narrative": "반도체 비중이 {{semi_weight}}까지 올라왔습니다[^cit_1].",
        "used_placeholders": ["semi_weight"],
        "used_citations": ["cit_1"],
    }


# ── 보조 함수 ────────────────────────────────────────────────────────────────
def test_split_sentences_keeps_decimal_intact() -> None:
    """소수점을 문장 끝으로 오인하면 길이 검사가 통째로 어긋난다."""
    assert split_sentences("비중이 42.3%입니다. 두 번째 문장입니다.") == [
        "비중이 42.3%입니다.",
        "두 번째 문장입니다.",
    ]


def test_render_placeholders_leaves_unknown_key() -> None:
    """목록 밖 key를 조용히 지우면 2번 검사가 통과해 버린다."""
    rendered = render_placeholders("{{a}}와 {{b}}", {"a": "42.3%"})
    assert rendered == "42.3%와 {{b}}"


# ── 1. 스키마 준수 ───────────────────────────────────────────────────────────
def test_schema_accepts_complete_payload() -> None:
    assert check_schema(_valid_payload()).passed


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"used_placeholders": [], "used_citations": []}, id="필드 누락"),
        pytest.param(
            {"narrative": "", "used_placeholders": [], "used_citations": []},
            id="빈 narrative",
        ),
        pytest.param(
            {"narrative": 42, "used_placeholders": [], "used_citations": []},
            id="narrative 타입 오류",
        ),
        pytest.param(
            {"narrative": "본문", "used_placeholders": "a", "used_citations": []},
            id="배열 아님",
        ),
        pytest.param(
            {"narrative": "본문", "used_placeholders": [1], "used_citations": []},
            id="원소 타입 오류",
        ),
    ],
)
def test_schema_rejects_broken_payload(payload: dict[str, object]) -> None:
    """필수 필드가 없거나 타입이 틀리면 이후 검사를 신뢰할 수 없다."""
    result = check_schema(payload)
    assert not result.passed
    assert result.disposition is Disposition.REGENERATE


# ── 2. 미치환 자리표시자 ─────────────────────────────────────────────────────
def test_unsubstituted_placeholder_accepts_clean_text() -> None:
    assert check_unsubstituted_placeholder("반도체 비중이 42.3%입니다.").passed


@pytest.mark.parametrize(
    "rendered",
    [
        pytest.param("비중이 {{semi_weight}}입니다.", id="온전한 잔재"),
        pytest.param("비중이 {{semi_weight입니다.", id="여는 괄호만"),
        pytest.param("비중이 semi_weight}}입니다.", id="닫는 괄호만"),
    ],
)
def test_unsubstituted_placeholder_rejects_residue(rendered: str) -> None:
    """짝이 깨진 중괄호도 잡아야 사용자 화면에 원본이 새지 않는다."""
    assert not check_unsubstituted_placeholder(rendered).passed


# ── 3. 미허용 자리표시자 ─────────────────────────────────────────────────────
def test_unknown_placeholder_accepts_allowed_key() -> None:
    result = check_unknown_placeholder(
        "비중이 {{semi_weight}}입니다.",
        {"semi_weight"},
        used_placeholders=["semi_weight"],
    )
    assert result.passed


def test_unknown_placeholder_rejects_key_outside_list() -> None:
    """요청마다 허용 목록이 다르므로, 목록 밖 key는 값이 새는 통로가 된다."""
    result = check_unknown_placeholder("비중이 {{secret_pnl}}입니다.", {"semi_weight"})
    assert not result.passed
    assert "secret_pnl" in result.reasons[0]


def test_unknown_placeholder_rejects_declared_only_key() -> None:
    """본문에 안 썼더라도 신고한 key가 목록 밖이면 프롬프트가 어긋난 것이다."""
    result = check_unknown_placeholder(
        "비중이 늘었습니다.", {"semi_weight"}, used_placeholders=["secret_pnl"]
    )
    assert not result.passed


# ── 4. 원시 수치 ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "narrative",
    [
        # 비율
        "반도체 비중이 42.3%입니다.",
        "반도체 비중이 42%입니다.",
        "기여도가 12%p 늘었습니다.",
        "반도체 비중이 42.3 퍼센트입니다.",
        "기여도가 3 퍼센트포인트입니다.",
        "전체의 3분의 1을 차지합니다.",
        # 금액
        "평가금액은 1,200만 원입니다.",
        "평가금액은 1200만원입니다.",
        "평가금액은 3억 원입니다.",
        "시가총액은 5조원입니다.",
        "주당 1,500원에 샀습니다.",
        "주당 $120에 샀습니다.",
        "주당 120달러에 샀습니다.",
        "누적 손익은 3억입니다.",
        # 수량
        "보유 종목은 3개입니다.",
        "보유 수량은 5주입니다.",
        "공시가 2건 있었습니다.",
        "반도체가 3종목입니다.",
        "세 개를 담고 있습니다.",
        "6개월 동안 유지했습니다.",
        "30일간 이어졌습니다.",
        "구독자는 10명입니다.",
        # 배수
        "밸류에이션이 2배입니다.",
        "밸류에이션이 1.5배입니다.",
        "거래량이 두 배입니다.",
        # 단위를 못 붙인 잔재
        "지표는 42.3입니다.",
        "지표는 7입니다.",
    ],
)
def test_raw_number_rejects_forbidden_expressions(narrative: str) -> None:
    """비율·금액·수량·배수는 자리표시자로만 나가야 한다. API 명세 §2.3."""
    result = check_raw_number(narrative)
    assert not result.passed, narrative
    assert result.disposition is Disposition.REGENERATE


@pytest.mark.parametrize(
    "narrative",
    [
        "2026년 실적입니다.",
        "2026년도 계획입니다.",
        "1990년대 이야기입니다.",
        "2025 회계연도 기준입니다.",
        "3분기 실적입니다.",
        "상반기 실적입니다.",
        "1월 15일 공시입니다.",
        "2026년 1월 15일 공시입니다.",
        "첫 번째 이유입니다.",
        "세 번째 이유입니다.",
        "1번째 이유입니다.",
        "둘째 이유입니다.",
        "제2차 회의였습니다.",
        "종목코드는 005930입니다.",
        "코스피 200 편입 종목입니다.",
        "비중이 {{semi_weight}}입니다.",
        "관련 공시가 있었습니다[^cit_1].",
    ],
)
def test_raw_number_accepts_allowed_expressions(narrative: str) -> None:
    """연도·분기·서수·종목코드는 수치가 아니라 식별자다. 막으면 문장이 망가진다."""
    assert check_raw_number(narrative).passed, narrative


# ── 5. 엔진 값 대조 ──────────────────────────────────────────────────────────
def test_engine_values_accepts_exact_match() -> None:
    result = check_engine_values(
        "반도체 비중이 42.3%입니다.",
        {"semi_weight": "42.3%"},
        {"semi_weight": "42.3%"},
    )
    assert result.passed


def test_engine_values_blocks_on_mismatch() -> None:
    """5번만 재생성이 아니라 즉시 차단이다. 파이프라인 고장을 재생성으로 덮으면 안 된다."""
    result = check_engine_values(
        "반도체 비중이 41.0%입니다.",
        {"semi_weight": "41.0%"},
        {"semi_weight": "42.3%"},
    )
    assert not result.passed
    assert result.disposition is Disposition.BLOCK


def test_engine_values_blocks_unknown_key() -> None:
    result = check_engine_values("값입니다.", {"ghost": "42.3%"}, {"semi_weight": "42.3%"})
    assert result.disposition is Disposition.BLOCK


def test_engine_values_blocks_metric_segment_outside_engine_output() -> None:
    """Segment의 수치 조각도 엔진 출력에서 나와야 한다."""
    segments = [
        Segment.text("반도체 비중이 "),
        Segment.metric("41.0%", 0.41, MetricSource.RISK_ENGINE, unit=Unit.RATIO),
        Segment.text("입니다."),
    ]
    result = check_engine_values(
        "반도체 비중이 42.3%입니다.",
        {"semi_weight": "42.3%"},
        {"semi_weight": "42.3%"},
        segments=segments,
    )
    assert result.disposition is Disposition.BLOCK


# ── 6. 근거 무결성 ───────────────────────────────────────────────────────────
def test_citation_integrity_accepts_known_citation() -> None:
    result = check_citation_integrity(
        "관련 공시가 있었습니다[^cit_1].", {"cit_1"}, used_citations=["cit_1"]
    )
    assert result.passed


def test_citation_integrity_rejects_unknown_citation() -> None:
    """없는 근거를 달면 사용자가 확인할 수 없는 주장이 된다."""
    result = check_citation_integrity("공시가 있었습니다[^cit_9].", {"cit_1"})
    assert not result.passed
    assert "cit_9" in result.reasons[0]


def test_citation_integrity_rejects_declared_only_citation() -> None:
    result = check_citation_integrity("공시가 있었습니다.", {"cit_1"}, used_citations=["cit_9"])
    assert not result.passed


# ── 7. 금지 표현 ─────────────────────────────────────────────────────────────
def test_forbidden_expression_accepts_neutral_sentence() -> None:
    result = check_forbidden_expression("반도체 비중이 42.3%까지 올라왔습니다[^cit_1].")
    assert result.passed


@pytest.mark.parametrize(
    "rendered",
    [
        pytest.param("지금이 매수 적기입니다.", id="매매 권유"),
        pytest.param("비중을 늘이세요.", id="매매 지시"),
        pytest.param("목표 주가는 자리표시자로 나갑니다.", id="목표주가"),
        pytest.param("A보다 B가 낫습니다.", id="우열-인접"),
        pytest.param("삼성전자보다 SK하이닉스가 더 매력적입니다.", id="우열-문장"),
        pytest.param("여기서 손절하는 편이 낫습니다.", id="손절 조언"),
        pytest.param("반등 가능성이 높습니다.", id="확률 단정"),
        pytest.param("지금이 기회입니다.", id="감정 유도"),
        pytest.param("크게 걱정하지 않으셔도 됩니다.", id="위험 축소"),
        pytest.param("실적은 개선될 것입니다.", id="미래 단정"),
    ],
)
def test_forbidden_expression_rejects_policy_violations(rendered: str) -> None:
    """§5.1 금지 분류. 정규식 계층이 잡을 수 있는 범위를 확인한다."""
    result = check_forbidden_expression(rendered)
    assert not result.passed, rendered
    assert result.disposition is Disposition.REGENERATE


def test_forbidden_expression_allows_attributed_future_statement() -> None:
    """출처를 밝힌 전망은 우리 주장이 아니므로 통과해야 한다."""
    assert check_forbidden_expression("회사는 실적이 개선될 것이라고 밝혔습니다.").passed


def test_forbidden_expression_rejects_low_confidence_causal() -> None:
    """인과 신뢰도가 낮으면 단정할 수 없다."""
    rendered = "금리 인하 때문에 올랐습니다."
    assert not check_forbidden_expression(rendered, causal_confidence=0.3).passed
    assert check_forbidden_expression(rendered, causal_confidence=0.9).passed


def test_forbidden_expression_blocks_causal_when_confidence_unknown() -> None:
    """모를 때는 막는 쪽으로 기운다. Guardrail의 기본값은 fail-closed여야 한다."""
    assert not check_forbidden_expression("금리 인하 때문에 올랐습니다.").passed


def test_forbidden_expression_delegates_to_semantic_classifier() -> None:
    """정규식에 안 걸리는 완곡한 권유는 의미 검사기가 맡는다. 여기서는 자리만 확인한다."""
    rendered = "지금이 좋은 자리로 보입니다."
    assert check_forbidden_expression(rendered).passed

    class _StubClassifier:
        def classify(self, text: str) -> list[SemanticFinding]:
            return [SemanticFinding(category="매매 권유", excerpt=text)]

    result = check_forbidden_expression(rendered, semantic_classifier=_StubClassifier())
    assert not result.passed
    assert "의미 검사" in result.reasons[0]


def test_null_semantic_classifier_finds_nothing() -> None:
    """의미 검사기가 붙기 전 기본값. 아무것도 잡지 않는다."""
    assert NullSemanticClassifier().classify("지금이 좋은 자리로 보입니다.") == ()


# ── 8. Wiki 어투 ─────────────────────────────────────────────────────────────
def test_wiki_tone_accepts_question_for_ai_inferred() -> None:
    result = check_wiki_tone("배당 성향을 중요하게 보시는 게 맞을까요?", WikiSource.AI_INFERRED)
    assert result.passed


def test_wiki_tone_rejects_assertion_for_ai_inferred() -> None:
    """추론은 사용자가 확인해 줘야 사실이 된다. 단정하면 틀린 사실이 굳는다."""
    result = check_wiki_tone("배당 성향을 중요하게 보십니다.", WikiSource.AI_INFERRED)
    assert not result.passed
    assert result.disposition is Disposition.REGENERATE


@pytest.mark.parametrize(
    "source", [WikiSource.USER_STATED, WikiSource.DERIVED_FROM_TRADES, None]
)
def test_wiki_tone_allows_assertion_for_confirmed_source(source: WikiSource | None) -> None:
    """사용자가 직접 말했거나 거래에서 나온 사실은 단정해도 된다."""
    assert check_wiki_tone("배당 성향을 중요하게 보십니다.", source).passed


# ── 9. 길이 ──────────────────────────────────────────────────────────────────
def test_length_accepts_within_limit() -> None:
    rendered = "첫 문장입니다. 두 번째 문장입니다."
    assert check_length(rendered, Feature.DAILY_BRIEFING_ITEM).passed


def test_length_rejects_over_limit() -> None:
    """기능별 문장 수 상한은 화면 레이아웃이 정한 것이라 넘기면 잘린다."""
    rendered = "첫 문장입니다. 두 번째 문장입니다. 세 번째 문장입니다."
    result = check_length(rendered, Feature.DAILY_BRIEFING_ITEM)
    assert not result.passed
    assert result.disposition is Disposition.REGENERATE


def test_length_rejects_under_limit() -> None:
    """§2 표의 구조(Fact → Impact)를 못 채우면 답변이 반쪽이 된다."""
    assert not check_length("첫 문장입니다.", Feature.DAILY_BRIEFING_ITEM).passed


def test_length_skipped_without_feature() -> None:
    assert check_length("문장 수를 알 수 없습니다.", None).passed


# ── 오케스트레이션 ───────────────────────────────────────────────────────────
def _clean_context() -> GuardContext:
    return GuardContext(
        engine_values={"semi_weight": "42.3%"},
        available_citations={"cit_1"},
        feature=Feature.DAILY_BRIEFING_ITEM,
    )


def _clean_payload() -> dict[str, object]:
    return {
        "narrative": (
            "반도체 비중이 {{semi_weight}}까지 올라왔습니다[^cit_1]. "
            "2026년 들어 가장 높은 수준입니다."
        ),
        "used_placeholders": ["semi_weight"],
        "used_citations": ["cit_1"],
    }


def test_run_output_guard_passes_clean_draft() -> None:
    """9개 검사를 모두 통과하면 치환된 본문을 그대로 내보낼 수 있다."""
    report = run_output_guard(_clean_payload(), _clean_context())
    assert report.passed
    assert report.disposition is None
    assert report.rendered is not None
    assert "42.3%" in report.rendered


def test_run_output_guard_stops_at_first_failure() -> None:
    """앞 검사가 뒤 검사의 전제이므로 기본은 fail-fast다."""
    payload = {"used_placeholders": [], "used_citations": []}
    report = run_output_guard(payload, _clean_context())
    assert report.failed_checks == (CheckName.SCHEMA,)


def test_run_output_guard_collects_all_when_asked() -> None:
    """재생성 프롬프트에 사유를 한 번에 싣고 싶을 때 쓴다."""
    payload = dict(_clean_payload())
    payload["narrative"] = "비중이 42.3%입니다[^cit_9]. 2026년 기준입니다."
    report = run_output_guard(payload, _clean_context(), stop_at_first_failure=False)
    assert CheckName.RAW_NUMBER in report.failed_checks
    assert CheckName.CITATION_INTEGRITY in report.failed_checks


def test_run_output_guard_block_wins_over_regenerate() -> None:
    """차단 사유가 하나라도 있으면 재생성으로 넘겨선 안 된다."""
    payload = dict(_clean_payload())
    payload["narrative"] = "반도체 비중이 {{semi_weight}}까지 올라왔습니다[^cit_1]."
    context = GuardContext(
        engine_values={"semi_weight": "42.3%"},
        available_citations={"cit_1"},
        feature=Feature.DAILY_BRIEFING_ITEM,
        segments=[Segment.metric("41.0%", 0.41, MetricSource.RISK_ENGINE, unit=Unit.RATIO)],
    )
    report = run_output_guard(payload, context, stop_at_first_failure=False)
    assert report.disposition is Disposition.BLOCK
