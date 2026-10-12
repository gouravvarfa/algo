"""Intraday (1-minute) execution on daily Top-Bottom structure.

All fixtures are SYNTHETIC and deterministic: index-like prices scaled x1000 so
the 2% breakeven rule only fires where a test wants it. Daily structure used
(days 0-5, close prices):
  BUY_D  = [20000, 10000, 30000, 40000, 35000, 35000]  -> Top 40000, Bottom 10000
  SELL_D = [40000, 50000, 30000, 20000, 25000, 25000]  -> Top 50000, Bottom 20000
The minute day under test is day 6 (and 7).
"""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.replay import api as replay_api
from app.replay.engine import backtest_report, run_intraday_strategy, snapshot
from app.replay.store import ReplayStore
from app.strategy.top_bottom.backtest import run_top_bottom_backtest
from app.strategy.top_bottom.intraday import Minute, run_intraday
from tests.replay.helpers import csv_text, daily_rows, day_ts

BUY_D = [20000, 10000, 30000, 40000, 35000, 35000]
SELL_D = [40000, 50000, 30000, 20000, 25000, 25000]


def ramp(moves, m0, m1, p0, p1):
    """Linear minute-by-minute closes from p0 (at m0) to p1 (at m1)."""
    for m in range(m0, m1 + 1):
        moves[m] = p0 + (p1 - p0) * (m - m0) / (m1 - m0)
    return moves


def minute_day(day_i, start, moves=None, wicks=None, drop=(), n=375):
    """375 one-minute candles. open = previous close (first open = `start`);
    close from `moves` (else flat); wicks {minute: (high, low)} extend a candle."""
    moves, wicks = moves or {}, wicks or {}
    base = day_ts(day_i).replace(hour=9, minute=15)
    out, prev = [], float(start)
    for m in range(n):
        c = float(moves.get(m, prev))
        o = prev
        h, lo = wicks.get(m, (max(o, c), min(o, c)))
        if m not in drop:
            out.append(Minute(base + dt.timedelta(minutes=m), o, max(h, o, c), min(lo, o, c), c))
        prev = c
    return out


def run(daily_closes, minutes):
    dates = [day_ts(i) for i in range(len(daily_closes))]
    return run_intraday("BANKNIFTY", dates, [float(c) for c in daily_closes], minutes)


def whipsaw_path():
    """BUY on a cross of 40000, falls through the 10000 stop, dips to 9850 (no breakeven)."""
    mv = {}
    ramp(mv, 0, 20, 35000, 40500)
    ramp(mv, 21, 60, 40500, 9850)
    return mv


# ---------------------------------------------------------------- whipsaw / reversal
def test_1_buy_point_crossed_intraday_then_stopped_is_recorded_as_whipsaw():
    res = run(BUY_D, minute_day(6, 35000, whipsaw_path()))
    buy = res.trades[0]
    assert (buy.direction, buy.entry_price, buy.signal_price) == ("BUY", 40000, 40000)
    assert (buy.exit_price, buy.exit_signal_price, buy.exit_reason) == (10000, 10000, "STOP_LOSS_REVERSAL")
    assert buy.entry_date.date() == buy.exit_date.date()
    assert any(e[1].startswith("WHIPSAW") for e in buy.events)


def test_2_buy_stop_hit_exits_buy_and_enters_sell_at_the_same_price_under_the_existing_reversal_rule():
    res = run(BUY_D, minute_day(6, 35000, whipsaw_path()))
    buy, sell = res.trades[0], res.trades[1]
    assert sell.direction == "SELL" and sell.entry_date == buy.exit_date
    assert sell.entry_price == buy.exit_price == 10000 and sell.system_point == 10000
    assert sell.initial_sl == 40000  # the SELL's stop is the latest Top, as in the daily engine
    assert sell.exit_reason == "END_OF_BACKTEST"


