"""LLM 호출 래퍼.

호출부는 :class:`LlmClient` 프로토콜만 본다. 테스트는 가짜 구현을 끼워 넣고,
키가 없는 환경은 :class:`NullLlmClient`를 받는다. `app.rag.embedding`의
`NullEmbedder`와 같은 방식이다 — 없는 내용을 지어내느니 명시적으로 실패한다.

공급자는 설정으로 고른다. 어느 쪽도 특별하지 않다 — :class:`OpenAIClient`와
:class:`AnthropicClient`는 같은 계약의 두 구현이고, 호출부는 어느 쪽이 붙었는지
모른다.

도구 루프(`app.llm.agent`)가 주고받는 메시지 모양은 Anthropic 표기를 따른다.
공급자마다 이 표기를 고치는 대신, 표기와 자기 API 사이의 번역을 각 클라이언트가
안에서 끝낸다. 루프를 공급자 수만큼 분기시키지 않으려는 선택이다.

공급자별로 조심할 점은 각 클래스 문서에 적는다.
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

OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"

__all__ = [
    "LlmResult",
    "ToolUse",
    "ToolTurn",
    "LlmClient",
    "AnthropicClient",
    "OpenAIClient",
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

    _WHY = "LLM 키가 없어 설명을 생성할 수 없습니다."

    async def generate(self, **_: Any) -> LlmResult:
        raise InsufficientData(self._WHY)

    async def converse(self, **_: Any) -> ToolTurn:
        raise InsufficientData(self._WHY)


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
    """Anthropic Messages API 구현. SDK 객체는 첫 호출 때 만든다 — 테스트가 주입할 수 있게.

    호출 파라미터는 프롬프트 정책 §6.1에 고정돼 있다. `temperature`와 assistant
    프리필은 현재 모델에서 제거되어 400을 돌려주므로 쓰지 않는다. `thinking`은
    예산 없는 adaptive만 받고, `effort`와 구조화 출력 스키마는 최상위가 아니라
    `output_config` 안에 들어간다.
    """

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


class OpenAIClient:
    """OpenAI Chat Completions 구현.

    Anthropic 표기와 다른 점만 여기서 흡수한다. 확인하고 맞춘 것들:

    - system이 블록 목록이 아니라 문자열 하나다. 이어 붙인다.
    - 도구 인자가 dict가 아니라 JSON 문자열로 온다. 파싱해서 돌려준다.
    - tool_result는 user 턴 안의 블록이 아니라 별도 `tool` 역할 메시지다.
    - `max_tokens`는 400이다. `max_completion_tokens`를 쓴다.
    - 도구를 부르면 `finish_reason`이 `tool_calls`다. 루프가 보는 이름인
      `tool_use`로 바꿔 준다.

    구조화 출력은 strict json_schema를 쓴다. NARRATIVE_SCHEMA가 이미 strict
    조건(모든 속성이 required, additionalProperties 없음)을 만족한다.
    """

    def __init__(self, api_key: str, *, model: str | None = None, client: Any = None) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY가 필요하다")
        self._key = api_key
        self.model = model or settings.openai_llm_model
        self._url = (settings.openai_base_url or OPENAI_DEFAULT_BASE_URL).rstrip("/")
        self._client = client

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx

        client = self._client or httpx.AsyncClient(timeout=settings.llm_timeout_s)
        try:
            res = await client.post(
                f"{self._url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._key}"},
            )
            res.raise_for_status()
            return res.json()
        except httpx.TimeoutException as exc:
            raise LLMTimeout("LLM 응답이 지연되어 중단했습니다.") from exc
        finally:
            if self._client is None:
                await client.aclose()

    async def generate(
        self,
        *,
        system: list[dict[str, Any]],
        user: str,
        schema: dict[str, Any],
        effort: str = "high",
        max_tokens: int | None = None,
    ) -> LlmResult:
        body = await self._post(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": _system_text(system)},
                    {"role": "user", "content": user},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "narrative", "schema": schema, "strict": True},
                },
                "reasoning_effort": _EFFORT.get(effort, "medium"),
                "max_completion_tokens": max_tokens or settings.llm_max_tokens,
            }
        )

        text = body["choices"][0]["message"].get("content") or ""
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            log.warning("구조화 출력이 JSON이 아니다. 스키마 검사로 넘긴다")
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        usage = _usage(body)
        result = LlmResult(payload=payload, **usage)
        log.info(
            "llm 호출 · model=%s cache_read=%d in=%d out=%d",
            self.model, result.cache_read_tokens, result.input_tokens, result.output_tokens,
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
        body = await self._post(
            {
                "model": self.model,
                "messages": [{"role": "system", "content": _system_text(system)}]
                + _to_openai_messages(messages),
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "parameters": t["input_schema"],
                        },
                    }
                    for t in tools
                ],
                "max_completion_tokens": max_tokens or settings.llm_max_tokens,
            }
        )

        turn = _turn_from_openai(body)
        log.info(
            "llm 도구 턴 · model=%s stop=%s tools=%d cache_read=%d in=%d out=%d",
            self.model, turn.stop_reason, len(turn.tool_uses),
            turn.cache_read_tokens, turn.input_tokens, turn.output_tokens,
        )
        return turn


# effort 어휘가 서로 다르다. 정책 §6.1의 이름을 OpenAI 쪽으로 옮긴다.
_EFFORT = {"low": "low", "medium": "medium", "high": "high"}


def _system_text(system: list[dict[str, Any]]) -> str:
    """system 블록 목록을 문자열 하나로 이어 붙인다.

    cache_control 같은 Anthropic 전용 열쇠는 버린다 — OpenAI는 프롬프트 캐시를
    알아서 하고 지시할 방법도 없다.
    """
    return "\n\n".join(b.get("text", "") for b in system if b.get("type") == "text")


def _to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic 표기의 대화를 OpenAI 표기로 옮긴다.

    tool_result가 관건이다. Anthropic은 user 턴 안의 블록으로 담지만 OpenAI는
    블록마다 별도 `tool` 메시지를 요구한다 — 하나가 여럿으로 늘어난다.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            out.append({"role": msg["role"], "content": content})
            continue

        blocks = list(content or ())
        results = [b for b in blocks if b.get("type") == "tool_result"]
        if results:
            out.extend(
                {
                    "role": "tool",
                    "tool_call_id": b.get("tool_use_id", ""),
                    "content": str(b.get("content", "")),
                }
                for b in results
            )
            continue

        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        calls = [
            {
                "id": b.get("id", ""),
                "type": "function",
                "function": {
                    "name": b.get("name", ""),
                    "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False),
                },
            }
            for b in blocks
            if b.get("type") == "tool_use"
        ]
        turn: dict[str, Any] = {"role": msg["role"], "content": text or None}
        if calls:
            turn["tool_calls"] = calls
        out.append(turn)
    return out


def _usage(body: dict[str, Any]) -> dict[str, int]:
    u = body.get("usage") or {}
    return {
        "cache_read_tokens": int((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0),
        "input_tokens": int(u.get("prompt_tokens", 0) or 0),
        "output_tokens": int(u.get("completion_tokens", 0) or 0),
    }


def _turn_from_openai(body: dict[str, Any]) -> ToolTurn:
    """응답을 루프가 아는 Anthropic 표기로 되돌린다."""
    choice = body["choices"][0]
    msg = choice["message"]

    content: list[dict[str, Any]] = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})

    uses: list[ToolUse] = []
    for call in msg.get("tool_calls") or ():
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            # 인자가 깨져도 턴 전체를 버리지 않는다. 빈 인자로 부르면 도구
            # 쪽에서 검증에 걸리고, 그 사유가 다음 턴에 그대로 실린다.
            log.warning("도구 인자가 JSON이 아니다 · %s", fn.get("name"))
            args = {}
        if not isinstance(args, dict):
            args = {}
        use = ToolUse(id=str(call.get("id") or ""), name=str(fn.get("name") or ""), input=args)
        uses.append(use)
        content.append(
            {"type": "tool_use", "id": use.id, "name": use.name, "input": use.input}
        )

    stop = "tool_use" if choice.get("finish_reason") == "tool_calls" else "end_turn"
    return ToolTurn(stop_reason=stop, content=content, tool_uses=tuple(uses), **_usage(body))


@lru_cache
def get_llm_client() -> LlmClient:
    """설정을 보고 구현을 고른다. 키가 없으면 Null이다.

    `LLM_PROVIDER`로 고르되, 고른 쪽 키가 비어 있으면 다른 쪽으로 넘어간다.
    키를 넣었는데 조용히 무시당하는 편보다 낫다.
    """
    order = ("openai", "anthropic")
    if settings.llm_provider == "anthropic":
        order = ("anthropic", "openai")

    for name in order:
        if name == "openai" and settings.openai_api_key:
            if settings.llm_provider != "openai":
                log.warning("ANTHROPIC_API_KEY가 없어 OpenAI로 대신 붙는다")
            return OpenAIClient(settings.openai_api_key)
        if name == "anthropic" and settings.anthropic_api_key:
            if settings.llm_provider != "anthropic":
                log.warning("OPENAI_API_KEY가 없어 Anthropic으로 대신 붙는다")
            return AnthropicClient(settings.anthropic_api_key)

    log.warning("LLM 키가 없어 설명 생성을 건너뛴다. .env를 확인하라")
    return NullLlmClient()
