"""평가 실행기.

    python -m eval.run --retrieval     검색 정확도 (임베딩 키 필요)
    python -m eval.run --metrics       지표 자체 점검 (키 불필요)

키가 없으면 해당 항목만 건너뛰고 나머지는 돈다. 키가 생겼을 때 바로 측정할 수
있도록 미리 만들어 두는 것이 목적이라, 지금 못 도는 부분도 형태는 갖춰 둔다.
"""

from __future__ import annotations

import argparse
import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
RETRIEVAL_SET = EVAL_DIR / "retrieval.yaml"


@dataclass
class RetrievalCase:
    id: str
    query: str
    ticker: str | None
    expect_title_contains: list[str]
    expect_empty: bool = False


def _load_cases(path: Path = RETRIEVAL_SET) -> list[RetrievalCase]:
    """YAML을 최소한으로 읽는다.

    pyyaml을 의존성에 넣지 않으려고 직접 파싱한다. 평가셋 형식이 단순하고
    고정돼 있어 범용 파서가 필요 없다 — 형식이 복잡해지면 그때 넣는다.
    """
    text = path.read_text(encoding="utf-8")
    body = text.split("\ncases:", 1)[1]
    cases: list[RetrievalCase] = []
    cur: dict = {}
    for raw in body.splitlines():
        line = raw.rstrip()
        if re.match(r"^\s*#", line) or not line.strip():
            continue
        if re.match(r"^\s*-\s+id:", line):
            if cur:
                cases.append(_to_case(cur))
            cur = {"id": line.split("id:", 1)[1].strip()}
            continue
        m = re.match(r"^\s+(\w+):\s*(.*)$", line)
        if m and cur:
            k, v = m.group(1), m.group(2).strip()
            if k in ("why", "corpus_note") or v == "|":
                continue
            cur[k] = v
    if cur:
        cases.append(_to_case(cur))
    return cases


def _to_case(d: dict) -> RetrievalCase:
    raw = d.get("expect_title_contains", "[]")
    titles = [t.strip() for t in raw.strip("[]").split(",") if t.strip()]
    ticker = d.get("ticker", "null").strip('"')
    return RetrievalCase(
        id=d["id"],
        query=d.get("query", ""),
        ticker=None if ticker in ("null", "") else ticker,
        expect_title_contains=titles,
        expect_empty=d.get("expect_empty", "false").lower() == "true",
    )


async def run_retrieval(top_k: int = 5) -> int:
    from app.core.db import engine
    from app.rag.embedding import NullEmbedder, get_embedder
    from app.rag.search import search

    if isinstance(get_embedder(), NullEmbedder):
        print("임베딩 키가 없어 검색 평가를 건너뛴다. OPENAI_API_KEY를 설정하라.")
        return 0

    cases = _load_cases()
    hits = graded = 0
    try:
        for c in cases:
            found = await search(c.query, top_k=top_k, ticker=c.ticker)
            titles = " | ".join(h["title"] for h in found)
            if c.expect_empty:
                # 오탐 관찰용. 지금은 top-k만 쓰므로 항상 무언가 나온다.
                print(f"  [관찰] {c.id:<22} 상위 {len(found)}건 — {titles[:60]}")
                continue
            graded += 1
            ok = any(
                any(want in h["title"] for want in c.expect_title_contains) for h in found
            )
            hits += ok
            print(f"  [{'HIT ' if ok else 'MISS'}] {c.id:<22} {titles[:60]}")
        if graded:
            print(f"\nRecall@{top_k}: {hits}/{graded} = {hits / graded:.3f}")
    finally:
        await engine.dispose()
    return 0


def run_metrics_selfcheck() -> int:
    """지표가 실제로 위반을 잡는지 확인한다.

    통과만 하는 지표는 쓸모가 없다. 일부러 어긴 입력을 넣어 걸리는지 본다.
    """
    from app.core.enums import MetricSource, Unit
    from app.core.schemas import Section, Segment
    from eval.metrics import groundedness, numerical_accuracy, portfolio_accuracy

    ok = True

    good = Section.from_segments(
        [
            Segment.text("반도체 관련 자산이 포트폴리오의 "),
            Segment.metric("42.3%", 0.423, MetricSource.RISK_ENGINE, unit=Unit.RATIO),
            Segment.text("를 차지합니다."),
        ]
    )
    r = numerical_accuracy(good, {"42.3%": 0.423})
    print(" ", r)
    ok &= r.passed

    bad = Section(text="반도체 비중이 42.3%이고 종목은 3개입니다.")
    r = numerical_accuracy(bad, {})
    print(" ", r)
    ok &= not r.passed  # 잡아야 정상

    drift = Section.from_segments(
        [Segment.metric("42.3%", 0.999, MetricSource.RISK_ENGINE, unit=Unit.RATIO)]
    )
    r = numerical_accuracy(drift, {"42.3%": 0.423})
    print(" ", r)
    ok &= not r.passed

    r = groundedness(Section(text="회사는 신규 계약을 체결했습니다."), set())
    print(" ", r)
    ok &= not r.passed

    r = groundedness(Section(text="회사는 신규 계약을 체결했습니다.[^cit_1]"), {"cit_1"})
    print(" ", r)
    ok &= r.passed

    r = portfolio_accuracy(
        Section.from_segments(
            [Segment.metric("41.7%", 0.417, MetricSource.PORTFOLIO_ENGINE, unit=Unit.RATIO)]
        ),
        {"005930": 0.417},
    )
    print(" ", r)
    ok &= r.passed

    r = portfolio_accuracy(
        Section.from_segments(
            [Segment.metric("41.7%", 0.417, MetricSource.PORTFOLIO_ENGINE, unit=Unit.RATIO)]
        ),
        {"005930": 0.180},
    )
    print(" ", r)
    ok &= not r.passed

    print("\n지표 자체 점검:", "통과" if ok else "실패")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description="평가 실행")
    p.add_argument("--retrieval", action="store_true", help="검색 정확도 (키 필요)")
    p.add_argument("--metrics", action="store_true", help="지표 자체 점검")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--list", action="store_true", help="평가셋 요약")
    a = p.parse_args()

    if a.list:
        cases = _load_cases()
        print(f"검색 평가셋 {len(cases)}건")
        for c in cases:
            tag = "빈 결과 기대" if c.expect_empty else ", ".join(c.expect_title_contains)
            print(f"  {c.id:<22} {c.query[:34]:<36} → {tag[:34]}")
        return 0
    if a.metrics:
        return run_metrics_selfcheck()
    if a.retrieval:
        return asyncio.run(run_retrieval(a.top_k))
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
