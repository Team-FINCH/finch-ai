"""Anthropic 호출 래퍼.

호출부는 :class:`LlmClient` 프로토콜만 본다. 테스트는 가짜 구현을 끼워 넣고,
키가 없는 환경은 :class:`NullLlmClient`를 받는다. `app.rag.embedding`의
`NullEmbedder`와 같은 방식이다 — 없는 내용을 지어내느니 명시적으로 실패한다.

호출 파라미터는 프롬프트 정책 §6.1에 고정돼 있다. `temperature`와 assistant
프리필은 현재 모델에서 제거되어 400을 돌려주므로 쓰지 않는다. `thinking`은
예산 없는 adaptive만 받고, `effort`와 구조화 출력 스키마는 최상위가 아니라
`output_config` 안에 들어간다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol, runtime_checkable

from app.core.config import settings
from app.core.errors import InsufficientData, LLMTimeout

log = logging.getLogger("app.llm.client")

__all__ = [
    "LlmResult",
    "ToolUse",
    "ToolTurn",
    "LlmClient",
    "AnthropicClient",
    "NullLlmClient",
    "get_llm_client",
]


@dataclass(frozen=True, slots=True)
class LlmResult:
    """호출 한 번의 결과. `payload`는 구조화 출력 JSON을 파싱한 것이다."""

    payload: dict[str, Any]
    cache_read_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ToolUse:
    """모델이 부른 도구 한 건."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolTurn:
    """도구 사용 루프의 한 턴.

    `content`는 응답 블록을 그대로 담는다. 다음 요청의 assistant 턴으로 손대지 않고
    되돌려 보내야 한다 — thinking 블록의 서명이 함께 실려 있어 한 글자만 바뀌어도
    거부된다.
    """

    stop_reason: str
    content: list[dict[str, Any]]
    tool_uses: tuple[ToolUse, ...] = ()
    cache_read_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@runtime_checkable
class LlmClient(Protocol):
    """생성 호출의 계약. 조립과 검사는 :mod:`app.llm.generate`가 한다."""

    async def generate(
        self,
        *,
        system: list[dict[str, Any]],
        user: str,
        schema: dict[str, Any],
        effort: str = "high",
        max_tokens: int | None = None,
    ) -> LlmResult: ...

    async def converse(
        self,
        *,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int | None = None,
    ) -> ToolTurn: ...


class NullLlmClient:
    """키가 없을 때의 기본값.

    빈 응답을 돌려주면 호출부가 "자료가 없다"는 정상 상태와 구분하지 못한다.
    설정이 빠졌다는 사실이 그대로 보이도록 예외를 던진다.
    """

    async def generate(self, **_: Any) -> LlmResult:
        raise InsufficientData("ANTHROPIC_API_KEY가 없어 설명을 생성할 수 없습니다.")

    async def converse(self, **_: Any) -> ToolTurn:
        raise InsufficientData("ANTHROPIC_API_KEY가 없어 설명을 생성할 수 없습니다.")


def _to_result(message: Any) -> LlmResult:
    """응답에서 text 블록만 이어 붙여 JSON으로 읽는다.

    thinking 블록이 섞여 오므로 type으로 걸러야 한다. 파싱에 실패해도 여기서
    던지지 않는다. 빈 payload를 돌려주면 스키마 검사(§5.2 1번)가 걸러 내고,
    그 사유가 그대로 재생성 프롬프트에 실린다.
    """
    text = "".join(
        block.text
        for block in getattr(message, "content", ())
        if getattr(block, "type", None) == "text"
    )
    try:
        payload = json.loads(text) if text else {}
    except json.JSONDecodeError:
        log.warning("구조화 출력이 JSON이 아니다. 스키마 검사로 넘긴다")
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    usage = getattr(message, "usage", None)
    return LlmResult(
        payload=payload,
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
    )


class AnthropicClient:
    """실제 호출. SDK 객체는 첫 호출 때 만든다 — 테스트가 주입할 수 있게."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str | None = None,
        timeout_s: float | None = None,
        client: Any | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY가 필요하다")
        self.model = model or settings.llm_model
        self._key = api_key
        self._timeout = timeout_s or settings.llm_timeout_s
        self._client = client

    def _sdk(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(
                api_key=self._key, timeout=self._timeout
            )
        return self._client

    async def generate(
        self,
        *,
        system: list[dict[str, Any]],
        user: str,
        schema: dict[str, Any],
        effort: str = "high",
        max_tokens: int | None = None,
    ) -> LlmResult:
        import anthropic

        try:
            message = await self._sdk().messages.create(
                model=self.model,
                max_tokens=max_tokens or settings.llm_max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                thinking={"type": "adaptive"},
                output_config={
                    "effort": effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        except anthropic.APITimeoutError as exc:
            raise LLMTimeout("LLM 응답이 지연되어 중단했습니다.") from exc

        result = _to_result(message)
        # §1.2 — 반복 요청에서 이 값이 계속 0이면 접두가 어딘가에서 깨지고 있다.
        log.info(
            "llm 호출 · model=%s effort=%s cache_read=%d in=%d out=%d",
            self.model,
            effort,
            result.cache_read_tokens,
            result.input_tokens,
            result.output_tokens,
        )
        return result

    async def converse(
        self,
        *,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int | None = None,
    ) -> ToolTurn:
        """도구 목록을 함께 넘기는 대화 호출. 구조화 출력은 쓰지 않는다.

        이 턴의 일은 도구를 고르는 것뿐이고, 본문은 `generate`가 자리표시자
        규약(§3) 아래에서 따로 쓴다.
        """
        import anthropic

        try:
            message = await self._sdk().messages.create(
                model=self.model,
                max_tokens=max_tokens or settings.llm_max_tokens,
                system=system,
                messages=messages,
                tools=tools,
                thinking={"type": "adaptive"},
            )
        except anthropic.APITimeoutError as exc:
            raise LLMTimeout("LLM 응답이 지연되어 중단했습니다.") from exc

        turn = _to_turn(message)
        log.info(
            "llm 도구 턴 · model=%s stop=%s tools=%d cache_read=%d in=%d out=%d",
            self.model,
            turn.stop_reason,
            len(turn.tool_uses),
            turn.cache_read_tokens,
            turn.input_tokens,
            turn.output_tokens,
        )
        return turn


def _blocks(message: Any) -> list[dict[str, Any]]:
    """응답 블록을 되돌려 보낼 수 있는 dict로 바꾼다. 내용은 손대지 않는다."""
    out: list[dict[str, Any]] = []
    for block in getattr(message, "content", ()):
        dump = getattr(block, "model_dump", None)
        out.append(dump(exclude_none=True) if callable(dump) else dict(block))
    return out


def _to_turn(message: Any) -> ToolTurn:
    """도구 사용 응답을 루프가 쓰는 형태로 정리한다."""
    content = _blocks(message)
    usage = getattr(message, "usage", None)
    return ToolTurn(
        stop_reason=str(getattr(message, "stop_reason", "") or ""),
        content=content,
        tool_uses=tuple(
            ToolUse(
                id=str(block.get("id") or ""),
                name=str(block.get("name") or ""),
                input=dict(block.get("input") or {}),
            )
            for block in content
            if block.get("type") == "tool_use"
        ),
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
    )


@lru_cache
def get_llm_client() -> LlmClient:
    """설정을 보고 구현을 고른다. 키가 없으면 Null이다."""
    if settings.anthropic_api_key:
        return AnthropicClient(settings.anthropic_api_key)
    log.warning("ANTHROPIC_API_KEY가 없어 설명 생성을 건너뛴다. .env를 확인하라")
    return NullLlmClient()
