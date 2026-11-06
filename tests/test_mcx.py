"""MCX gold / silver in the Top-Bottom engine: own session (09:00), rupee value of a point, contract list, same index rules as BankNifty. Candles are SYNTHETIC fixtures."""
import asyncio
import datetime as dt

import pytest

from app.paper_trading import engine as eng
from app.providers import angelone_scrip_master as sm
from app.providers.commodities import COMMODITIES
from app.strategy.top_bottom.gap_protocol import MCX, NSE, Bar15, resolve_gap_entry, resolve_gap_exit
from app.strategy.top_bottom.intraday import Minute, run_intraday


def inst(**kw):
    base = dict(id="x", symbol="GOLDM", instrument_type="FUTURE", timeframe="1D", quantity=100, lots=1, lot_size=100, token="1", trading_symbol="GOLDM04DEC26FUT",
                exch_seg="MCX", expiry="2026-12-04", from_date="2026-01-01", added_at="2026-11-01")
    base.update(kw)
    return eng.Instrument(**base)


def test_an_old_saved_instrument_without_the_new_fields_is_an_nse_one():
    i = inst(symbol="RELIANCE", exch_seg="NFO", quantity=500, lot_size=500)
    assert i.session == "NSE" and i.point_value is None and i.sess is NSE and i.pnl_units == 500


def test_mcx_point_value_decides_the_rupees_not_the_lot_size():
    # GOLD: 1 kg lot quoted per 10 g -> Rs 100 per point per lot; GOLDM: 100 g lot -> Rs 10; 3 lots of GOLDM -> 30
    assert inst(symbol="GOLD", quantity=1, lot_size=1, lots=1, session="MCX", point_value=100).pnl_units == 100
    assert inst(session="MCX", point_value=10, lots=3, quantity=300).pnl_units == 30
    # without a point value it stays units x points, as before
    assert inst(lots=3, quantity=300).pnl_units == 300


def test_every_listed_commodity_has_a_whole_positive_point_value():
    assert set(COMMODITIES) == {"GOLD", "GOLDM", "GOLDTEN", "GOLDGUINEA", "GOLDPETAL", "SILVER", "SILVERM", "SILVERMIC", "SILVER100"}
    assert all(isinstance(v["point_value"], int) and v["point_value"] > 0 for v in COMMODITIES.values())


def test_mcx_market_hours():
    mon = dt.datetime(2026, 11, 2)
    at = lambda h, m: mon.replace(hour=h, minute=m)
    assert eng.market_open(at(20, 0), MCX) and not eng.market_open(at(20, 0))             # NSE is closed at 20:00, MCX is not
    assert not eng.market_open(at(8, 59), MCX) and eng.market_open(at(9, 0), MCX) and eng.market_open(at(23, 30), MCX)
    assert not eng.market_open(dt.datetime(2026, 11, 7, 12, 0), MCX)                      # Saturday
    assert eng.market_open(at(9, 15)) and not eng.market_open(at(9, 14))                  # NSE is unchanged


def test_gold_and_silver_follow_the_index_rules_not_the_stock_rules():
    assert eng.breakeven_pct_for("GOLD") is None and eng.breakeven_pct_for("SILVERMIC") is None and eng.breakeven_pct_for("RELIANCE") == 0.03
    assert eng.struct_cap_for("GOLDM") == eng.struct_cap_for("BANKNIFTY") and eng.struct_cap_for("RELIANCE") == eng.STOCK_STRUCT_CAP


def test_the_engine_sweeps_fast_while_an_mcx_strategy_is_open():
    e = eng.PaperEngine.__new__(eng.PaperEngine)
    e.instruments = {"a": inst(session="MCX")}
    evening = dt.datetime(2026, 11, 2, 20, 0)
    real = eng.now_ist
    try:
        eng.now_ist = lambda: evening
        assert e._any_market_open()
        e.instruments = {"a": inst(symbol="RELIANCE", exch_seg="NFO", session="NSE")}
        assert not e._any_market_open()                     # NSE only: closed in the evening, as before
    finally:
        eng.now_ist = real


