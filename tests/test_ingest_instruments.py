"""종목 마스터 적재기 테스트.

네트워크와 DB 없이 돈다. pykrx와 DART 호출은 전부 목으로 바꾼다 —
외부가 죽었다고 CI가 빨개지면 회귀 테스트로서 쓸모가 없다.
"""

from __future__ import annotations

import io
import zipfile

import httpx
import pandas as pd
import pytest
from sqlalchemy.dialects import postgresql

from app.core.enums import Market
from ingest import instruments as ing

CORP_CODE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<result>
  <list>
    <corp_code>00126380</corp_code>
    <corp_name>삼성전자</corp_name>
    <stock_code>005930</stock_code>
    <modify_date>20260801</modify_date>
  </list>
  <list>
    <corp_code>00164779</corp_code>
    <corp_name>SK하이닉스</corp_name>
    <stock_code>000660</stock_code>
    <modify_date>20260801</modify_date>
  </list>
  <list>
    <corp_code>99999999</corp_code>
    <corp_name>삼성전자</corp_name>
    <stock_code> </stock_code>
    <modify_date>20260801</modify_date>
  </list>
</result>
""".encode()

DART_ERROR_XML = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b"<result><status>013</status><message>\xec\xa1\xb0\xed\x9a\x8c\xeb\x90\x9c "
    b"\xeb\x8d\xb0\xec\x9d\xb4\xed\x84\xb0\xea\xb0\x80 \xec\x97\x86\xec\x8a\xb5\xeb\x8b\x88\xeb\x8b\xa4."
    b"</status></result>"
)


def _zip_of(payload: bytes, name: str = "CORPCODE.xml") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, payload)
    return buffer.getvalue()


# ── 종목코드 정규화 ──────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("005930", "005930"),
        (5930, "005930"),      # pandas가 정수로 읽어버린 경우
        (660, "000660"),
        (" 000660 ", "000660"),
    ],
)
def test_normalize_ticker_keeps_leading_zeros(raw: object, expected: str) -> None:
    """선행 0이 사라지면 char_length(ticker) = 6 제약에 걸린다."""
    assert ing.normalize_ticker(raw) == expected


@pytest.mark.parametrize("raw", ["00593012", "ABCDEF", "", None])
def test_normalize_ticker_rejects_invalid(raw: object) -> None:
    with pytest.raises(ValueError):
        ing.normalize_ticker(raw)


# ── DART 고유번호 ────────────────────────────────────────
def test_extract_corp_code_xml_unzips_payload() -> None:
    """DART는 zip으로 준다. 풀지 않고 파싱하면 실패 원인이 드러나지 않는다."""
    assert ing.extract_corp_code_xml(_zip_of(CORP_CODE_XML)) == CORP_CODE_XML


def test_extract_corp_code_xml_reports_dart_error() -> None:
    """키가 틀리면 zip이 아니라 오류 XML이 온다. 상태 코드를 로그에 남길 수 있어야 한다."""
    with pytest.raises(ing.CorpCodeError, match="013"):
        ing.extract_corp_code_xml(DART_ERROR_XML)


def test_parse_corp_codes_uses_stock_code_only() -> None:
    """비상장 법인은 stock_code가 공백이라 매핑에서 빠진다."""
    mapping = ing.parse_corp_codes(CORP_CODE_XML)
    assert mapping == {"005930": "00126380", "000660": "00164779"}


def test_corp_code_is_not_matched_by_name() -> None:
    """동명 법인이 존재한다. 종목명으로 붙이면 엉뚱한 고유번호가 들어간다."""
    rows = [ing.InstrumentRow(ticker="005930", name="삼성전자", market="KOSPI")]
    ing.attach_corp_codes(rows, ing.parse_corp_codes(CORP_CODE_XML))
    assert rows[0].corp_code == "00126380"  # 이름이 같은 99999999가 아니다


def test_attach_corp_codes_leaves_unmatched_none() -> None:
    rows = [ing.InstrumentRow(ticker="123456", name="없는종목", market="KOSDAQ")]
    assert ing.attach_corp_codes(rows, {"005930": "00126380"}) == 0
    assert rows[0].corp_code is None


def test_fetch_corp_codes_reads_zip_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ing, "CORP_CODE_CACHE", ing.Path("/nonexistent-dir/corpCode.xml"))
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=_zip_of(CORP_CODE_XML)))
    with httpx.Client(transport=transport) as client:
        assert ing.fetch_corp_codes("key", client) == {
            "005930": "00126380",
            "000660": "00164779",
        }


def test_load_corp_codes_skips_without_api_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """키가 없다고 적재 전체가 죽으면 안 된다."""
    monkeypatch.setattr(ing.settings, "dart_api_key", "", raising=False)
    called = False

    def _boom(*_args: object, **_kwargs: object) -> dict[str, str]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(ing, "fetch_corp_codes", _boom)
    with caplog.at_level("WARNING", logger="ingest.instruments"):
        assert ing.load_corp_codes() == {}
    assert called is False
    assert "DART_API_KEY" in caplog.text


def test_load_corp_codes_survives_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ing.settings, "dart_api_key", "key", raising=False)

    def _fail(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(ing, "fetch_corp_codes", _fail)
    assert ing.load_corp_codes() == {}


# ── KRX 수집 ─────────────────────────────────────────────
CAPS = pd.DataFrame(
    {
        "종가": [51900, 84300],
        "시가총액": [309831714345000, 61370599369500],
        "거래량": [18541624, 3397112],
        "거래대금": [309831714345000, 61370599369500],
        "상장주식수": [5969782550, 728002365],
    },
    index=pd.Index([5930, 660], name="티커"),  # 정수 인덱스로 유실 상황을 재현한다
)

SECTORS = pd.DataFrame(
    {
        "종목명": ["삼성전자", "SK하이닉스"],
        "업종명": ["전기전자", "전기전자"],
        "종가": [51900, 84300],
    },
    index=pd.Index(["005930", "000660"], name="종목코드"),
)


class _FakeStock:
    """pykrx 대역. 호출 시그니처만 맞춘다."""

    def __init__(self, *, sectors: pd.DataFrame | None = None) -> None:
        self._sectors = SECTORS if sectors is None else sectors

    def get_market_cap_by_ticker(self, _date: str, market: str) -> pd.DataFrame:
        return CAPS if market == "KOSPI" else CAPS.iloc[:0]

    def get_market_ticker_list(self, _date: str, market: str) -> list[str]:
        return ["005930", "000660"] if market == "KOSPI" else []

    def get_market_sector_classifications(self, _date: str, market: str) -> pd.DataFrame:
        return self._sectors if market == "KOSPI" else self._sectors.iloc[:0]

    def get_market_ticker_name(self, ticker: str) -> str:
        return {"005930": "삼성전자", "000660": "SK하이닉스"}.get(ticker, "")

    def get_index_ticker_list(self, _date: str, market: str) -> list[str]:
        return ["1012"] if market == "KOSPI" else []

    def get_index_ticker_name(self, _ticker: str) -> str:
        return "전기전자"












# ── upsert ───────────────────────────────────────────────
def _compiled(rows: list[ing.InstrumentRow]) -> str:
    return str(ing.build_upsert_statement(rows).compile(dialect=postgresql.dialect()))


def test_upsert_statement_is_idempotent() -> None:
    """ticker 충돌 시 갱신한다. 재실행해도 중복 행이 생기지 않는다."""
    sql = _compiled([ing.InstrumentRow(ticker="005930", name="삼성전자", market="KOSPI")])
    assert "INSERT INTO instruments" in sql
    assert "ON CONFLICT (ticker) DO UPDATE" in sql


@pytest.mark.parametrize("column", ing.PRESERVING_COLUMNS)
def test_upsert_preserves_optional_columns(column: str) -> None:
    """DART나 업종 단계를 건너뛴 실행이 기존 값을 NULL로 덮으면 안 된다."""
    sql = _compiled([ing.InstrumentRow(ticker="005930", name="삼성전자", market="KOSPI")])
    assert f"coalesce(excluded.{column}, instruments.{column})" in sql.lower()


def test_upsert_refreshes_updated_at() -> None:
    sql = _compiled([ing.InstrumentRow(ticker="005930", name="삼성전자", market="KOSPI")])
    assert "updated_at = now()" in sql.lower()


def test_upsert_batches_all_rows() -> None:
    rows = [ing.InstrumentRow(ticker=f"{i:06d}", name=f"종목{i}", market="KOSDAQ") for i in range(3)]
    sql = _compiled(rows)
    assert sql.count("(%(ticker_m0)s") + sql.count("%(ticker_m1)s") >= 1
    assert "ON CONFLICT (ticker) DO UPDATE" in sql


async def test_upsert_noop_on_empty_rows() -> None:
    assert await ing.upsert([]) == 0


# ── 전체 흐름 ────────────────────────────────────────────




# ── DART 기반 수집 ───────────────────────────────────────
def _company(cls_: str, induty: str, name: str = "테스트전자") -> dict:
    return {"status": "000", "corp_cls": cls_, "induty_code": induty,
            "stock_name": name, "corp_name": name + "(주)"}


def test_build_row_maps_market_from_corp_cls() -> None:
    assert ing.build_row("005930", _company("Y", "264")).market == Market.KOSPI
    assert ing.build_row("035720", _company("K", "63120")).market == Market.KOSDAQ


def test_build_row_drops_delisted() -> None:
    """corpCode.xml의 stock_code 보유 법인에는 폐지 종목이 섞여 있다.

    corp_cls가 E(기타)나 N(코넥스)이면 현재 상장이 아니므로 넣지 않는다.
    """
    assert ing.build_row("036720", _company("E", "58221")) is None
    assert ing.build_row("123456", _company("N", "26")) is None


def test_build_row_keeps_raw_induty_code() -> None:
    row = ing.build_row("000660", _company("Y", "2612"))
    assert row.sector_code == "2612", "원본 KSIC를 남겨야 나중에 매핑을 바꿔도 재적재가 필요 없다"


def test_semiconductor_cluster_groups_together() -> None:
    """엔진 산식 §3.1의 섹터 집중도 진단이 성립하려면 이들이 한 섹터여야 한다.

    KSIC 원본으로는 26/26/29로 흩어진다.
    """
    cases = {"005930": "264", "000660": "2612", "000990": "2611", "042700": "29271"}
    sectors = {ing.build_row(t, _company("Y", c)).sector for t, c in cases.items()}
    assert sectors == {"반도체"}


def test_build_row_without_induty_code_falls_back() -> None:
    row = ing.build_row("999999", _company("Y", ""))
    assert row.sector == "기타"
    assert row.sector_code is None


def test_fetch_company_returns_none_on_rejected_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "013", "message": "조회된 데이타가 없습니다."})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert ing.fetch_company(client, "key", "00000000") is None


def test_fetch_company_survives_non_json_response() -> None:
    """DART가 오류 시 JSON이 아닌 문서를 돌려주기도 한다. 한 건 때문에 배치가 죽으면 안 된다."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>error</html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert ing.fetch_company(client, "key", "00126380") is None


