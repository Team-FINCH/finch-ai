"""DART 공시 적재기 테스트.

네트워크와 DB 없이 돈다. DART 호출은 전부 httpx.MockTransport로 바꾼다 —
외부가 죽었다고 CI가 빨개지면 회귀 테스트로서 쓸모가 없다.
"""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from app.rag import chunking, dart
from app.rag.embedding import NullEmbedder

BODY_XML = """<?xml version="1.0" encoding="utf-8"?>
<DOCUMENT>
  <BODY><P>계약금액은 최근 매출액 대비 3.2% 수준이다.</P></BODY>
</DOCUMENT>
"""


def _zip_of(payload: bytes, name: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, payload)
    return buffer.getvalue()


def _page(items: list[dict], total_page: int) -> dict:
    return {"status": "000", "message": "정상", "total_page": total_page, "list": items}


def _item(rcept_no: str) -> dict:
    return {
        "rcept_no": rcept_no,
        "corp_name": "삼성전자",
        "report_nm": "단일판매·공급계약 체결",
        "rcept_dt": "20260814",
    }


# ── 공시목록 ───────────────────────────────────────────────────


def test_fetch_filing_list_follows_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    """825건짜리 종목이 있다. 첫 페이지만 읽으면 대부분을 놓친다."""
    monkeypatch.setattr(dart, "REQUEST_DELAY_S", 0)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page_no = request.url.params["page_no"]
        seen.append(page_no)
        return httpx.Response(200, json=_page([_item(f"2026081400{page_no}")], total_page=3))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        filings = dart.fetch_filing_list(client, "k", "00126380", "005930", "20260701", "20260819")

    assert seen == ["1", "2", "3"]
    assert [f.rcept_no for f in filings] == ["20260814001", "20260814002", "20260814003"]


def test_fetch_filing_list_stops_on_no_data() -> None:
    """status 013은 '조회된 데이터 없음'이다. 오류로 다루면 멀쩡한 종목이 실패로 남는다."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "013", "message": "조회된 데이타가 없습니다."})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert dart.fetch_filing_list(client, "k", "x", "005930", "20260701", "20260819") == []


def test_fetch_filing_list_survives_non_json_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>error</html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert dart.fetch_filing_list(client, "k", "x", "005930", "20260701", "20260819") == []


# ── 원문 ───────────────────────────────────────────────────────


def test_extract_body_reads_xml_in_zip() -> None:
    payload = _zip_of(BODY_XML.encode(), "20260814000342.xml")
    assert "계약금액" in dart.extract_body(payload, "20260814000342")


def test_extract_body_falls_back_when_not_well_formed() -> None:
    """DART 원문은 SGML에 가까워 파서가 거부하는 경우가 있다. 그래도 텍스트는 살려야 한다."""
    payload = _zip_of("<DOCUMENT><P>본문<BR>줄바꿈</DOCUMENT>".encode(), "20260814000342.xml")
    text = dart.extract_body(payload, "20260814000342")
    assert "본문" in text and "<" not in text


def test_extract_body_drops_style_block() -> None:
    """DART 원문은 앞머리에 CSS를 달고 온다. 그대로 두면 첫 청크가 통째로 스타일시트가 된다."""
    body = "<DOCUMENT><STYLE>.xforms * { font-family: 돋움체;}</STYLE><P>계약 체결</P></DOCUMENT>"
    text = dart.extract_body(_zip_of(body.encode(), "20260814000342.xml"), "20260814000342")
    assert "계약 체결" in text
    assert "font-family" not in text


def test_extract_body_decodes_cp949() -> None:
    payload = _zip_of("<P>구공시</P>".encode("cp949"), "20260814000342.xml")
    assert "구공시" in dart.extract_body(payload, "20260814000342")


def test_extract_body_reports_dart_error_when_not_zip() -> None:
    error = b"<result><status>020</status><message>\xed\x95\x9c\xeb\x8f\x84 \xec\xb4\x88\xea\xb3\xbc</message></result>"
    with pytest.raises(dart.DartError) as excinfo:
        dart.extract_body(error, "20260814000342")
    assert "020" in str(excinfo.value)


def test_fetch_document_returns_none_on_failure() -> None:
    """한 건이 실패해도 배치를 멈추지 않는다."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not a zip")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert dart.fetch_document(client, "k", "20260814000342") is None


def test_filing_published_at_and_url() -> None:
    filing = dart.Filing("20260814000342", "005930", "삼성전자", "공급계약", "20260814")
    assert filing.published_at is not None
    assert filing.published_at.year == 2026
    assert filing.url.endswith("rcpNo=20260814000342")
    assert dart.Filing("1", "005930", "", "", "").published_at is None


# ── 청킹 ───────────────────────────────────────────────────────


def test_chunk_keeps_paragraphs_together() -> None:
    text = "첫 문단이다.\n\n둘째 문단이다."
    assert chunking.chunk(text, size=100, overlap=10) == [text]


def test_chunk_splits_oversized_paragraph_with_overlap() -> None:
    pieces = chunking.chunk("가" * 250, size=100, overlap=20)
    assert len(pieces) > 1
    assert all(len(p) <= 100 for p in pieces)
    assert sum(len(p) for p in pieces) > 250  # 겹친 만큼 총합이 원문보다 길다


def test_chunk_ignores_empty_input() -> None:
    assert chunking.chunk("   \n\n  ") == []


def test_chunk_rejects_overlap_not_smaller_than_size() -> None:
    """overlap >= size면 창이 전진하지 않아 무한히 같은 조각을 만든다."""
    with pytest.raises(ValueError):
        chunking.chunk("본문", size=100, overlap=100)


# ── 임베딩 ─────────────────────────────────────────────────────


def test_null_embedder_returns_nulls() -> None:
    """제공자 키가 없다. 0 벡터를 채우면 유사도 검색이 조용히 틀린다."""
    assert NullEmbedder().embed(["a", "b"]) == [None, None]
