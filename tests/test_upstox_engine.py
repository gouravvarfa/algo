"""The live engine (Top Bottom) running on Upstox data: the same engine, a different data source. Candles and prices are made up here (no network); what is checked is
that the engine accepts the Upstox provider and feed, resolves BANKNIFTY futures through Upstox's list, builds the page's snapshot from Upstox-shaped candles, takes the
live price from the Upstox feed and the index prices from Upstox's index keys, and keeps Angel One's tokens out of it."""
import asyncio
import datetime as dt
import math
import time

import pandas as pd
import pytest

from app.live.ticks import Tick
from app.live.upstox_feed import UpstoxPollFeed
from app.paper_trading import engine as em
from app.providers import upstox_instruments as inst, upstox_provider as up
from app.services import upstox_credential_store as store

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
BANK_INDEX, FUT_KEY = "NSE_INDEX|Nifty Bank", "NSE_FO|48699"
ROWS = [
    {"segment": "NSE_INDEX", "instrument_type": "INDEX", "instrument_key": BANK_INDEX, "trading_symbol": "NIFTY BANK", "name": "Nifty Bank"},
    {"segment": "NSE_FO", "instrument_type": "FUT", "instrument_key": FUT_KEY, "trading_symbol": "BANKNIFTY FUT", "name": "BANKNIFTY", "underlying_symbol": "BANKNIFTY",
     "lot_size": 30, "expiry": 4102444799000},                                     # the year 2099: always "live"
]


def daily(days: int = 220) -> pd.DataFrame:
    """Weekday candles ending yesterday: a wave, so there are real tops and bottoms for the strategy to find."""
    today = dt.datetime.now(IST).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    days_list = [today - dt.timedelta(days=i) for i in range(days, 0, -1) if (today - dt.timedelta(days=i)).weekday() < 5]
    rows = []
    for n, d in enumerate(days_list):
        c = 54000 + 1500 * math.sin(n / 6.0) + 8 * n
        rows.append((d, c - 40, c + 120, c - 120, c, 0.0))
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"]).set_index("date")
    return df.astype(float)


class FakeUpstox(up.UpstoxProvider):
    def __init__(self):
        super().__init__()
        self.asked = []
        self.frame = daily()

    async def get_intraday_ohlc(self, exch_seg, symbol_token, interval, days_back=5, include_partial=False):
        self.asked.append((symbol_token, interval, days_back))
        if interval == "ONE_DAY":
            return self.frame.copy()
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])          # no intraday data in this test: the engine must carry on


@pytest.fixture
def engine(tmp_path, monkeypatch):
    from app.live import candle_store
    for name, f in (("STATE_PATH", "w.json"), ("JOURNAL_PATH", "j.jsonl"), ("JOURNAL_SEEN", "js.json"), ("LEGACY_OWNER_PATH", "l.txt"), ("CONTRACTS_PATH", "contracts.json")):
        monkeypatch.setattr(em, name, tmp_path / f)
    monkeypatch.setattr(candle_store, "DB", tmp_path / "candles.db")
    monkeypatch.setattr(store, "CREDENTIAL_PATH", tmp_path / "upstox_credentials.json")
    monkeypatch.setattr(inst, "CACHE_PATH", tmp_path / "instruments.json")
    monkeypatch.setattr(em, "CANDLE_CALL_GAP", 0.0)

    async def load():
        return inst.normalise(ROWS, dt.date(2026, 10, 27))

    monkeypatch.setattr(inst, "load", load)
    return em.PaperEngine(FakeUpstox())


def run(coro):
    return asyncio.run(coro)


def test_the_engine_takes_the_feed_and_the_index_keys_from_the_data_source(engine):
    assert isinstance(engine.feed, UpstoxPollFeed)
    assert engine.index_tokens == {"BANKNIFTY": ("NSE", BANK_INDEX), "NIFTY": ("NSE", "NSE_INDEX|Nifty 50")}


def test_an_angel_one_engine_is_unchanged(tmp_path, monkeypatch):
    from app.live.ticks import TickFeed

    class P:
        async def get_session(self): return object()
        def reset_session(self): pass

    monkeypatch.setattr(em, "STATE_PATH", tmp_path / "w.json")
    e = em.PaperEngine(P())
    assert isinstance(e.feed, TickFeed) and e.index_tokens == em.INDEX_TOKENS and e.index_tokens["BANKNIFTY"] == ("NSE", "99926009")


def test_banknifty_futures_are_found_through_upstoxs_list_and_the_feed_watches_them(engine):
    i = run(engine.add("BANKNIFTY", "FUTURE", "1D", 1, 150, owner="u1"))
    assert (i.token, i.exch_seg, i.lot_size, i.quantity) == (FUT_KEY, "NFO", 30, 30) and i.expiry == "2100-01-01"
    assert i.trading_symbol == "BANKNIFTY01JAN00FUT"
    assert FUT_KEY in engine.feed.keys()


