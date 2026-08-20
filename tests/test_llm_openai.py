"""OpenAI 어댑터 테스트.

도구 루프는 Anthropic 표기로 메시지를 조립한다. 이 클래스가 하는 일은 그 표기와
OpenAI API 사이를 옮기는 것이므로, 시험할 것도 번역이 맞는지 하나뿐이다.
API를 두드려 확인한 사실(맞지 않으면 400)을 그대로 못 박는다.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.core.config import settings
from app.llm.client import (
    AnthropicClient,
    NullLlmClient,
    OpenAIClient,
    _system_text,
    _to_openai_messages,
    _turn_from_openai,
    get_llm_client,
)


class _Response:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeHttp:
    """보낸 payload를 붙잡아 두는 가짜 클라이언트."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.sent: dict[str, Any] = {}

    async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _Response:
        self.sent = json
        self.url = url
        return _Response(self.body)


def _chat(message: dict[str, Any], finish: str = "stop") -> dict[str, Any]:
    return {
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": 5},
        },
    }


# ── system 번역 ──────────────────────────────────────────
def test_system_블록을_문자열로_잇는다() -> None:
    """OpenAI의 system은 블록 목록이 아니라 문자열 하나다."""
    out = _system_text(
        [
            {"type": "text", "text": "첫째"},
            {"type": "text", "text": "둘째", "cache_control": {"type": "ephemeral"}},
        ]
    )
    assert out == "첫째\n\n둘째"


# ── 메시지 번역 ──────────────────────────────────────────
def test_tool_result는_별도_tool_메시지가_된다() -> None:
    """Anthropic은 user 턴 안의 블록, OpenAI는 역할이 tool인 별도 메시지다."""
    out = _to_openai_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "a", "content": "{}"},
                    {"type": "tool_result", "tool_use_id": "b", "content": "[]"},
                ],
            }
        ]
    )
    assert [m["role"] for m in out] == ["tool", "tool"]
    assert [m["tool_call_id"] for m in out] == ["a", "b"]


def test_tool_use는_tool_calls로_옮기고_인자를_문자열로_만든다() -> None:
    out = _to_openai_messages(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "찾아보겠습니다."},
                    {"type": "tool_use", "id": "t1", "name": "get_portfolio", "input": {"k": "가"}},
                ],
            }
        ]
    )
    (msg,) = out
    assert msg["content"] == "찾아보겠습니다."
    call = msg["tool_calls"][0]
    assert call["id"] == "t1"
    assert call["function"]["name"] == "get_portfolio"
    # OpenAI는 dict가 아니라 JSON 문자열을 받는다.
    assert json.loads(call["function"]["arguments"]) == {"k": "가"}


def test_문자열_content는_그대로_지나간다() -> None:
    out = _to_openai_messages([{"role": "user", "content": "안녕"}])
    assert out == [{"role": "user", "content": "안녕"}]


# ── 응답 번역 ────────────────────────────────────────────
def test_tool_calls는_stop_reason_tool_use로_바뀐다() -> None:
    """루프는 `tool_use`만 보고 계속할지 정한다. 이름이 다르면 한 턴에 끝난다."""
    turn = _turn_from_openai(
        _chat(
            {
                "content": None,
                "tool_calls": [
                    {"id": "t1", "function": {"name": "get_portfolio", "arguments": '{"a": 1}'}}
                ],
            },
            finish="tool_calls",
        )
    )
    assert turn.stop_reason == "tool_use"
    assert turn.tool_uses[0].input == {"a": 1}
    assert turn.content == [
        {"type": "tool_use", "id": "t1", "name": "get_portfolio", "input": {"a": 1}}
    ]
    assert (turn.input_tokens, turn.output_tokens, turn.cache_read_tokens) == (11, 7, 5)


def test_도구를_안_부르면_end_turn이다() -> None:
    turn = _turn_from_openai(_chat({"content": "답입니다."}))
    assert turn.stop_reason == "end_turn"
    assert turn.tool_uses == ()
    assert turn.content == [{"type": "text", "text": "답입니다."}]


def test_인자가_깨져도_턴을_버리지_않는다() -> None:
    """빈 인자로 부르면 도구 쪽 검증에 걸리고 그 사유가 다음 턴에 실린다."""
    turn = _turn_from_openai(
        _chat({"tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{깨짐"}}]},
              finish="tool_calls")
    )
    assert turn.tool_uses[0].input == {}


# ── 요청 조립 ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_generate가_strict_스키마와_max_completion_tokens를_보낸다() -> None:
    """`max_tokens`는 400이다. strict는 스키마가 조건을 만족해 쓸 수 있다."""
    http = _FakeHttp(_chat({"content": '{"ok": true}'}))
    client = OpenAIClient("k", model="gpt-5.4-mini", client=http)

    result = await client.generate(
        system=[{"type": "text", "text": "지시"}],
        user="질문",
        schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        effort="high",
        max_tokens=99,
    )

    assert result.payload == {"ok": True}
    assert "max_tokens" not in http.sent
    assert http.sent["max_completion_tokens"] == 99
    assert http.sent["response_format"]["json_schema"]["strict"] is True
    assert http.sent["reasoning_effort"] == "high"
    assert http.sent["messages"][0] == {"role": "system", "content": "지시"}


@pytest.mark.asyncio
async def test_converse가_도구를_function_모양으로_보낸다() -> None:
    http = _FakeHttp(_chat({"content": "답"}))
    client = OpenAIClient("k", client=http)

    await client.converse(
        system=[{"type": "text", "text": "지시"}],
        messages=[{"role": "user", "content": "질문"}],
        tools=[
            {
                "name": "get_portfolio",
                "description": "보유 종목",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    )

    (tool,) = http.sent["tools"]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "get_portfolio"
    # Anthropic은 input_schema, OpenAI는 parameters다.
    assert tool["function"]["parameters"] == {"type": "object", "properties": {}}


@pytest.mark.asyncio
async def test_JSON이_아닌_응답은_빈_payload로_넘긴다() -> None:
    """여기서 던지지 않는다. 스키마 검사가 걸러 내고 사유가 재생성에 실린다."""
    http = _FakeHttp(_chat({"content": "죄송하지만"}))
    result = await OpenAIClient("k", client=http).generate(
        system=[], user="질문", schema={"type": "object"}
    )
    assert result.payload == {}


# ── 공급자 선택 ──────────────────────────────────────────
@pytest.mark.parametrize(
    ("provider", "openai", "anthropic", "want"),
    [
        ("openai", "k", "", OpenAIClient),
        ("anthropic", "", "k", AnthropicClient),
        # 고른 쪽 키가 없으면 넘어간다. 키를 넣었는데 무시당하는 편보다 낫다.
        ("anthropic", "k", "", OpenAIClient),
        ("openai", "", "k", AnthropicClient),
        ("openai", "", "", NullLlmClient),
    ],
)
def test_공급자를_설정과_키를_보고_고른다(
    monkeypatch: pytest.MonkeyPatch, provider: str, openai: str, anthropic: str, want: type
) -> None:
    monkeypatch.setattr(settings, "llm_provider", provider)
    monkeypatch.setattr(settings, "openai_api_key", openai)
    monkeypatch.setattr(settings, "anthropic_api_key", anthropic)
    get_llm_client.cache_clear()
    assert isinstance(get_llm_client(), want)
    get_llm_client.cache_clear()
