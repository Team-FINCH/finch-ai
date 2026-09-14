"""Exercise real selection SQL against a disposable SQLite instrument table."""

import asyncio
import json
import logging
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import create_engine, text

from app.core.config import Settings, settings
from app.rag import dart
from ingest import events, financials, news, prices
from ingest.universe import target_tickers

DEFAULT = Settings.model_fields["service_tickers"].default
OUTSIDE = "999999"


class LocalSession:
    """Async call boundary backed by real, local SQLAlchemy query execution."""

    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, stmt):
        return self.connection.execute(stmt)

    async def scalars(self, stmt):
        return self.connection.execute(stmt).scalars()


@pytest.fixture
def instrument_db(monkeypatch):
    engine = create_engine("sqlite://")
    with engine.connect() as connection:
        connection.execute(
            text(
                "CREATE TABLE instruments (ticker TEXT PRIMARY KEY, name TEXT, "
                "corp_code TEXT, status TEXT, market_cap NUMERIC)"
            )
        )
        # Reverse insertion order and an out-of-universe high-cap stock expose
        # accidental selection by insertion, ticker or market capitalization.
        for i, ticker in enumerate(reversed((*DEFAULT, OUTSIDE))):
            connection.execute(
                text("INSERT INTO instruments VALUES (:ticker, :name, :corp, 'listed', :cap)"),
                {
                    "ticker": ticker,
                    "name": f"Name {ticker}",
                    "corp": f"C{ticker}",
                    "cap": 1000000 - i,
                },
            )
        session = LocalSession(connection)
        monkeypatch.setattr(news, "SessionFactory", lambda: session)
        monkeypatch.setattr(dart, "SessionFactory", lambda: session)
        monkeypatch.setattr(settings, "service_tickers", DEFAULT)
        yield session
    engine.dispose()


@pytest.fixture(params=["news", "prices", "financials", "dart"])
def selector(request, instrument_db):
    async def select_targets(**kwargs):
        if request.param == "news":
            rows = await news.load_targets(**kwargs)
            assert all(name == f"Name {ticker}" for ticker, name in rows)
        elif request.param == "dart":
            rows = await dart.load_targets(**kwargs)
            assert all(corp == f"C{ticker}" for ticker, corp in rows)
        elif request.param == "financials":
            rows = await financials._targets(instrument_db, **kwargs)
            assert all(corp == f"C{ticker}" for ticker, corp in rows)
        else:
            if "tickers" in kwargs:
                kwargs["explicit"] = kwargs.pop("tickers")
            return await prices._target_tickers(instrument_db, **kwargs)
        return [row[0] for row in rows]

    return select_targets


async def test_default_resolves_all_30(selector, caplog):
    with caplog.at_level(logging.INFO):
        result = await selector()
    assert result == list(DEFAULT)
    assert len(result) == 30
    assert "requested=30 resolved=30" in caplog.text


async def test_missing_universe_ticker_warns_and_keeps_available(selector, instrument_db, caplog):
    instrument_db.connection.execute(
        text("DELETE FROM instruments WHERE ticker=:t"), {"t": DEFAULT[0]}
    )
    assert await selector() == list(DEFAULT[1:])
    assert "requested=30 resolved=29" in caplog.text
    assert DEFAULT[0] in caplog.text


async def test_only_two_resolved_is_visible(selector, instrument_db, caplog):
    instrument_db.connection.execute(
        text("DELETE FROM instruments WHERE ticker NOT IN (:a, :b)"),
        {"a": DEFAULT[0], "b": DEFAULT[1]},
    )
    assert await selector() == list(DEFAULT[:2])
    assert "requested=30 resolved=2" in caplog.text


async def test_no_default_targets_fails(selector, instrument_db, caplog):
    instrument_db.connection.execute(text("DELETE FROM instruments"))
    with pytest.raises(ValueError, match="No service universe targets resolved"):
        await selector()
    assert "requested=30 resolved=0" in caplog.text


async def test_explicit_tickers_override_universe_and_limit(selector):
    assert await selector(tickers=[OUTSIDE, DEFAULT[-1]], limit=1) == [OUTSIDE, DEFAULT[-1]]


async def test_explicit_missing_keeps_empty_result(selector):
    assert await selector(tickers=["888888"], limit=1) == []