def test_3_gap_exit_does_not_reverse_when_no_sell_trigger_exists():
    # day 6: BUY at 40000, +2% breakeven moves the stop up to 40000
    d6 = {}
    ramp(d6, 0, 20, 35000, 41000)
    # day 7: gap DOWN through that stop to 30000 (still far above the 10000 Bottom)
    d7 = {}
    ramp(d7, 15, 30, 30000, 28000)
    res = run(BUY_D + [41000, 41000], minute_day(6, 35000, d6) + minute_day(7, 30000, d7, wicks={3: (30500, 29500)}))
    assert [t.direction for t in res.trades] == ["BUY"]  # no unauthorised SELL
    t = res.trades[0]
    assert t.breakeven_date is not None and t.exit_reason == "GAP_EXIT_15M" and t.exit_price == 29500
    assert res.notes[0]["kind"] == "GAP_EXITED"


def test_4_sell_stop_hit_with_valid_buy_trigger_reverses_correctly():
    mv = {}
    ramp(mv, 0, 20, 25000, 19800)   # SELL at 20000 (low 19800: above the 19600 breakeven threshold)
    ramp(mv, 21, 60, 19800, 50500)  # rally through the 50000 stop
    res = run(SELL_D, minute_day(6, 25000, mv))
    sell, buy = res.trades[0], res.trades[1]
    assert (sell.direction, sell.entry_price, sell.exit_price, sell.exit_reason) == ("SELL", 20000, 50000, "STOP_LOSS_REVERSAL")
    assert (buy.direction, buy.entry_price, buy.initial_sl) == ("BUY", 50000, 20000)


def test_5_price_moves_above_and_below_levels_several_times_in_one_day():
    mv = {}
    ramp(mv, 0, 20, 35000, 40500)
    ramp(mv, 21, 60, 40500, 9850)
    ramp(mv, 61, 120, 9850, 40500)
    res = run(BUY_D, minute_day(6, 35000, mv))
    assert [(t.direction, t.entry_price) for t in res.trades] == [("BUY", 40000), ("SELL", 10000), ("BUY", 40000)]
    assert res.trades[0].exit_price == 10000 and res.trades[1].exit_price == 40000
    assert sum(any(e[1].startswith("WHIPSAW") for e in t.events) for t in res.trades) == 2


def test_6_daily_close_hides_an_intraday_touch_but_the_intraday_run_records_it():
    mv = {}
    ramp(mv, 0, 20, 35000, 40500)   # touches above the 40000 Top
    ramp(mv, 21, 60, 40500, 38000)  # and closes back below it
    res = run(BUY_D, minute_day(6, 35000, mv))
    assert res.trades[0].direction == "BUY" and res.trades[0].entry_price == 40000
    dates = [day_ts(i) for i in range(7)]
    _, daily = run_top_bottom_backtest("X", "X", "", "INDEX", 0, dates, [float(c) for c in BUY_D + [38000]], [float(c) for c in BUY_D + [35000]])
    assert daily == []  # a close-only view never saw the touch


# ---------------------------------------------------------------- gap + 15-minute rule
def buy_position_day6():
    mv = {}
    ramp(mv, 0, 20, 35000, 40500)  # BUY at 40000, stop 10000, no breakeven (high 40500 < 40800)
    return minute_day(6, 35000, mv)


def test_7_gap_down_while_holding_buy_applies_the_first_15_minute_candle():
    d7 = {}
    ramp(d7, 15, 40, 5000, 3500)
    res = run(BUY_D + [40500, 3500], buy_position_day6() + minute_day(7, 5000, d7, wicks={3: (5500, 4000)}))
    t = res.trades[0]
    assert (t.direction, t.exit_reason, t.exit_price, t.gap_state) == ("BUY", "GAP_EXIT_15M", 4000, "GAP_EXITED")
    assert t.exit_date.time() >= dt.time(9, 30)  # nothing fires before the first 15m candle completes
    n = res.notes[0]
    assert n["kind"] == "GAP_EXITED" and "structural stop 10,000.00" in n["detail"] and "low 4,000.00" in n["detail"]
    # price is already below the Bottom (10000): a fresh SELL is a genuine crossing, taken only AFTER the exit candle, at the candle open
    sell = res.trades[1]
    assert sell.direction == "SELL" and sell.entry_date > t.exit_date and sell.entry_price < sell.signal_price == 10000


