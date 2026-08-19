"""자동 평가 지표.

세 가지만 둔다. 응답 정책 §13의 목록은 열한 개지만 초기에 다 만들면 어느 것도
제대로 못 쓴다. 여기 있는 셋은 LLM 판정 없이 계산되므로 키가 없어도 돌고,
CI에 붙일 수 있다.

    Numerical Accuracy   응답의 수치가 엔진 값과 일치하는가
    Groundedness         주장에 근거가 붙어 있는가
    Portfolio Accuracy   실제 보유 종목·비중을 반영했는가

Retrieval Precision은 임베딩 키가 있어야 하므로 eval/run.py에 둔다.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from app.core.schemas import Section

# 비율·금액·수량·배수. 응답 정책 §2.3의 금지 목록과 같은 경계다.
_FORBIDDEN_NUMBER = re.compile(
    r"""
    \d+(?:[.,]\d+)?\s*%p?          # 42.3% / 0.9%p
  | \d{1,3}(?:,\d{3})+\s*원?        # 1,200,000
  | \d+(?:\.\d+)?\s*(?:억|조|만)\s*원  # 1,200만 원
  | \d+(?:\.\d+)?\s*배              # 2배
  | \d+\s*(?:주|개|종목)             # 3종목
""",
    re.VERBOSE,
)

# 연도·분기·서수·종목코드는 허용한다.
_ALLOWED_NUMBER = re.compile(
    r"\d{4}\s*년|\d\s*분기|\d\s*반기|제?\s*\d+\s*(?:번째|위|차)|\b\d{6}\b"
)

_CITATION = re.compile(r"\[\^cit_[0-9a-zA-Z_]+\]")


@dataclass
class MetricResult:
    name: str
    passed: bool
    score: float
    detail: str = ""
    violations: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        return f"[{mark}] {self.name} {self.score:.3f} {self.detail}"


def _strip_allowed(text: str) -> str:
    """허용 수치를 지운 뒤 남는 숫자만 검사 대상으로 본다."""
    return _ALLOWED_NUMBER.sub(" ", text)


def numerical_accuracy(section: Section, engine_values: dict[str, float]) -> MetricResult:
    """수치가 엔진 값과 일치하는가.

    두 가지를 본다. 문장에 원시 수치가 직접 박혀 있지 않은지, 그리고 metric
    조각의 raw가 엔진이 계산한 값과 같은지.

    LLM은 계산을 시키지 않아도 주어진 숫자를 반올림한다. 18.3%를 "약 18%"로
    바꾸는 식이라, 문자열만 보면 그럴듯해서 놓치기 쉽다.
    """
    violations: list[str] = []

    # metric 조각이 차지한 자리는 정당한 수치이므로 검사에서 뺀다.
    remainder = section.text
    for seg in section.metrics():
        remainder = remainder.replace(seg.value, " ", 1)

    for m in _FORBIDDEN_NUMBER.finditer(_strip_allowed(remainder)):
        violations.append(f"문장에 원시 수치: {m.group(0).strip()!r}")

    checked = 0
    for seg in section.metrics():
        if seg.source is None or seg.raw is None:
            violations.append(f"metric에 source/raw 누락: {seg.value!r}")
            continue
        key = f"{seg.source.value}:{seg.value}"
        expected = engine_values.get(seg.value) if engine_values else None
        if expected is None:
            continue
        checked += 1
        if not math.isclose(float(seg.raw), expected, rel_tol=1e-9, abs_tol=1e-12):
            violations.append(f"엔진 값 불일치 {key}: 기대 {expected} 실제 {seg.raw}")

    total = max(len(section.metrics()), 1)
    score = max(0.0, 1.0 - len(violations) / total)
    return MetricResult(
        "numerical_accuracy",
        not violations,
        score,
        f"metric {len(section.metrics())}개 · 엔진 대조 {checked}개",
        violations,
    )


def groundedness(section: Section, available_citations: set[str]) -> MetricResult:
    """주장에 근거가 붙어 있는가.

    문장 단위로 본다. 근거가 필요 없는 문장(엔진 수치만 말하는 문장)까지
    강제하면 과탐이 되므로, metric 조각을 포함한 문장은 면제한다.
    """
    violations: list[str] = []
    sentences = [s.strip() for s in re.split(r"(?<=다\.)\s+", section.text) if s.strip()]
    if not sentences:
        return MetricResult("groundedness", True, 1.0, "문장 없음")

    metric_values = {seg.value for seg in section.metrics()}
    need = have = 0
    for s in sentences:
        cites = _CITATION.findall(s)
        for c in cites:
            cid = c[2:-1]
            if cid not in available_citations:
                violations.append(f"존재하지 않는 근거 참조: {cid}")
        # 엔진 수치만 말하는 문장은 외부 근거가 필요 없다.
        if any(v in s for v in metric_values):
            continue
        need += 1
        if cites:
            have += 1

    score = 1.0 if need == 0 else have / need
    ok = not violations and score >= 0.5
    return MetricResult(
        "groundedness", ok, score, f"근거 필요 {need}문장 중 {have}문장", violations
    )


def portfolio_accuracy(
    section: Section, holdings: dict[str, float], *, tolerance: float = 1e-6
) -> MetricResult:
    """실제 보유 종목·비중을 반영했는가.

    응답에 등장한 비중 조각이 실제 포트폴리오와 맞는지 본다. 개인화가
    작동하는지를 보는 지표라, 보유하지 않은 종목을 보유한 것처럼 말하는
    경우도 여기서 잡는다.
    """
    violations: list[str] = []
    checked = 0

    for seg in section.metrics():
        if seg.unit is None or seg.unit.value != "ratio" or seg.raw is None:
            continue
        matched = [t for t, w in holdings.items() if math.isclose(float(seg.raw), w, abs_tol=tolerance)]
        if matched:
            checked += 1
            continue
        # 비중처럼 보이는데 어느 보유 종목과도 맞지 않는다.
        if 0.0 < float(seg.raw) <= 1.0:
            violations.append(f"보유 비중과 맞지 않는 값: {seg.value} ({seg.raw})")

    total = max(checked + len(violations), 1)
    score = checked / total
    return MetricResult(
        "portfolio_accuracy",
        not violations,
        score,
        f"비중 조각 {checked}개 일치",
        violations,
    )


def evaluate_section(
    section: Section,
    *,
    engine_values: dict[str, float] | None = None,
    citations: set[str] | None = None,
    holdings: dict[str, float] | None = None,
) -> list[MetricResult]:
    """세 지표를 한 번에 돌린다. 입력이 없는 지표는 건너뛴다."""
    out = [numerical_accuracy(section, engine_values or {})]
    if citations is not None:
        out.append(groundedness(section, citations))
    if holdings is not None:
        out.append(portfolio_accuracy(section, holdings))
    return out
