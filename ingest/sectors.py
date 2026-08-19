"""KSIC 업종코드를 우리 섹터로 옮긴다.

매핑은 코드가 아니라 데이터(ksic_sectors.json)에 둔다. 섹터 정의는 도메인 판단이라
계속 조정되는데, 그때마다 코드를 고치면 이력이 로직 변경과 섞인다.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

TABLE_PATH = Path(__file__).with_name("ksic_sectors.json")

UNKNOWN = "기타"


@lru_cache(maxsize=1)
def _table() -> dict:
    data = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    known = set(data["sectors"])
    unknown = {v for v in data["prefix"].values()} - known
    if unknown:
        raise ValueError(f"sectors에 없는 섹터가 prefix에 있다: {sorted(unknown)}")
    unknown = {o["sector"] for o in data["override"].values()} - known
    if unknown:
        raise ValueError(f"sectors에 없는 섹터가 override에 있다: {sorted(unknown)}")
    return data


def sectors() -> list[str]:
    return list(_table()["sectors"])


def resolve(induty_code: str | None, ticker: str | None = None) -> tuple[str, str | None]:
    """(섹터, 사용한 KSIC 접두사)를 돌려준다.

    긴 접두사가 이긴다. 29271(반도체 제조용 기계)이 29(기계·장비)보다 먼저 걸려야
    반도체 장비 회사가 반도체 사이클로 묶인다.

    종목 단위 override는 KSIC가 시장 통념과 크게 어긋날 때만 쓴다.
    """
    t = _table()

    if ticker and ticker in t["override"]:
        return t["override"][ticker]["sector"], None

    code = (induty_code or "").strip()
    if not code:
        return UNKNOWN, None

    prefixes = t["prefix"]
    for size in range(len(code), 1, -1):
        head = code[:size]
        if head in prefixes:
            return prefixes[head], head

    return UNKNOWN, None
