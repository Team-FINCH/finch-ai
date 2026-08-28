"""응답에 사용한 프롬프트 묶음의 재현 가능한 버전."""

from __future__ import annotations

import hashlib

from app.llm.generate import load_prompt

ENDPOINT_PROMPTS: dict[str, tuple[str, ...]] = {
    "stocks.analysis": ("base_system", "stock_analyst"),
    "chat": ("base_system", "ask_my_portfolio"),
    "portfolio.diagnosis": ("base_system", "portfolio_doctor"),
    "portfolio.attribution": ("base_system", "attribution"),
    "orders.preview": ("base_system", "before_you_trade"),
    "briefing": ("base_system", "daily_briefing"),
}


def prompt_fingerprint(*names: str) -> str:
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(load_prompt(name).encode())
        digest.update(b"\0")
    return f"prompt_{digest.hexdigest()[:12]}"


def prompt_version_for(endpoint: str) -> str | None:
    names = ENDPOINT_PROMPTS.get(endpoint)
    return prompt_fingerprint(*names) if names else None