def test_collect_instruments_skips_failures_and_keeps_going(monkeypatch) -> None:
    monkeypatch.setattr(ing, "COMPANY_DELAY_S", 0)
    payloads = {
        "c1": _company("Y", "264", "가"),
        "c2": None,                        # 조회 실패
        "c3": _company("E", "58221", "다"),  # 폐지
        "c4": _company("K", "63120", "라"),
    }
    monkeypatch.setattr(
        ing, "fetch_company", lambda client, key, cc: payloads[cc]
    )
    mapping = {"000001": "c1", "000002": "c2", "000003": "c3", "000004": "c4"}
    rows = ing.collect_instruments(None, "key", mapping)
    assert [r.ticker for r in rows] == ["000001", "000004"]


async def test_run_stops_when_corp_code_mapping_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """DART 키가 없으면 종목 목록 자체를 얻을 수 없다. 빈 적재로 덮어쓰지 않는다."""
    monkeypatch.setattr(ing, "load_corp_codes", lambda client: {})
    assert await ing.run() == 0


async def test_run_attaches_corp_codes_before_upsert(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[ing.InstrumentRow] = []
    monkeypatch.setattr(ing, "load_corp_codes", lambda client: {"005930": "00126380"})
    monkeypatch.setattr(
        ing,
        "collect_instruments",
        lambda client, key, mapping: [
            ing.InstrumentRow(ticker="005930", name="삼성전자", market=Market.KOSPI),
            ing.InstrumentRow(ticker="999999", name="미상", market=Market.KOSDAQ),
        ],
    )

    async def _capture(rows: list[ing.InstrumentRow]) -> int:
        captured.extend(rows)
        return len(rows)

    monkeypatch.setattr(ing, "upsert", _capture)
    assert await ing.run() == 2
    assert captured[0].corp_code == "00126380"
    assert captured[1].corp_code is None, "매핑에 없는 종목은 고유번호를 붙이지 않는다"
