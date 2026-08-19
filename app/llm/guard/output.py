"""LLM 출력단 Guardrail.

응답 정책 §5.2의 9개 검사를 그대로 옮긴 것이다. 문자열을 받아 판정만 돌려주는
순수 함수 모음이며, 네트워크도 DB도 건드리지 않는다.

검사는 §5.2가 정한 순서대로 돈다. 앞 검사가 뒤 검사의 전제가 되기 때문이다.
자리표시자가 목록 밖 key(3번)면 치환 자체를 신뢰할 수 없고, 치환이 끝나지
않았으면(2번) 엔진 값 대조(5번)가 의미를 잃는다. 그래서 기본값은 fail-fast다.

실패는 두 종류로 갈린다.

- ``Disposition.REGENERATE`` — 표현 문제다. 위반 항목을 프롬프트에 명시하고 다시 만든다.
- ``Disposition.BLOCK`` — 5번(엔진 값 대조) 전용이다. 값이 어긋났다는 것은 파이프라인
  어딘가가 고장났다는 뜻이므로, 다시 생성해서 우연히 맞기를 기대하면 안 된다.
  즉시 ``GUARDRAIL_BLOCKED``로 처리한다.

7번(금지 표현)은 여기서 정규식 계층만 구현한다. "지금이 좋은 자리로 보입니다"처럼
금지어에 걸리지 않는 매수 권유는 정규식으로 잡히지 않으므로, 의미 검사를 붙일
자리를 :class:`SemanticClassifier` 프로토콜로 남겨 두었다. 실제 LLM 분류 호출은
이 모듈의 책임이 아니다.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.core.enums import SegmentType, WikiSource
from app.core.schemas import Segment

__all__ = [
    "CheckName",
    "Disposition",
    "Feature",
    "GuardContext",
    "GuardReport",
    "CheckResult",
    "Violation",
    "SemanticClassifier",
    "SemanticFinding",
    "LlmDraft",
    "SENTENCE_LIMITS",
    "CAUSAL_CONFIDENCE_MIN",
    "REQUIRED_FIELDS",
    "split_sentences",
    "render_placeholders",
    "check_schema",
    "check_unsubstituted_placeholder",
    "check_unknown_placeholder",
    "check_raw_number",
    "check_engine_values",
    "check_citation_integrity",
    "check_forbidden_expression",
    "check_wiki_tone",
    "check_length",
    "run_output_guard",
]


# ── 판정 타입 ────────────────────────────────────────────
class Disposition(StrEnum):
    """실패했을 때 무엇을 할지. 응답 정책 §5.2 · §7."""

    REGENERATE = "regenerate"
    BLOCK = "block"


class CheckName(StrEnum):
    """§5.2 표의 9개 검사. 값은 재생성 프롬프트에 그대로 실린다."""

    SCHEMA = "schema"
    UNSUBSTITUTED_PLACEHOLDER = "unsubstituted_placeholder"
    UNKNOWN_PLACEHOLDER = "unknown_placeholder"
    RAW_NUMBER = "raw_number"
    ENGINE_VALUE = "engine_value"
    CITATION_INTEGRITY = "citation_integrity"
    FORBIDDEN_EXPRESSION = "forbidden_expression"
    WIKI_TONE = "wiki_tone"
    LENGTH = "length"


#: §5.2 표의 "실패 시" 열. 5번만 차단이고 나머지는 재생성이다.
DISPOSITION_BY_CHECK: dict[CheckName, Disposition] = {
    CheckName.SCHEMA: Disposition.REGENERATE,
    CheckName.UNSUBSTITUTED_PLACEHOLDER: Disposition.REGENERATE,
    CheckName.UNKNOWN_PLACEHOLDER: Disposition.REGENERATE,
    CheckName.RAW_NUMBER: Disposition.REGENERATE,
    CheckName.ENGINE_VALUE: Disposition.BLOCK,
    CheckName.CITATION_INTEGRITY: Disposition.REGENERATE,
    CheckName.FORBIDDEN_EXPRESSION: Disposition.REGENERATE,
    CheckName.WIKI_TONE: Disposition.REGENERATE,
    CheckName.LENGTH: Disposition.REGENERATE,
}

#: §5.2 표의 검사 순서. 앞 검사가 뒤 검사의 전제다.
CHECK_ORDER: tuple[CheckName, ...] = (
    CheckName.SCHEMA,
    CheckName.UNSUBSTITUTED_PLACEHOLDER,
    CheckName.UNKNOWN_PLACEHOLDER,
    CheckName.RAW_NUMBER,
    CheckName.ENGINE_VALUE,
    CheckName.CITATION_INTEGRITY,
    CheckName.FORBIDDEN_EXPRESSION,
    CheckName.WIKI_TONE,
    CheckName.LENGTH,
)


@dataclass(frozen=True, slots=True)
class Violation:
    """검사 하나가 잡아낸 위반 한 건."""

    check: CheckName
    reason: str
    evidence: str | None = None

    @property
    def disposition(self) -> Disposition:
        return DISPOSITION_BY_CHECK[self.check]

    def __str__(self) -> str:
        if self.evidence is None:
            return f"[{self.check}] {self.reason}"
        return f"[{self.check}] {self.reason}: {self.evidence!r}"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """검사 하나의 결과. 통과 여부와 사유, 실패 시 재생성/차단 구분을 담는다."""

    check: CheckName
    violations: tuple[Violation, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.violations

    @property
    def disposition(self) -> Disposition | None:
        """통과했으면 ``None``. 실패했으면 재생성인지 차단인지."""
        return None if self.passed else DISPOSITION_BY_CHECK[self.check]

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(str(v) for v in self.violations)


def _ok(check: CheckName) -> CheckResult:
    return CheckResult(check=check)


def _fail(check: CheckName, *reasons: tuple[str, str | None]) -> CheckResult:
    return CheckResult(
        check=check,
        violations=tuple(
            Violation(check=check, reason=reason, evidence=evidence)
            for reason, evidence in reasons
        ),
    )


# ── 의미 검사 자리 (7번) ─────────────────────────────────
@dataclass(frozen=True, slots=True)
class SemanticFinding:
    """의미 검사가 잡아낸 항목. 정규식으로는 걸리지 않는 완곡한 권유 등."""

    category: str
    excerpt: str


@runtime_checkable
class SemanticClassifier(Protocol):
    """금지 표현 의미 검사기의 계약.

    "지금이 좋은 자리로 보입니다"는 §5.1 금지어 목록 어디에도 걸리지 않지만
    명백한 매수 권유다. 정규식 계층이 통과시킨 문장을 경량 LLM 분류 호출에
    태우는 자리이며, 실제 호출 구현은 이 모듈의 범위 밖이다.
    """

    def classify(self, text: str) -> Sequence[SemanticFinding]:
        """금지 범주에 해당하는 부분을 돌려준다. 없으면 빈 시퀀스."""


class NullSemanticClassifier:
    """아무것도 잡지 않는 기본 구현. 의미 검사기가 붙기 전까지 쓴다."""

    def classify(self, text: str) -> Sequence[SemanticFinding]:  # noqa: ARG002
        return ()


# ── 기능별 문장 수 (9번) ─────────────────────────────────
class Feature(StrEnum):
    """응답 정책 §2 답변 구조 표준의 기능 구분."""

    DAILY_BRIEFING_ITEM = "daily_briefing_item"
    STOCK_ANALYST_SECTION = "stock_analyst_section"
    THESIS_CHECK = "thesis_check"
    PORTFOLIO_DOCTOR_FINDING = "portfolio_doctor_finding"
    WHY_MOVED = "why_moved"
    BEFORE_YOU_TRADE = "before_you_trade"
    ASK_MY_PORTFOLIO = "ask_my_portfolio"


#: (최소, 최대) 문장 수. §2 표의 "문장" 열을 그대로 옮겼다.
#: 표가 정한 구조(Fact → Impact 등)가 곧 최소 문장 수이므로 하한도 함께 본다.
SENTENCE_LIMITS: dict[Feature, tuple[int, int]] = {
    Feature.DAILY_BRIEFING_ITEM: (2, 2),
    Feature.STOCK_ANALYST_SECTION: (3, 4),
    Feature.THESIS_CHECK: (4, 4),
    Feature.PORTFOLIO_DOCTOR_FINDING: (3, 3),
    Feature.WHY_MOVED: (3, 4),
    Feature.BEFORE_YOU_TRADE: (2, 3),
    Feature.ASK_MY_PORTFOLIO: (1, 6),
}


# ── 공통 정규식 ──────────────────────────────────────────
#: `{{key}}` 형태의 정상 자리표시자.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")
#: 짝이 깨진 것까지 포함한 중괄호 잔재. 2번 검사용.
_BRACE_RESIDUE_RE = re.compile(r"\{\{[^{}]*\}\}|\{\{|\}\}")
#: `[^cit_1]` 형태의 근거 참조.
_CITATION_RE = re.compile(r"\[\^(cit_[A-Za-z0-9_-]+)\]")
#: 소수점을 문장 끝으로 오인하지 않도록 숫자 뒤 마침표는 제외한다.
_SENTENCE_END_RE = re.compile(r"(?<!\d)[.!?]+(?=\s|$)")


def split_sentences(text: str) -> list[str]:
    """문장 단위로 자른다. 종결부호를 붙인 채로 돌려준다."""
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(text):
        chunk = text[start : match.end()].strip()
        if chunk:
            sentences.append(chunk)
        start = match.end()
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def render_placeholders(narrative: str, values: Mapping[str, str]) -> str:
    """`{{key}}`를 엔진 값으로 치환한다. 목록에 없는 key는 그대로 남긴다.

    남겨 두는 것이 핵심이다. 조용히 지우면 2번 검사가 통과해 버린다.
    """
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), narrative)


# ── 1. 스키마 준수 ───────────────────────────────────────
#: Structured Output 필수 필드. 응답 정책 §3.
REQUIRED_FIELDS: tuple[str, ...] = ("narrative", "used_placeholders", "used_citations")


@dataclass(frozen=True, slots=True)
class LlmDraft:
    """스키마 검사를 통과한 LLM 원본 출력."""

    narrative: str
    used_placeholders: tuple[str, ...] = ()
    used_citations: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> LlmDraft:
        return cls(
            narrative=payload["narrative"],
            used_placeholders=tuple(payload.get("used_placeholders") or ()),
            used_citations=tuple(payload.get("used_citations") or ()),
        )


def check_schema(payload: Mapping[str, Any]) -> CheckResult:
    """1. 스키마 준수 — 필수 필드가 있고 타입이 맞는지."""
    check = CheckName.SCHEMA
    problems: list[tuple[str, str | None]] = []

    for name in REQUIRED_FIELDS:
        if name not in payload:
            problems.append(("필수 필드가 없다", name))

    narrative = payload.get("narrative")
    if "narrative" in payload:
        if not isinstance(narrative, str):
            problems.append(("narrative는 문자열이어야 한다", type(narrative).__name__))
        elif not narrative.strip():
            problems.append(("narrative가 비어 있다", None))

    for name in ("used_placeholders", "used_citations"):
        if name not in payload:
            continue
        value = payload[name]
        if not isinstance(value, (list, tuple)):
            problems.append((f"{name}는 배열이어야 한다", type(value).__name__))
        elif any(not isinstance(item, str) for item in value):
            problems.append((f"{name} 원소는 문자열이어야 한다", repr(value)))

    return _fail(check, *problems) if problems else _ok(check)


# ── 2. 미치환 자리표시자 ─────────────────────────────────
def check_unsubstituted_placeholder(rendered: str) -> CheckResult:
    """2. 치환이 끝난 문장에 `{{ }}`가 남아 있으면 안 된다."""
    check = CheckName.UNSUBSTITUTED_PLACEHOLDER
    residues = _BRACE_RESIDUE_RE.findall(rendered)
    if not residues:
        return _ok(check)
    return _fail(
        check,
        *(("치환되지 않은 자리표시자가 남아 있다", residue) for residue in residues),
    )


# ── 3. 미허용 자리표시자 ─────────────────────────────────
def check_unknown_placeholder(
    narrative: str,
    allowed_keys: Collection[str],
    *,
    used_placeholders: Sequence[str] = (),
) -> CheckResult:
    """3. 요청마다 주어진 목록 밖의 key를 쓰면 안 된다.

    본문에 실제로 박힌 key와 모델이 스스로 신고한 ``used_placeholders``를 모두 본다.
    신고만 하고 쓰지 않은 key는 위반이 아니다. 값이 새어 나가지 않기 때문이다.
    """
    check = CheckName.UNKNOWN_PLACEHOLDER
    allowed = set(allowed_keys)
    problems: list[tuple[str, str | None]] = []

    for key in _PLACEHOLDER_RE.findall(narrative):
        if key not in allowed:
            problems.append(("허용 목록에 없는 자리표시자를 썼다", key))
    for key in used_placeholders:
        if key not in allowed:
            problems.append(("used_placeholders에 허용되지 않은 key가 있다", key))

    return _fail(check, *problems) if problems else _ok(check)


# ── 4. 원시 수치 ─────────────────────────────────────────
_NOISE = "〔〕"

#: 자리표시자 없이 그대로 써도 되는 표기. 응답 정책 §1.1 · API 명세 §2.3.
#: 연도·분기·서수·종목코드가 문서가 명시한 허용 목록이고,
#: 날짜(월·일)와 지수 이름은 금지 목록(비율·금액·수량·배수) 어디에도 속하지 않는
#: 시점·고유명사 표기이므로 함께 허용한다.
_ALLOWED_NUMERIC: tuple[re.Pattern[str], ...] = (
    re.compile(r"\d{4}\s*회계연도"),
    re.compile(r"\d{4}\s*년(?:도|대)?"),
    re.compile(r"\d\s*분기"),
    re.compile(r"[상하]반기"),
    re.compile(r"\d{1,2}\s*월(?:\s*\d{1,2}\s*일)?"),
    re.compile(r"제?\s*\d+\s*(?:번째|차례째|차|째)"),
    re.compile(r"(?:첫|두|둘|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)\s*번째"),
    re.compile(r"(?:첫|둘|셋|넷|다섯|여섯|일곱|여덟|아홉|열)째"),
    re.compile(r"(?:KOSPI|코스피)\s*200|(?:KOSDAQ|코스닥)\s*150|S&P\s*500|나스닥\s*100"),
    # 종목코드. 뒤에 단위가 붙으면 코드가 아니라 수치다.
    re.compile(r"(?<!\d)\d{6}(?!\d)(?!\s*(?:원|주|개|건|명|배|%|퍼센트))"),
)

#: 반드시 자리표시자로 써야 하는 표기. 금지 사유를 함께 둔다.
_FORBIDDEN_NUMERIC: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("비율", re.compile(r"[\d,]+(?:\.\d+)?\s*%\s*(?:p|포인트)?")),
    ("비율", re.compile(r"[\d,]+(?:\.\d+)?\s*퍼센트(?:\s*포인트)?")),
    ("비율", re.compile(r"\d+\s*분의\s*\d+")),
    ("금액", re.compile(r"\$\s*[\d,]+(?:\.\d+)?")),
    ("금액", re.compile(r"[\d,]+(?:\.\d+)?\s*(?:조|억|만|천|백)?\s*원")),
    ("금액", re.compile(r"[\d,]+(?:\.\d+)?\s*(?:달러|엔|위안|유로)")),
    ("금액", re.compile(r"[\d,]+(?:\.\d+)?\s*(?:조|억|만)(?!\s*원)")),
    ("수량", re.compile(r"[\d,]+\s*(?:개월|일간|일\s*동안|주간|년간|년\s*동안|일째)")),
    ("수량", re.compile(r"[\d,]+\s*(?:개|종목|주|건|곳|명|사|가지|차례|회|칸|판)")),
    ("배수", re.compile(r"[\d,]+(?:\.\d+)?\s*배")),
    ("배수", re.compile(r"(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*배")),
    (
        "수량",
        re.compile(
            r"(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*(?:개|종목|주|건|곳|명|가지)"
        ),
    ),
)

_LEFTOVER_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def check_raw_number(narrative: str) -> CheckResult:
    """4. 비율·금액·수량·배수를 직접 적으면 안 된다.

    치환 전 원문에 대고 돌린다. 치환 뒤에는 엔진이 넣은 진짜 수치가 정상적으로
    등장하므로, 이 검사는 LLM이 스스로 수를 적었는지만 볼 수 있는 자리에서 돌아야 한다.

    허용 목록(연도·분기·서수·종목코드·날짜·지수명)을 먼저 지우고 남은 숫자를 본다.
    단위를 알 수 없는 숫자도 위반으로 본다. §3이 "반올림한 근사값도 허용되지
    않습니다"라고 못 박았으므로, 판단이 서지 않을 때는 막는 쪽이 규약에 맞다.
    """
    check = CheckName.RAW_NUMBER
    work = _PLACEHOLDER_RE.sub(_NOISE, narrative)
    work = _BRACE_RESIDUE_RE.sub(_NOISE, work)
    work = _CITATION_RE.sub(_NOISE, work)
    for pattern in _ALLOWED_NUMERIC:
        work = pattern.sub(_NOISE, work)

    problems: list[tuple[str, str | None]] = []
    for label, pattern in _FORBIDDEN_NUMERIC:
        matches = pattern.findall(work)
        if not matches:
            continue
        for found in pattern.finditer(work):
            problems.append((f"{label}는 자리표시자로 써야 한다", found.group(0).strip()))
        work = pattern.sub(_NOISE, work)

    for found in _LEFTOVER_NUMBER_RE.finditer(work):
        problems.append(("단위를 확인할 수 없는 원시 수치다", found.group(0)))

    return _fail(check, *problems) if problems else _ok(check)


# ── 5. 엔진 값 대조 ──────────────────────────────────────
def check_engine_values(
    rendered: str,
    substitutions: Mapping[str, str],
    engine_values: Mapping[str, str],
    *,
    segments: Sequence[Segment] | None = None,
) -> CheckResult:
    """5. 치환 결과가 엔진 출력과 한 글자도 다르지 않은지.

    이 검사만 실패 시 재생성이 아니라 즉시 차단이다. 값이 어긋났다는 것은
    프롬프트가 아니라 파이프라인이 고장났다는 뜻이라, 다시 생성해 우연히 맞기를
    기대하면 틀린 수치를 내보낼 위험만 커진다. 응답 정책 §5.2 · §7.
    """
    check = CheckName.ENGINE_VALUE
    problems: list[tuple[str, str | None]] = []

    for key, value in substitutions.items():
        if key not in engine_values:
            problems.append(("엔진이 내지 않은 자리표시자를 치환했다", key))
            continue
        expected = engine_values[key]
        if value != expected:
            problems.append(
                ("치환 값이 엔진 출력과 다르다", f"{key}: {value!r} != {expected!r}")
            )
        elif expected not in rendered:
            problems.append(("치환한 값이 본문에 없다", f"{key}: {expected!r}"))

    if segments is not None:
        known = set(engine_values.values())
        for segment in segments:
            if segment.type is SegmentType.METRIC and segment.value not in known:
                problems.append(("엔진 출력에 없는 수치 조각이다", segment.value))

    return _fail(check, *problems) if problems else _ok(check)


# ── 6. 근거 무결성 ───────────────────────────────────────
def check_citation_integrity(
    rendered: str,
    available_citations: Collection[str],
    *,
    used_citations: Sequence[str] = (),
) -> CheckResult:
    """6. 존재하지 않는 cit_id를 참조하면 안 된다."""
    check = CheckName.CITATION_INTEGRITY
    available = set(available_citations)
    problems: list[tuple[str, str | None]] = []

    for cit_id in _CITATION_RE.findall(rendered):
        if cit_id not in available:
            problems.append(("존재하지 않는 근거를 참조했다", cit_id))
    for cit_id in used_citations:
        if cit_id not in available:
            problems.append(("used_citations에 존재하지 않는 근거가 있다", cit_id))

    return _fail(check, *problems) if problems else _ok(check)


# ── 7. 금지 표현 ─────────────────────────────────────────
#: 응답 정책 §5.1 금지 표현. 문장 단위 판단이 필요한 두 항목은 아래에서 따로 본다.
_FORBIDDEN_EXPRESSIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "매매 권유",
        re.compile(
            r"매수\s*적기|매도\s*적기|매수\s*타이밍|매수\s*시점입니다"
            r"|비중을\s*(?:늘|줄)이(?:세요|시|십시오)"
            r"|담으(?:세요|시|십시오)|사(?:세요|십시오)|파(?:세요|십시오)"
            r"|매수하(?:세요|시|십시오)|매도하(?:세요|시|십시오)"
            r"|추천(?:합니다|드립니다|드려요)"
        ),
    ),
    (
        "목표주가",
        re.compile(r"목표\s*주?가|적정\s*주가|상승\s*여력|하락\s*여력"),
    ),
    (
        "종목 간 우열",
        re.compile(r"보다\s*(?:더\s*)?(?:낫|나은|우수|매력|유리|좋)|우위에\s*있|열위"),
    ),
    (
        "손절·익절 조언",
        re.compile(
            r"손절|익절|정리하시는\s*(?:편|것)|이익\s*(?:을\s*)?실현하(?:세요|시)"
            r"|손실을\s*확정"
        ),
    ),
    (
        "확률 단정",
        re.compile(r"가능성이\s*(?:높|큽|큰)|확률이\s*(?:높|큽)|유력합니다|틀림없"),
    ),
    (
        "감정 유도",
        re.compile(r"놓치면\s*후회|서두르|지금이\s*기회|늦기\s*전에|기회를\s*놓치"),
    ),
    (
        "위험 축소",
        re.compile(
            r"크게\s*걱정|걱정하지\s*않으셔도|걱정하실\s*필요|안심하(?:셔도|시)"
            r"|우려할\s*필요(?:는)?\s*없"
        ),
    ),
)

#: AI가 자기 목소리로 미래를 단정하는 어미. §4.1이 허용한 `~로 보입니다`는 뺀다.
_FUTURE_ASSERTION_RE = re.compile(
    r"것입니다|겁니다|될\s*전망입니다|예상됩니다|전망됩니다|확실합니다"
)
#: 주어를 출처로 바꾸면 같은 문장이 사실 전달이 된다. §5.1 미래 단정 항목.
_ATTRIBUTION_RE = re.compile(
    r"에\s*따르면|회사는|사측은|당국은|발표했|밝혔|전망했|제시했|공시했|보고했|\[\^cit_"
)
#: 인과 단정. 신뢰도가 낮으면 같은 날 있었던 사실만 적어야 한다.
_CAUSAL_RE = re.compile(r"때문에|덕분에|(?:으로|로)\s*인해|영향으로\s*(?:올랐|내렸|상승|하락)")
#: §5.1이 인과 단정을 금지하는 신뢰도 경계.
CAUSAL_CONFIDENCE_MIN = 0.6

# 종목 간 우열은 인접 정규식만으로 잡히지 않는다. "A보다 B가 낫습니다"처럼
# 비교 대상과 우열 표현 사이에 다른 말이 끼어들기 때문이다. 문장 단위로
# 비교 표지와 우열 어휘가 함께 나오면 위반으로 본다.
_COMPARATIVE_RE = re.compile(r"[가-힣A-Za-z0-9)\]]\s*보다")
_SUPERIORITY_RE = re.compile(r"낫|나은|우수|매력적|유리|좋|앞선|앞섭|뛰어난")


def check_forbidden_expression(
    rendered: str,
    *,
    causal_confidence: float | None = None,
    semantic_classifier: SemanticClassifier | None = None,
) -> CheckResult:
    """7. §5.1 금지 표현 분류.

    정규식 계층이다. 완곡한 권유("지금이 좋은 자리로 보입니다")는 표현이 아니라
    의미를 봐야 잡히므로 ``semantic_classifier``에 위임한다. 넘기지 않으면 의미
    검사는 건너뛴다.

    ``causal_confidence``를 넘기지 않으면 인과 신뢰도를 모른다는 뜻이므로 단정을
    막는다. Guardrail은 모를 때 통과시키는 쪽이 아니라 막는 쪽으로 기울어야 한다.
    """
    check = CheckName.FORBIDDEN_EXPRESSION
    problems: list[tuple[str, str | None]] = []

    for label, pattern in _FORBIDDEN_EXPRESSIONS:
        for found in pattern.finditer(rendered):
            problems.append((f"{label} 표현이다", found.group(0)))

    for sentence in split_sentences(rendered):
        if _COMPARATIVE_RE.search(sentence) and _SUPERIORITY_RE.search(sentence):
            problems.append(("종목 간 우열 표현이다", sentence))
        attributed = _ATTRIBUTION_RE.search(sentence) is not None
        if _FUTURE_ASSERTION_RE.search(sentence) and not attributed:
            problems.append(("미래 단정이다. 주어를 출처로 바꿔야 한다", sentence))
        if _CAUSAL_RE.search(sentence) and (
            causal_confidence is None or causal_confidence < CAUSAL_CONFIDENCE_MIN
        ):
            problems.append(("신뢰도가 낮은 인과 단정이다", sentence))

    if semantic_classifier is not None:
        for finding in semantic_classifier.classify(rendered):
            problems.append((f"{finding.category} 표현이다(의미 검사)", finding.excerpt))

    return _fail(check, *problems) if problems else _ok(check)


# ── 8. Wiki 어투 ─────────────────────────────────────────
def check_wiki_tone(rendered: str, source: WikiSource | None) -> CheckResult:
    """8. ``ai_inferred`` 항목은 단정할 수 없다. 질문형만 허용한다.

    응답 정책 §4.2. 추론은 사용자가 확인해 줘야 사실이 되므로, 되묻지 않고
    단정하면 Wiki에 틀린 사실이 굳어 버린다.
    """
    check = CheckName.WIKI_TONE
    if source is not WikiSource.AI_INFERRED:
        return _ok(check)

    problems: list[tuple[str, str | None]] = []
    for sentence in split_sentences(rendered):
        if not sentence.endswith("?"):
            problems.append(("ai_inferred 항목은 질문형만 쓸 수 있다", sentence))

    return _fail(check, *problems) if problems else _ok(check)


# ── 9. 길이 ──────────────────────────────────────────────
def check_length(rendered: str, feature: Feature | None) -> CheckResult:
    """9. 기능별 문장 수 상한. 응답 정책 §2 표."""
    check = CheckName.LENGTH
    if feature is None:
        return _ok(check)

    low, high = SENTENCE_LIMITS[feature]
    count = len(split_sentences(rendered))
    if count > high:
        return _fail(check, (f"{feature} 문장 수 상한 {high}을 넘었다", f"{count}문장"))
    if count < low:
        return _fail(check, (f"{feature} 구조상 최소 {low}문장이 필요하다", f"{count}문장"))
    return _ok(check)


# ── 오케스트레이션 ───────────────────────────────────────
@dataclass(frozen=True, slots=True)
class GuardContext:
    """검사에 필요한 바깥 사실. 요청마다 새로 만든다."""

    #: 이번 요청에 허용된 자리표시자 key -> 엔진이 낸 표시 문자열.
    engine_values: Mapping[str, str] = field(default_factory=dict)
    available_citations: Collection[str] = ()
    feature: Feature | None = None
    wiki_source: WikiSource | None = None
    causal_confidence: float | None = None
    semantic_classifier: SemanticClassifier | None = None
    segments: Sequence[Segment] | None = None

    @property
    def allowed_keys(self) -> Collection[str]:
        return self.engine_values.keys()


@dataclass(frozen=True, slots=True)
class GuardReport:
    """9개 검사를 돌린 결과."""

    results: tuple[CheckResult, ...]
    rendered: str | None = None

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def violations(self) -> tuple[Violation, ...]:
        return tuple(v for r in self.results for v in r.violations)

    @property
    def disposition(self) -> Disposition | None:
        """통과면 ``None``. 차단 사유가 하나라도 있으면 차단이 이긴다."""
        dispositions = {v.disposition for v in self.violations}
        if Disposition.BLOCK in dispositions:
            return Disposition.BLOCK
        if Disposition.REGENERATE in dispositions:
            return Disposition.REGENERATE
        return None

    @property
    def failed_checks(self) -> tuple[CheckName, ...]:
        return tuple(r.check for r in self.results if not r.passed)

    @property
    def reasons(self) -> tuple[str, ...]:
        """재생성 프롬프트에 그대로 실을 수 있는 사유 목록. §7."""
        return tuple(str(v) for v in self.violations)


def run_output_guard(
    payload: Mapping[str, Any],
    context: GuardContext,
    *,
    stop_at_first_failure: bool = True,
) -> GuardReport:
    """§5.2의 9개 검사를 순서대로 돌린다.

    기본은 fail-fast다. 앞 검사가 뒤 검사의 전제이기 때문이다. 위반을 한 번에
    모아 보고 싶으면 ``stop_at_first_failure=False``로 두되, 앞이 깨진 상태에서
    돌린 뒤쪽 결과는 참고용으로만 쓴다.
    """
    results: list[CheckResult] = []

    def record(result: CheckResult) -> bool:
        results.append(result)
        return result.passed or not stop_at_first_failure

    if not record(check_schema(payload)):
        return GuardReport(results=tuple(results))

    draft = LlmDraft.from_payload(payload) if "narrative" in payload else LlmDraft("")
    substitutions = {
        key: context.engine_values[key]
        for key in _PLACEHOLDER_RE.findall(draft.narrative)
        if key in context.engine_values
    }
    rendered = render_placeholders(draft.narrative, context.engine_values)

    steps: tuple[Callable[[], CheckResult], ...] = (
        lambda: check_unsubstituted_placeholder(rendered),
        lambda: check_unknown_placeholder(
            draft.narrative,
            context.allowed_keys,
            used_placeholders=draft.used_placeholders,
        ),
        lambda: check_raw_number(draft.narrative),
        lambda: check_engine_values(
            rendered,
            substitutions,
            context.engine_values,
            segments=context.segments,
        ),
        lambda: check_citation_integrity(
            rendered,
            context.available_citations,
            used_citations=draft.used_citations,
        ),
        lambda: check_forbidden_expression(
            rendered,
            causal_confidence=context.causal_confidence,
            semantic_classifier=context.semantic_classifier,
        ),
        lambda: check_wiki_tone(rendered, context.wiki_source),
        lambda: check_length(rendered, context.feature),
    )

    for step in steps:
        if not record(step()):
            break

    return GuardReport(results=tuple(results), rendered=rendered)