def test_the_page_snapshot_is_built_from_upstox_candles(engine):
    i = run(engine.add("BANKNIFTY", "FUTURE", "1D", 1, 150, owner="u1"))
    assert engine.errors == {} and engine.faults == {}
    snap = engine.snapshots[i.id]
    assert snap["instrument"]["token"] == FUT_KEY and len(snap["candles"]) > 50
    assert snap["latest_top"] is not None and snap["latest_bottom"] is not None
    assert snap["ltp"] == pytest.approx(engine.provider.frame["close"].iloc[-1], abs=0.01)
    assert any(c[0] == FUT_KEY and c[1] == "ONE_DAY" for c in engine.provider.asked)
    assert all(not str(c[0]).isdigit() for c in engine.provider.asked)                  # an Upstox engine never asks for an Angel One token


def test_the_live_price_from_upstox_becomes_the_pages_price(engine):
    i = run(engine.add("BANKNIFTY", "FUTURE", "1D", 1, 150, owner="u1"))
    engine.feed.connected = True
    engine.feed.latest[FUT_KEY] = Tick(FUT_KEY, 1, 55555.5, dt.datetime.now(IST), time.time())
    run(engine.refresh_one(i))
    snap = engine.snapshots[i.id]
    assert snap["ltp"] == 55555.5 and snap["feed"]["live"] is True and snap["candles"][-1]["close"] == 55555.5


def test_index_prices_use_upstoxs_index_key_and_say_where_they_come_from(engine):
    engine.feed.connected = True
    engine.feed.latest[BANK_INDEX] = Tick(BANK_INDEX, 1, 54141.5, dt.datetime.now(IST), time.time())
    q = run(engine.index_quote("BANKNIFTY"))
    assert q["price"] == 54141.5 and q["live"] is True and q["source"] == "Upstox live price"
    assert q["prev_close"] == pytest.approx(engine.provider.frame["close"].iloc[-1], abs=0.01)
    assert q["change"] == pytest.approx(54141.5 - q["prev_close"], abs=0.01)


def test_the_overview_says_which_data_source_runs_and_reports_the_feed(engine):
    run(engine.add("BANKNIFTY", "FUTURE", "1D", 1, 150, owner="u1"))
    o = engine.overview(owner="u1")
    assert o["feed"]["source"] == "Upstox" and len(o["items"]) == 1


def test_start_watches_the_index_keys_not_angel_tokens(engine):
    async def go():
        engine.start()
        try:
            assert BANK_INDEX in engine.feed.keys() and "99926009" not in engine.feed.keys()
        finally:
            await engine.stop()

    run(go())


def test_without_an_upstox_login_the_instrument_says_how_to_fix_it_and_nothing_crashes(engine):
    store.clear()

    class Ended(FakeUpstox):
        async def get_intraday_ohlc(self, *a, **k):
            return await self._call(lambda t: asyncio.sleep(0))                    # the login check every real call makes

    engine.provider = Ended()
    engine.instruments["x"] = em.Instrument(id="x", symbol="BANKNIFTY", instrument_type="FUTURE", timeframe="1D", quantity=30, token=FUT_KEY, trading_symbol="BANKNIFTY01JAN00FUT",
                                            exch_seg="NFO", from_date="2026-01-01T00:00:00", added_at="2026-01-01T00:00:00", owner="u1")
    run(engine.refresh_one(engine.instruments["x"]))
    assert "Upstox" in engine.errors["x"] and "Connect with Upstox" in engine.errors["x"] and "x" in engine.faults


def test_instruments_saved_for_angel_one_are_left_alone_not_used_and_not_lost(tmp_path, monkeypatch):
    import json
    angel = {"id": "old", "symbol": "BANKNIFTY", "instrument_type": "FUTURE", "timeframe": "1D", "quantity": 30, "token": "48699", "trading_symbol": "BANKNIFTY27OCT26FUT",
             "exch_seg": "NFO", "from_date": "2026-01-01T00:00:00", "added_at": "2026-01-01T00:00:00", "owner": "u1"}
    mine = {**angel, "id": "new", "token": FUT_KEY, "owner": "u2"}
    path = tmp_path / "w.json"
    path.write_text(json.dumps([angel, mine]))
    monkeypatch.setattr(em, "STATE_PATH", path)
    e = em.PaperEngine(FakeUpstox())
    assert sorted(e.instruments) == ["new"]                                        # the Angel One token is never sent to Upstox
    e._save()
    assert sorted(i["id"] for i in json.loads(path.read_text())) == ["new", "old"]      # and it is still in the file for the day Angel One is the source again
    assert sorted(em.PaperEngine(FakeUpstox()).instruments) == ["new"]


def test_the_health_board_does_not_call_the_switched_off_option_download_a_problem_on_upstox(engine, monkeypatch):
    from app.ops import context, health
    monkeypatch.setattr(context, "engine", engine)
    t = health.collector_tile()
    assert t["status"] == health.NA and "Upstox" in t["reason"]
    monkeypatch.setattr(context, "engine", None)                      # no engine / Angel One: the old checks apply (they never answer N/A)
    assert health.collector_tile()["status"] != health.NA