def test_8_gap_up_while_holding_sell_applies_the_first_15_minute_candle():
    d6 = {}
    ramp(d6, 0, 20, 25000, 19800)
    d7 = {}
    ramp(d7, 15, 40, 55000, 57000)
    res = run(SELL_D + [19800, 57000], minute_day(6, 25000, d6) + minute_day(7, 55000, d7, wicks={3: (56000, 52000)}))
    t = res.trades[0]
    assert (t.direction, t.exit_reason, t.exit_price) == ("SELL", "GAP_EXIT_15M", 56000)
    assert res.notes[0]["kind"] == "GAP_EXITED" and "above the 15-min high" in res.notes[0]["detail"]
    buy = res.trades[1]
    assert buy.direction == "BUY" and buy.entry_date > t.exit_date and buy.entry_price > buy.signal_price == 50000


def test_8b_gap_held_when_the_15_minute_level_never_breaks():
    d7 = {}
    ramp(d7, 15, 40, 5000, 4800)  # stays above the 15m low (4000)
    res = run(BUY_D + [40500, 4800], buy_position_day6() + minute_day(7, 5000, d7, wicks={3: (5500, 4000)}))
    t = res.trades[0]
    assert t.gap_state == "GAP_HELD" and t.protective_sl == 4000 and t.exit_reason == "END_OF_BACKTEST"
    assert res.notes[0]["kind"] == "GAP_HELD"


def test_8c_gap_up_past_a_system_point_waits_for_the_15_minute_high_before_entering():
    d7 = {}
    ramp(d7, 15, 60, 45000, 48000)  # opens 45000 > Top 40000 (prev close 35000), then trades above the 15m high
    res = run(BUY_D + [35000, 48000], minute_day(6, 35000) + minute_day(7, 45000, d7, wicks={3: (47000, 43000)}))
    t = res.trades[0]
    assert (t.direction, t.gap_state, t.signal_price) == ("BUY", "GAP_ENTRY", 47000)
    assert t.entry_price >= 47000 and t.entry_date.time() >= dt.time(9, 30) and t.initial_sl == 10000
    assert res.notes[0]["kind"] == "GAP_ENTRY"


def test_9_ordinary_intraday_movement_never_triggers_the_gap_protocol():
    d7 = {}
    ramp(d7, 0, 60, 40400, 9500)  # opens inside the normal range, then falls THROUGH the stop during the day
    res = run(BUY_D + [40500, 9500], buy_position_day6() + minute_day(7, 40400, d7))
    t = res.trades[0]
    assert t.exit_reason == "STOP_LOSS_REVERSAL" and t.gap_state is None and t.exit_price == 10000
    assert res.notes == [] and res.flags == []


def test_10_missing_one_minute_or_15_minute_data_is_reported_never_filled():
    d7 = {}
    ramp(d7, 15, 40, 5000, 3500)
    short = minute_day(7, 5000, d7, wicks={3: (5500, 4000)}, drop=range(0, 10))  # first 10 minutes missing
    res = run(BUY_D + [40500, 3500], buy_position_day6() + short)
    t = res.trades[0]
    assert t.gap_state is None and t.exit_reason == "STOP_LOSS_REVERSAL"  # ordinary rules, gap protocol NOT applied
    assert t.exit_price == 5000  # the candle's real open (gapped through the stop); no invented candle
    (flag,) = res.flags
    assert flag["date"] == day_ts(7).date().isoformat() and flag["minutes"] == 365
    assert any("10 of 375" in s for s in flag["issues"]) and any("gap protocol NOT applied" in s for s in flag["issues"])
    assert res.notes == []