@pytest.mark.parametrize("limit", [1, 5, 30, 3000])
async def test_limit_caps_config_order(selector, limit):
    assert await selector(limit=limit) == list(DEFAULT[:limit])


@pytest.mark.parametrize("limit", [0, -1])
async def test_nonpositive_limit_rejected(selector, limit):
    with pytest.raises(ValueError, match="limit must be positive"):
        await selector(limit=limit)


async def test_environment_universe_used_by_all(selector, monkeypatch):
    monkeypatch.setenv("SERVICE_TICKERS", json.dumps([OUTSIDE, DEFAULT[-1]]))
    overridden = Settings(_env_file=None)
    monkeypatch.setattr(settings, "service_tickers", overridden.service_tickers)
    assert await selector() == [OUTSIDE, DEFAULT[-1]]


async def test_delisted_default_is_ineligible(selector, instrument_db, caplog):
    instrument_db.connection.execute(
        text("UPDATE instruments SET status='delisted' WHERE ticker=:t"), {"t": DEFAULT[0]}
    )
    assert await selector() == list(DEFAULT[1:])
    assert "requested=30 resolved=29" in caplog.text


@pytest.mark.parametrize("module", [dart, financials])
async def test_dart_requires_corp_code(module, instrument_db, caplog):
    instrument_db.connection.execute(
        text("UPDATE instruments SET corp_code=NULL WHERE ticker=:t"), {"t": DEFAULT[0]}
    )
    rows = await dart.load_targets() if module is dart else await financials._targets(instrument_db)
    assert [t for t, _ in rows] == list(DEFAULT[1:])
    assert "requested=30 resolved=29" in caplog.text


@pytest.mark.parametrize("value", [[], ["005930", "005930"], ["5930"], ["abcdef"]])
def test_invalid_universe_rejected(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, service_tickers=value)


def test_explicit_bypasses_even_invalid_limit():
    assert target_tickers([OUTSIDE], -1) == [OUTSIDE]


def test_backend_universe_parity():
    backend = Path(__file__).resolve().parents[2] / "backend/src/main/resources/application.yaml"
    if not backend.is_file():
        pytest.skip("Backend config absent in standalone AI checkout")
    config = next(yaml.safe_load_all(backend.read_text()))
    codes = config["finch"]["kis"]["realtime"]["codes"]
    assert len(codes) == len(DEFAULT) == 30
    assert set(codes) == set(DEFAULT)


@pytest.mark.parametrize("module", [news, prices, financials, events])
@pytest.mark.parametrize(
    "args, expected",
    [
        ([], list(DEFAULT)),
        (["--limit", "1"], list(DEFAULT[:1])),
        (["--tickers", OUTSIDE + "," + DEFAULT[-1], "--limit", "1"], [OUTSIDE, DEFAULT[-1]]),
    ],
)
def test_cli_resolves_targets(module, args, expected, instrument_db, monkeypatch):
    """Run argument parsing and actual target SQL, stopping before ingestion."""

    class TargetsResolved(Exception):
        pass

    async def capture(*positional, **kwargs):
        if module is news:
            rows = await news.load_targets(positional[1], positional[3])
            result = [t for t, _ in rows]
        elif module is prices:
            result = await prices._target_tickers(instrument_db, kwargs["tickers"], kwargs["limit"])
        elif module is financials:
            result = [
                t
                for t, _ in await financials._targets(
                    instrument_db, kwargs["limit"], kwargs["tickers"]
                )
            ]
        else:
            result = [t for t, _ in await events.load_targets(kwargs["limit"], kwargs["tickers"])]
        assert result == expected
        raise TargetsResolved

    monkeypatch.setattr(module, "run" if module is news else "ingest", capture)
    monkeypatch.setattr(settings, "dart_api_key", "unit-test-only")
    monkeypatch.setattr(sys, "argv", [module.__name__, *args])
    with pytest.raises(TargetsResolved):
        if module is news:
            news.main()
        else:
            asyncio.run(module._main())


def test_backend_parity_skips_standalone_checkout(monkeypatch):
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    with pytest.raises(pytest.skip.Exception, match="standalone AI checkout"):
        test_backend_universe_parity()
