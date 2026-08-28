"""하이브리드 검색 재정렬 규칙."""

from __future__ import annotations

from collections import namedtuple
from datetime import UTC, datetime

from app.rag.search import fuse

Row = namedtuple(
    "Row",
    "id text ticker title published_at doc_type source publisher url raw_score",
)
NOW = datetime(2026, 8, 28, tzinfo=UTC)


def _row(
    id_: str,
    *,
    title: str,
    published_at: datetime,
    doc_type: str = "news",
    source: str = "NAVER_API_HUB",
) -> Row:
    return Row(
        id_,
        title,
        "005930",
        title,
        published_at,
        doc_type,
        source,
        None,
        None,
        0.5,
    )


def test_관련도_순위가_같으면_최신_뉴스가_먼저다() -> None:
    old = _row(
        "old",
        title="과거 HBM 기사",
        published_at=datetime(2025, 8, 28, tzinfo=UTC),
    )
    recent = _row(
        "recent",
        title="최근 HBM 기사",
        published_at=datetime(2026, 8, 27, tzinfo=UTC),
    )

    hits = fuse([old, recent], [recent, old], top_k=2, now=NOW)

    assert [hit["title"] for hit in hits] == ["최근 HBM 기사", "과거 HBM 기사"]
    assert hits[0]["freshness_score"] > hits[1]["freshness_score"]


def test_관련도와_날짜가_같으면_공식_출처가_먼저다() -> None:
    published = datetime(2026, 8, 20, tzinfo=UTC)
    unknown = _row(
        "unknown",
        title="출처 미상 자료",
        published_at=published,
        doc_type="filing",
        source="UNKNOWN",
    )
    dart = _row(
        "dart",
        title="DART 공시",
        published_at=published,
        doc_type="filing",
        source="DART",
    )

    hits = fuse([unknown, dart], [dart, unknown], top_k=2, now=NOW)

    assert [hit["title"] for hit in hits] == ["DART 공시", "출처 미상 자료"]
    assert hits[0]["source_weight"] > hits[1]["source_weight"]


def test_어휘_검색_상위_결과는_최종_후보에서_빠지지_않는다() -> None:
    dense = [
        _row(
            f"dense-{index}",
            title=f"밀집 후보 {index}",
            published_at=NOW,
        )
        for index in range(3)
    ]
    exact = _row("exact", title="주식소각결정", published_at=NOW, doc_type="filing", source="DART")

    hits = fuse(dense, [exact], top_k=2, now=NOW)

    assert "주식소각결정" in {hit["title"] for hit in hits}


def test_제목_검색_상위_결과는_긴_본문_후보에_묻히지_않는다() -> None:
    dense = [
        _row(
            f"dense-{index}",
            title=f"긴 반기보고서 {index}",
            published_at=NOW,
            doc_type="filing",
            source="DART",
        )
        for index in range(3)
    ]
    title_match = _row(
        "title-match",
        title="풍문또는보도에대한해명",
        published_at=NOW,
        doc_type="filing",
        source="DART",
    )

    hits = fuse(dense, [], [title_match], top_k=2, now=NOW)

    assert "풍문또는보도에대한해명" in {hit["title"] for hit in hits}