# ---- the gap protocol on the MCX day (first candle 09:00-09:15, later candles from 09:15)
def bars(day, rows, start=(9, 0)):
    t = dt.datetime(2026, 11, day, *start)
    return [Bar15(t + dt.timedelta(minutes=15 * i), *r) for i, r in enumerate(rows)]


def test_the_mcx_first_candle_is_the_09_00_one_and_nse_bars_are_not_taken_for_it():
    b = bars(3, [(100, 102, 99, 101), (101, 101.5, 98, 98.5)])
    assert resolve_gap_exit("BUY", b, session=MCX).status == "GAP_EXITED"                 # 09:15 candle breaks the first candle's low (99)
    assert resolve_gap_entry("SELL", b, session=MCX).status == "TRIGGERED"
    assert resolve_gap_exit("BUY", b).status == "GAP_HELD"                                 # NSE rule: the 09:15 candle is "the first one" and nothing comes after it, so nothing breaks it
    nse = bars(3, [(100, 102, 99, 101), (101, 101.5, 98, 98.5)], start=(9, 15))
    assert resolve_gap_exit("BUY", nse).status == "GAP_EXITED"                             # unchanged for NSE


def test_a_mcx_day_is_over_at_23_55_not_15_30():
    b = bars(3, [(100, 102, 99, 101), (101, 101.5, 100, 101)])
    held = lambda hh, mm: resolve_gap_exit("BUY", b, dt.datetime(2026, 11, 3, hh, mm), session=MCX).status
    assert held(16, 0) == "GAP_EXIT_PENDING" and held(23, 55) == "GAP_HELD"               # NSE would call it "held" at 15:30


# ---- the intraday engine on MCX minutes
D = lambda day: dt.datetime(2026, 2, day)
HIST = ([dt.datetime(2026, 2, 20 + i) for i in range(6)], [100, 110, 100, 90, 100, 108])          # Top 110 and Bottom 90 are confirmed


def minute_day(day, start_h, start_m, prices):
    t = dt.datetime(2026, 3, day, start_h, start_m)
    return [Minute(t + dt.timedelta(minutes=i), p, p, p, p) for i, p in enumerate(prices)]


def test_mcx_minutes_are_judged_against_the_mcx_day_not_375_minutes():
    issues = lambda res: [i for f in res.flags for i in f.get("issues", []) if "one-minute candles missing" in i]
    full = run_intraday("GOLD", *HIST, minute_day(6, 9, 0, [105.0] * 870), session=MCX)
    assert issues(full) == []                                                                 # a whole MCX day is complete
    short = run_intraday("GOLD", *HIST, minute_day(6, 9, 0, [105.0] * 500), session=MCX)
    assert issues(short) == ["370 of 870 one-minute candles missing"]
    nse = run_intraday("BANKNIFTY", *HIST, minute_day(6, 9, 15, [105.0] * 300))
    assert issues(nse) == ["75 of 375 one-minute candles missing"]                           # NSE: unchanged


def test_a_gap_through_the_stop_waits_for_the_09_15_candle_on_mcx():
    # day 1: buy breakout at 110 (stop = Bottom 90); day 2 opens 85 (below the stop) and stays inside its first 15 minutes, then breaks the first candle's low
    d1 = minute_day(6, 9, 0, [105.0] * 16 + [110.5] * 20)
    first = [85.0, 86.0, 84.0] + [85.0] * 12                      # 09:00-09:14: high 86, low 84
    d2 = minute_day(7, 9, 0, first + [83.0, 82.0] + [82.0] * 10)  # 09:15 onwards breaks 84
    res = run_intraday("GOLD", *HIST, d1 + d2, session=MCX)
    buy = next(t for t in res.trades if t.direction == "BUY")
    assert buy.exit_date is not None and buy.exit_date.date() == dt.date(2026, 3, 7)
    assert buy.exit_date.time() >= dt.time(9, 15)                  # nothing fires before the second candle (09:15), as nothing fires before 09:30 on NSE


