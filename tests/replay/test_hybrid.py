"""'Daily + hidden 1-minute' engine: the user sees and steps through DAILY candles; the 1-minute candles of each session are
processed internally and surface as an exact-timestamp event log. SYNTHETIC fixtures (prices scaled like test_intraday.py)."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.replay import api as replay_api
from app.replay.engine import backtest_report, run_hybrid_strategy, snapshot
from app.replay.store import ReplayStore
from app.strategy.top_bottom.gap_protocol import FirstCandle
from tests.replay.helpers import csv_text, daily_rows, day_ts
from tests.replay.test_intraday import BUY_D, minute_day, ramp, whipsaw_path

SELL_HELD = [60000, 60500, 59500, 59000, 59400, 59400, 58900]


def rows(minutes):
    return [(m.ts, m.open, m.high, m.low, m.close, 0.0) for m in minutes]


def hybrid(daily_closes, minutes, **kw):
    first15 = kw.pop("first15", None)
    return run_hybrid_strategy(daily_rows(daily_closes, **kw), rows(minutes), 0.0, first15)


def last(run, window=None):
    return snapshot(run, len(run.candles) - 1, window=window or len(run.candles))


def types(snap):
    return [e["type"] for e in snap["event_log"]]


def sixteen_jan_shape():
    d7 = {}
    ramp(d7, 0, 60, 58900, 60600)       # pokes through the carried-in SELL's stop 60500 ...
    ramp(d7, 61, 120, 60600, 60300)     # ... and the Daily close ends BELOW the level
    return hybrid(SELL_HELD + [60300], minute_day(7, 58900, d7))


def test_only_daily_candles_are_shown_one_step_per_daily_session():
    d7 = {}
    ramp(d7, 0, 60, 58900, 60600)
    run = hybrid(SELL_HELD + [60300, 60100], minute_day(7, 58900, d7) + minute_day(8, 60300))
    assert run.interval == "1D" and len(run.candles) == 2          # 750 one-minute candles, but 2 playback steps
    s0 = snapshot(run, 0)
    assert s0["interval"] == "1D" and [c["time"][:10] for c in s0["candles"]] == [day_ts(7).date().isoformat()]
    stored = daily_rows(SELL_HELD + [60300, 60100])[7]
    assert (s0["candles"][0]["open"], s0["candles"][0]["high"], s0["candles"][0]["low"], s0["candles"][0]["close"]) == stored[1:5]
    assert len(snapshot(run, 1)["candles"]) == 2


def test_16_jan_shape_daily_close_below_the_level_does_not_erase_the_intraday_crossing():
    s = last(sixteen_jan_shape())
    assert s["candles"][-1]["close"] == 60300 < 60500           # the visible Daily candle closes below the level
    assert [t["side"] for t in s["trades"]] == ["SELL", "BUY"]  # ... yet the crossing was processed
    sell, buy = s["trades"]
    assert sell["carried_in"] and sell["reason"] == "STOP_LOSS_REVERSAL" and sell["exit"] == 60500
    assert buy["entry"] == 60500 and buy["exit"] is None and s["position"] == "BUY"
    assert any("Position carried in: SELL" in w for w in s["warnings"])


def test_event_log_is_chronological_with_exact_timestamps_and_round_trip_does_not_close_the_position():
    s = last(sixteen_jan_shape())
    assert types(s) == ["STOP_EXIT", "REVERSAL_ENTRY", "ROUND_TRIP"]
    stamps = [e["time"] for e in s["event_log"]]
    assert stamps == sorted(stamps) and stamps[0] == stamps[1] and stamps[2] > stamps[1]
    assert all(t.startswith(day_ts(7).date().isoformat() + "T") for t in stamps)   # real minute timestamps, not day-level
    assert s["position"] == "BUY" and s["open_trade"]["stop"] == 59000             # still held; its stop (Bottom) was never hit


def test_whipsaw_sequence_is_recorded_in_order():
    run = hybrid(BUY_D + [38000], minute_day(6, 35000, whipsaw_path()))
    s = last(run)
    assert types(s) == ["SYSTEM_POINT_CROSSED", "BUY_ENTRY", "ROUND_TRIP", "STOP_EXIT", "WHIPSAW_STOP_OUT", "REVERSAL_ENTRY"]
    assert [e["price"] for e in s["event_log"] if e["type"] in ("SYSTEM_POINT_CROSSED", "BUY_ENTRY", "STOP_EXIT")] == [40000, 40000, 10000]


def test_breakeven_activation_exit_and_waiting_state_are_in_the_log_and_nothing_reenters():
    mv = {}
    ramp(mv, 0, 20, 35000, 40500)
    ramp(mv, 21, 40, 40500, 41200)
    ramp(mv, 41, 80, 41200, 39500)
    ramp(mv, 81, 140, 39500, 41500)     # re-crosses the consumed Top: nothing happens
    s = last(hybrid(BUY_D + [41500], minute_day(6, 35000, mv)))
    assert types(s) == ["SYSTEM_POINT_CROSSED", "BUY_ENTRY", "BREAKEVEN_ACTIVATED", "BREAKEVEN_EXIT_AFTER_2_PERCENT_PROFIT"]
    assert s["position"] == "FLAT" and s["waiting"] and len(s["trades"]) == 1


def test_gap_protocol_event_uses_the_stored_15_minute_candle_when_present_and_says_so():
    d6 = {}
    ramp(d6, 0, 20, 35000, 40500)
    d7 = {}
    ramp(d7, 15, 40, 5000, 3500)
    mins = minute_day(6, 35000, d6) + minute_day(7, 5000, d7, wicks={3: (5500, 4000)})
    stored = {day_ts(7).date(): FirstCandle(high=5500, low=4000, open=5000)}
    with_15m = last(hybrid(BUY_D + [40500, 3500], mins, first15=stored))
    derived = last(hybrid(BUY_D + [40500, 3500], mins))
    gap = [e for e in with_15m["event_log"] if e["type"] == "GAP_PROTOCOL"]
    assert len(gap) == 1 and "stored 15-minute candle" in gap[0]["text"] and "low 4,000.00" in gap[0]["text"]
    assert "derived from the genuine 1-minute candles" in [e for e in derived["event_log"] if e["type"] == "GAP_PROTOCOL"][0]["text"]
    assert with_15m["trades"][0]["reason"] == "GAP_EXIT_15M"


def test_ordinary_sessions_do_not_get_the_15_minute_protocol():
    d7 = {}
    ramp(d7, 0, 60, 40400, 9500)
    run = hybrid(BUY_D + [40500, 9500], minute_day(6, 35000, {**ramp({}, 0, 20, 35000, 40500)}) + minute_day(7, 40400, d7))
    assert "GAP_PROTOCOL" not in types(last(run))


def test_missing_intraday_and_15_minute_data_is_reported_never_filled():
    d7 = {}
    ramp(d7, 15, 40, 5000, 3500)
    short = minute_day(7, 5000, d7, wicks={3: (5500, 4000)}, drop=range(0, 10))
    s = last(hybrid(BUY_D + [40500, 3500], minute_day(6, 35000, ramp({}, 0, 20, 35000, 40500)) + short))
    msgs = [e["text"] for e in s["event_log"] if e["type"] == "INTRADAY_DATA_INCOMPLETE"]
    assert any("10 of 375" in m for m in msgs) and any("gap protocol NOT applied" in m for m in msgs)
    assert s["trades"][0]["reason"] == "STOP_LOSS_REVERSAL"   # ordinary rules; no gap protocol on an incomplete candle
    # a Daily session with NO 1-minute data at all is flagged too (its intraday touches were not processed)
    gap_run = hybrid(BUY_D + [40500, 40000, 39000], minute_day(6, 35000) + minute_day(8, 39000))
    assert any("NOT processed" in e["text"] for e in last(gap_run)["event_log"])
    assert any("incomplete 1-minute data" in w for w in last(gap_run)["warnings"])


def test_ambiguous_same_candle_is_labelled():
    s = last(hybrid(BUY_D + [20000], minute_day(6, 35000, {5: 20000}, wicks={5: (40500, 9500)})))
    assert "AMBIGUOUS" in types(s) and s["trades"][0]["ambiguous"]


def test_no_future_sessions_are_visible_and_replay_equals_backtest():
    run = sixteen_jan_shape()
    d8 = {}
    ramp(d8, 0, 120, 60300, 59000)   # a later session (BUY stopped at 59000? no: only the Bottom) must stay hidden
    run2 = hybrid(SELL_HELD + [60300, 59000], minute_day(7, 58900, {**ramp({}, 0, 60, 58900, 60600), **ramp({}, 61, 120, 60600, 60300)}) + minute_day(8, 60300, d8))
    strip = lambda s: {k: v for k, v in s.items() if k not in ("total", "at_end", "window_start")}
    assert strip(snapshot(run2, 0)) == strip(snapshot(run, 0))                      # day 7 identical whatever happens on day 8
    assert strip(snapshot(run2, 1, window=2)) == strip(backtest_report(run2))      # final replay state == full backtest


# ---------------------------------------------------------------- API: the user only picks the engine
@pytest.fixture
def client(tmp_path):
    replay_api.set_store(ReplayStore(tmp_path / "hyb.db"))
    yield TestClient(app), tmp_path
    replay_api.set_store(None)


def test_api_hybrid_needs_no_dates_steps_daily_and_survives_refresh(client):
    c, tp = client
    d7 = {}
    ramp(d7, 0, 60, 58900, 60600)
    ramp(d7, 61, 120, 60600, 60300)
    mins = minute_day(7, 58900, d7) + minute_day(8, 60300)
    assert c.post("/api/replay/import", json={"csv_text": csv_text(daily_rows(SELL_HELD + [60300, 60100]), "1D")}).status_code == 200
    assert c.post("/api/replay/import", json={"csv_text": csv_text(rows(mins), "1m")}).status_code == 200
    plan = c.get("/api/replay/plan", params={"mode": "hybrid"}).json()
    assert (plan["interval"], plan["candles"], plan["minute_candles"]) == ("1D", 2, 750)
    r = c.post("/api/replay/sessions", json={"mode": "hybrid"}).json()
    assert r["total"] == 2 and r["config"]["interval"] == "1D" and r["snapshot"]["mode"] == "hybrid"
    sid = r["id"]
    c.post(f"/api/replay/sessions/{sid}/position", json={"index": 1, "speed": 5})
    replay_api.set_store(ReplayStore(tp / "hyb.db"))
    back = c.get("/api/replay/sessions/latest").json()
    assert back["snapshot"]["index"] == 1 and back["config"]["speed"] == 5 and back["snapshot"]["position"] == "BUY"
    prev = c.get("/api/replay/preview", params={"mode": "hybrid"}).json()
    assert prev["interval"] == "1D" and len(prev["candles"]) == 2          # pick-start shows Daily candles too
    bt = c.post("/api/replay/backtest", json={"mode": "hybrid"}).json()
    final = c.get(f"/api/replay/sessions/{sid}", params={"index": 1, "window": 2}).json()["snapshot"]
    strip = lambda s: {k: v for k, v in s.items() if k not in ("total", "at_end", "window_start")}
    assert strip(final) == strip(bt)
