"""Upstox's public instrument list: cut down to indexes, cash stocks and live futures, searched by what people type, downloaded once a day and kept on disk."""
import asyncio
import datetime as dt
import gzip
import json
import os

import httpx
import pytest

from app.providers import upstox_instruments as inst

ROWS = [
    {"segment": "NSE_INDEX", "instrument_type": "INDEX", "instrument_key": "NSE_INDEX|Nifty 50", "trading_symbol": "NIFTY 50", "name": "Nifty 50"},
    {"segment": "NSE_INDEX", "instrument_type": "INDEX", "instrument_key": "NSE_INDEX|Nifty Bank", "trading_symbol": "NIFTY BANK", "name": "Nifty Bank"},
    {"segment": "NSE_INDEX", "instrument_type": "INDEX", "instrument_key": "NSE_INDEX|Nifty IT", "trading_symbol": "NIFTY IT", "name": "Nifty IT"},
    {"segment": "NSE_EQ", "instrument_type": "EQ", "instrument_key": "NSE_EQ|INE002A01018", "trading_symbol": "RELIANCE", "name": "RELIANCE INDUSTRIES LTD", "short_name": "Reliance Industries"},
    {"segment": "NSE_EQ", "instrument_type": "EQ", "instrument_key": "NSE_EQ|INE467B01029", "trading_symbol": "TCS", "name": "TATA CONSULTANCY SERV LT", "short_name": "TCS"},
    {"segment": "NSE_EQ", "instrument_type": "SG", "instrument_key": "NSE_EQ|IN0020999999", "trading_symbol": "GOVTBOND", "name": "A government bond"},
    {"segment": "NSE_FO", "instrument_type": "CE", "instrument_key": "NSE_FO|1", "trading_symbol": "BANKNIFTY 55000 CE", "name": "BANKNIFTY", "expiry": 1793125799000},
    {"segment": "NSE_FO", "instrument_type": "FUT", "instrument_key": "NSE_FO|48699", "trading_symbol": "BANKNIFTY FUT 27 OCT 26", "name": "BANKNIFTY", "underlying_symbol": "BANKNIFTY", "lot_size": 30, "expiry": 1793125799000},
    {"segment": "NSE_FO", "instrument_type": "FUT", "instrument_key": "NSE_FO|61466", "trading_symbol": "BANKNIFTY FUT 23 NOV 26", "name": "BANKNIFTY", "underlying_symbol": "BANKNIFTY", "lot_size": 30, "expiry": 1795458599000},
    {"segment": "NSE_FO", "instrument_type": "FUT", "instrument_key": "NSE_FO|1000", "trading_symbol": "NIFTY FUT 29 SEP 26", "name": "NIFTY", "underlying_symbol": "NIFTY", "lot_size": 75, "expiry": 1790000000000},
]
TODAY = dt.date(2026, 10, 27)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(inst, "CACHE_PATH", tmp_path / "instruments.json")
    monkeypatch.setattr(inst, "_mem", None)


def test_the_list_keeps_indexes_cash_stocks_and_live_futures_only():
    rows = inst.normalise(ROWS, TODAY)
    symbols = [r["symbol"] for r in rows]
    assert "RELIANCE" in symbols and "NIFTY BANK" in symbols and "BANKNIFTY FUT 27 OCT 26" in symbols
    assert not any(s in symbols for s in ("GOVTBOND", "BANKNIFTY 55000 CE", "NIFTY FUT 29 SEP 26"))     # a bond, an option and an expired future are left out
    fut = next(r for r in rows if r["symbol"] == "BANKNIFTY FUT 27 OCT 26")
    assert fut["expiry"] == "2026-10-27" and fut["lot"] == 30 and fut["underlying"] == "BANKNIFTY" and fut["key"] == "NSE_FO|48699" and fut["kind"] == "FUT"


def test_search_finds_what_people_type():
    rows = inst.normalise(ROWS, TODAY)
    found = lambda q: [r["symbol"] for r in inst.search(rows, q)]
    assert found("banknifty")[0] == "NIFTY BANK" and "BANKNIFTY FUT 27 OCT 26" in found("banknifty")            # the index first, then its futures
    futs = found("BANKNIFTY")
    assert futs.index("BANKNIFTY FUT 27 OCT 26") < futs.index("BANKNIFTY FUT 23 NOV 26")                      # nearest expiry first
    assert found("nifty")[0] == "NIFTY 50" and "NIFTY BANK" in found("nifty") and "NIFTY IT" in found("nifty")
    assert found("reli") == ["RELIANCE"] and found("tata consultancy") == ["TCS"] and found("zzzz") == [] and found("") == []
    assert len(inst.search(rows, "nifty", limit=1)) == 1


def test_the_list_is_downloaded_once_and_kept_for_the_day(monkeypatch):
    hits = []

    def handler(request):
        hits.append(1)
        return httpx.Response(200, content=gzip.compress(json.dumps(ROWS).encode()))

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    first = asyncio.run(inst.load())
    inst._mem = None                                           # a restart: the saved file is used, nothing is downloaded
    second = asyncio.run(inst.load())
    assert len(hits) == 1 and len(first) == len(second) and inst.CACHE_PATH.exists()


def test_an_old_saved_list_is_used_when_upstox_cannot_be_reached(monkeypatch):
    inst.CACHE_PATH.write_text(json.dumps(inst.normalise(ROWS, TODAY)))
    old = (dt.datetime.now() - dt.timedelta(days=3)).timestamp()
    os.utime(inst.CACHE_PATH, (old, old))

    def boom(request):
        raise httpx.ConnectError("down")

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(boom), **kw))
    assert len(asyncio.run(inst.load())) > 0


def test_with_no_list_at_all_the_failure_is_named(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("down")

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(boom), **kw))
    with pytest.raises(inst.InstrumentsUnavailable):
        asyncio.run(inst.load())