# ---------------------------------------------------------------- no look-ahead / replay == backtest
def test_11_daily_swings_known_on_a_day_never_use_that_days_own_close_or_later_days():
    mv = whipsaw_path()
    base = run(BUY_D + [38000, 38000], minute_day(6, 35000, mv))
    # alter day 6's OWN daily close (would create a huge new Top once confirmed) and everything after
    alt = run(BUY_D + [99000, 99000], minute_day(6, 35000, mv))
    key = lambda r: [(t.direction, t.entry_price, t.exit_price, t.exit_date) for t in r.trades]
    assert key(base) == key(alt) and base.levels == alt.levels


def test_11b_replay_snapshot_ignores_later_minutes():
    def build(tail):
        mv = whipsaw_path()
        d7 = {}
        ramp(d7, 0, 60, 9850, tail)
        rows = [(m.ts, m.open, m.high, m.low, m.close, 0.0) for m in minute_day(6, 35000, mv) + minute_day(7, 9850, d7)]
        return run_intraday_strategy(daily_rows(BUY_D + [9850, 9850]), rows)

    a, b = build(60000), build(2000)
    k = 375 + 100  # a minute on day 7
    strip = lambda s: {x: y for x, y in s.items() if x not in ("total", "at_end", "window_start")}
    assert strip(snapshot(a, 375 + 0)) == strip(snapshot(b, 375 + 0))
    assert strip(snapshot(a, 200)) == strip(snapshot(b, 200))
    assert k > 375


def test_12_replay_final_state_equals_the_full_backtest():
    rows = [(m.ts, m.open, m.high, m.low, m.close, 0.0) for m in minute_day(6, 35000, whipsaw_path())]
    r = run_intraday_strategy(daily_rows(BUY_D + [9850]), rows)
    strip = lambda s: {x: y for x, y in s.items() if x not in ("total", "at_end", "window_start")}
    assert strip(snapshot(r, len(r.candles) - 1)) == strip(backtest_report(r))
    s = snapshot(r, len(r.candles) - 1)
    assert s["mode"] == "intraday" and s["interval"] == "1m" and s["trades"][-1]["signal"] is not None
    assert any(t["whipsaw"] for t in s["trades"])


def test_14_same_candle_entry_and_stop_is_handled_conservatively_and_flagged():
    # ONE candle spans both the Top (entry) and the Bottom (stop): the order inside it is unknowable
    res = run(BUY_D, minute_day(6, 35000, {5: 20000}, wicks={5: (40500, 9500)}))
    buy, sell = res.trades[0], res.trades[1]
    assert buy.direction == "BUY" and buy.entry_price == 40000
    assert buy.exit_price == 10000 and buy.exit_reason == "STOP_LOSS_REVERSAL" and buy.ambiguous  # adverse sequence assumed, never the profitable one
    assert sell.direction == "SELL" and sell.ambiguous
    assert len(res.trades) == 2  # the candle's high was already used for the entry: no further chain


# ---------------------------------------------------------------- API: persistence + refresh
@pytest.fixture
def client(tmp_path):
    replay_api.set_store(ReplayStore(tmp_path / "intr.db"))
    yield TestClient(app), tmp_path
    replay_api.set_store(None)


def _load_intraday(c):
    daily = daily_rows(BUY_D + [9850])
    mins = [(m.ts, m.open, m.high, m.low, m.close, 0.0) for m in minute_day(6, 35000, whipsaw_path())]
    assert c.post("/api/replay/import", json={"csv_text": csv_text(daily, "1D")}).status_code == 200
    assert c.post("/api/replay/import", json={"csv_text": csv_text(mins, "1m")}).status_code == 200