# ---- the contract list
ROWS = [
    {"exch_seg": "MCX", "instrumenttype": "FUTCOM", "name": "GOLDM", "symbol": "GOLDM05JAN27FUT", "token": "2", "expiry": "05JAN2027", "lotsize": "100", "tick_size": "100.000000"},
    {"exch_seg": "MCX", "instrumenttype": "FUTCOM", "name": "GOLDM", "symbol": "GOLDM04DEC26FUT", "token": "1", "expiry": "04DEC2026", "lotsize": "100", "tick_size": "100.000000"},
    {"exch_seg": "MCX", "instrumenttype": "FUTCOM", "name": "SILVERMIC", "symbol": "SILVERMIC30NOV26FUT", "token": "3", "expiry": "30NOV2026", "lotsize": "1", "tick_size": "100.000000"},
    {"exch_seg": "MCX", "instrumenttype": "FUTCOM", "name": "COPPER", "symbol": "COPPER30NOV26FUT", "token": "4", "expiry": "30NOV2026", "lotsize": "2500", "tick_size": "5.0"},
    {"exch_seg": "MCX", "instrumenttype": "OPTFUT", "name": "GOLDM", "symbol": "GOLDM04DEC26FUT25000CE", "token": "5", "expiry": "04DEC2026", "lotsize": "100", "tick_size": "5.0"},
    {"exch_seg": "NFO", "instrumenttype": "FUTSTK", "name": "RELIANCE", "symbol": "RELIANCE29DEC26FUT", "token": "6", "expiry": "29DEC2026", "lotsize": "500", "tick_size": "5.0"},
]


@pytest.fixture
def master(monkeypatch):
    async def fake():
        return ROWS
    monkeypatch.setattr(sm, "_fetch_scrip_master", fake)


def test_the_contract_search_returns_mcx_gold_and_silver_only_and_nearest_first(master):
    out = asyncio.run(sm.get_future_contracts("GOLDM"))
    assert [c.trading_symbol for c in out] == ["GOLDM04DEC26FUT", "GOLDM05JAN27FUT"]               # options and COPPER are never offered
    assert all(c.exch_seg == "MCX" and c.lot_size == 100 and not c.is_index for c in out)
    assert asyncio.run(sm.get_future_contracts("COPPER")) == []
    assert [c.exch_seg for c in asyncio.run(sm.get_future_contracts("RELIANCE"))] == ["NFO"]      # stock futures are as before
    assert [c.underlying for c in asyncio.run(sm.search_futures("SILVER"))] == ["SILVERMIC"]


# ---- assigning a gold / silver contract through the engine
def test_assigning_goldm_creates_an_mcx_instrument_that_survives_a_restart(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(eng, "STATE_PATH", tmp_path / "w.json")
    monkeypatch.setattr(eng, "JOURNAL_PATH", tmp_path / "j.jsonl")
    monkeypatch.setattr(eng, "LEGACY_OWNER_PATH", tmp_path / "l.txt")

    async def fake_master():
        return ROWS
    monkeypatch.setattr(sm, "_fetch_scrip_master", fake_master)

    class P:
        async def get_session(self): return object()
        def reset_session(self): pass

    async def no_refresh(self, inst):
        return None
    monkeypatch.setattr(eng.PaperEngine, "refresh_one", no_refresh)
    e = eng.PaperEngine(P())
    monkeypatch.setattr(eng, "now_ist", lambda: dt.datetime(2026, 11, 3, 10, 0))
    i = asyncio.run(e.add("GOLDM", "FUTURE", "1D", 2, 300, owner="u1"))
    assert (i.exch_seg, i.session, i.point_value, i.lots, i.lot_size, i.quantity) == ("MCX", "MCX", 10, 2, 100, 200)
    assert i.trading_symbol == "GOLDM04DEC26FUT" and i.expiry == "2026-12-04" and i.pnl_units == 20          # 2 lots x Rs 10 a point
    saved = json.loads((tmp_path / "w.json").read_text())
    assert saved[0]["session"] == "MCX" and saved[0]["point_value"] == 10
    again = eng.PaperEngine(P())
    assert again.instruments[i.id].sess is MCX and again.instruments[i.id].pnl_units == 20
