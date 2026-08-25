"""LLM 입력단 Guardrail.

출력단(output.py)과 짝을 이루는 입력 검사다. 순수 함수 모음으로 네트워크도
DB도 건드리지 않는다.

두 층으로 나눈다.

- :func:`sanitize` — 모든 요청에 무조건 거친다. 제어문자 제거처럼 '정리'다.
  사용자 과실이고 막을 이유가 없다.
- :func:`injection_hit` — 프롬프트 인젝션 의심 패턴을 찾는다. '차단'이다.
  정규식 휴리스틱이라 우회가 가능하지만, 값싸고 즉각적이며 명백한 시도는
  걸어낸다. LLM 기반 분류가 필요해지면 SemanticClassifier 자리처럼 확장한다.

판정은 보수적으로 한다 — 애매하면 통과시키고 로그를 남긴다. 정상 질문을
막는 비용(신뢰 붕괴)이 공격 하나 통과보다 크다.
"""

from __future__ import annotations

import re

__all__ = ["MAX_QUESTION_LEN", "injection_hit", "sanitize"]

# 질문 길이 상한. 정상 질문은 수십 글자 수준이고, 붙여넣기 공격은 길다.
MAX_QUESTION_LEN = 2_000

# C0 제어문자 중 줄바꿈·탭은 살리고 나머지는 뗀다. NUL·ANSI 이스케이프가
# 로그 파서와 하류 프롬프트 조립을 흔드는 것을 막는다.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"(이전|위|앞).{0,6}지시.{0,4}(무시|잊)",
        r"지시.{0,6}(무시|무시하고)",
        r"(시스템|시스템의)\s*(프롬프트|지시|명령)",
        r"시스템\s*프롬프트",
        r"ignore\s+(all\s+)?(previous|prior|above)",
        r"disregard\s+(your|all|the)",
        r"(developer|god)\s*mode",
        r"너는?\s*이제부터?",
        r"(페르소나|역할)을?\s*(바꿔|변경|벗)",
        r"(비밀|내부)\s*(설정|규칙|프롬프트).{0,6}(알려|보여|출력)",
    )
)


def sanitize(raw: str) -> str:
    """제어문자를 떼고 양끝 공백을 누른다. 정리일 뿐 판정이 아니다."""
    return _CONTROL.sub("", raw).strip()


def injection_hit(question: str) -> str | None:
    """인젝션 의심 패턴에 걸린 첫 패턴 문자열. 아니면 None."""
    for pattern in _INJECTION_PATTERNS:
        if hit := pattern.search(question):
            return hit.group(0)
    return None