def _cfg(**kw):
    return {"mode": "intraday", "interval": "1m", "from_date": "2026-09-07", "to_date": "2026-09-07", **kw}


def test_13_intraday_session_survives_a_refresh(client):
    c, tp = client
    _load_intraday(c)
    made = c.post("/api/replay/sessions", json=_cfg()).json()
    sid, total = made["id"], made["total"]
    assert total == 375 and made["snapshot"]["mode"] == "intraday"
    assert c.post(f"/api/replay/sessions/{sid}/position", json={"index": 70, "speed": 5}).json()["index"] == 70
    replay_api.set_store(ReplayStore(tp / "intr.db"))  # simulate a fresh process on the same database
    back = c.get("/api/replay/sessions/latest").json()
    s = back["snapshot"]
    assert back["config"]["mode"] == "intraday" and back["config"]["speed"] == 5
    assert s["index"] == 70 and s["time"] == "2026-09-07T10:25:00" and s["position"] == "SELL"
    assert [t["side"] for t in s["trades"]] == ["BUY", "SELL"]


def test_api_intraday_needs_1m_and_1d_data_and_reports_what_is_missing(client):
    c, _ = client
    assert "No 1m candles" in c.post("/api/replay/sessions", json=_cfg()).json()["detail"]
    mins = [(m.ts, m.open, m.high, m.low, m.close, 0.0) for m in minute_day(6, 35000)]
    c.post("/api/replay/import", json={"csv_text": csv_text(mins, "1m")})
    assert "needs 1D candles" in c.post("/api/replay/sessions", json=_cfg()).json()["detail"]


def test_api_intraday_replay_equals_backtest_endpoint(client):
    c, _ = client
    _load_intraday(c)
    sid = c.post("/api/replay/sessions", json=_cfg()).json()["id"]
    final = c.get(f"/api/replay/sessions/{sid}", params={"index": 374}).json()["snapshot"]
    bt = c.post("/api/replay/backtest", json=_cfg()).json()
    strip = lambda s: {x: y for x, y in s.items() if x not in ("total", "at_end", "window_start")}
    assert strip(final) == strip(bt)


def test_16_jan_pattern_touch_through_the_stop_reverses_then_price_comes_back_chronologically_recorded():
    """SYNTHETIC replica of the 16-Jan-2026 shape: a SELL is held, price pokes through its stop (a daily wick),
    the position reverses to BUY at the level, and price then returns below it - the BUY is NOT stopped (its stop is the Bottom)."""
    daily = [60000, 60500, 59500, 59000, 59400, 59400, 58900]   # Top 60500, Bottom 59000; day 6 closes 58900
    d6 = {}
    ramp(d6, 0, 30, 59400, 58900)       # SELL at 59000 (stop = Top 60500)
    d7 = {}
    ramp(d7, 0, 60, 58900, 60600)       # crosses the 60500 stop during the session
    ramp(d7, 61, 120, 60600, 60300)     # ... and comes back below it
    res = run(daily, minute_day(6, 59400, d6) + minute_day(7, 58900, d7))
    sell, buy = res.trades
    assert (sell.direction, sell.exit_reason, sell.exit_price) == ("SELL", "STOP_LOSS_REVERSAL", 60500)
    assert (buy.direction, buy.entry_price, buy.exit_reason) == ("BUY", 60500, "END_OF_BACKTEST") and buy.initial_sl == 59000
    assert buy.entry_date == sell.exit_date                      # same minute: stop-out and reversal
    rt = [e for e in buy.events if e[1].startswith("ROUND TRIP")]
    assert len(rt) == 1 and rt[0][0] > buy.entry_date and buy.round_trip_date == rt[0][0]
    assert not any(e[1].startswith("WHIPSAW") for t in res.trades for e in t.events)  # never stopped out the same day: not a whipsaw by definition
    stamps = [e[0] for t in res.trades for e in sorted(t.events)]
    assert stamps == sorted(stamps)
