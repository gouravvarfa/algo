"""Optional 'Minimum System Point Separation' filter (OFF by default) and the excessive-reversal regression.
SYNTHETIC replica of the 2026-05-04 case: Bottom 55,400.35 and Top 55,403.60 (3.25 points apart) confirmed on
consecutive Daily closes, price then oscillating across both inside a narrow range."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.replay import api as replay_api
from app.replay.engine import run_hybrid_strategy, snapshot
from app.replay.store import ReplayStore
from app.strategy.top_bottom.intraday import Minute, run_intraday
from tests.replay.helpers import csv_text, daily_rows, day_ts
from tests.replay.test_intraday import minute_day, ramp, run

CLOSE_POINTS = [56000, 55400.35, 55403.6, 54900, 54900, 54900]    # Bottom 55400.35 (day 1), Top 55403.6 (day 2): 3.25 apart


def storm_minutes():
    mv = {m: (55398.0 if m % 2 else 55407.0) for m in range(40)}   # each 1-minute candle spans both levels
    return minute_day(6, 55402.0, mv)


def fp(res):
    return [(t.direction, t.entry_date, t.entry_price, t.exit_date, t.exit_price, t.exit_reason) for t in res.trades]


def storm(min_separation=None):
    dates = [day_ts(i) for i in range(len(CLOSE_POINTS))]
    return run_intraday("BANKNIFTY", dates, [float(c) for c in CLOSE_POINTS], storm_minutes(), min_separation=min_separation)


# ---------------------------------------------------------------- Phase 1: reproduction
def test_regression_close_system_points_generate_dozens_of_trades_when_the_filter_is_off():
    off = storm()
    assert len(off.trades) > 12                                        # the excessive-reversal case, reproduced
    reversals = [t for k, t in enumerate(off.trades) if k and off.trades[k - 1].exit_reason == "STOP_LOSS_REVERSAL" and off.trades[k - 1].exit_date == t.entry_date]
    assert len(reversals) >= len(off.trades) - 2                        # (almost) every trade is a stop-and-reverse
    assert any(t.ambiguous for t in off.trades)                         # candles spanning both levels -> conservative chains
    top, bot = off.levels[0][1], off.levels[0][2]
    assert (top, bot) == (55403.6, 55400.35) and round(abs(top - bot), 2) == 3.25


# ---------------------------------------------------------------- Phase 2: the filter
def test_default_and_zero_threshold_keep_the_original_sequence_exactly():
    assert fp(storm(None)) == fp(storm(0)) == fp(storm(0.0))


def test_threshold_at_or_below_the_separation_changes_nothing_and_above_it_blocks():
    original = fp(storm())
    assert fp(storm(3.0)) == original and fp(storm(3.25)) == original          # 3.25 is NOT < 3.25: still allowed
    blocked = storm(5.0)
    assert blocked.trades == [] and len(blocked.blocked) >= 1
    assert all(b["separation"] == 3.25 and b["threshold"] == 5.0 and b["kind"] == "ENTRY" for b in blocked.blocked)


def test_the_filter_never_touches_the_system_points_themselves():
    assert storm().levels == storm(5.0).levels == storm(1000.0).levels


def test_a_blocked_reversal_still_closes_the_position_and_leaves_the_strategy_flat():
    mv = {}
    ramp(mv, 0, 30, 54900, 55403.0)    # carried-in SELL (stop 55403.6) creeps up to just below its stop ...
    for m in range(31, 61):
        mv[m] = 55410.0                # ... then one clean candle through it, and price stays above (no storm)
    mins = minute_day(6, 54900, mv)
    dates = [day_ts(i) for i in range(6)]
    from app.replay.engine import reconstruct_state
    initial, _ = reconstruct_state(daily_rows(CLOSE_POINTS), day_ts(6).date())
    assert initial.trade.direction == "SELL" and initial.trade.active_stop == 55403.6
    closes = [float(c) for c in CLOSE_POINTS]
    off = run_intraday("BANKNIFTY", dates, closes, mins, initial)
    on = run_intraday("BANKNIFTY", dates, closes, mins, reconstruct_state(daily_rows(CLOSE_POINTS), day_ts(6).date())[0], min_separation=5.0)
    assert [t.direction for t in off.trades] == ["SELL", "BUY"]                                  # original: stop-and-reverse
    assert [(t.direction, t.exit_reason, t.exit_price) for t in on.trades] == [("SELL", "STOP_LOSS_REVERSAL", 55403.6)]  # exit NOT blocked
    # the reversal entry was refused; the later, genuine re-crossing of the Top is refused for the same reason
    assert [b["kind"] for b in on.blocked] == ["REVERSAL", "ENTRY"] and on.blocked[0]["side"] == "BUY"


def test_wide_separation_is_never_filtered():
    wide = [56000, 50000, 60000, 54900, 54900, 54900]
    mv = {}
    ramp(mv, 0, 60, 54900, 61000)
    dates = [day_ts(i) for i in range(6)]
    a = run_intraday("BANKNIFTY", dates, [float(c) for c in wide], minute_day(6, 54900, mv))
    b = run_intraday("BANKNIFTY", dates, [float(c) for c in wide], minute_day(6, 54900, mv), min_separation=500.0)
    assert fp(a) == fp(b) and not b.blocked


# ---------------------------------------------------------------- visibility + API
def rows(minutes):
    return [(m.ts, m.open, m.high, m.low, m.close, 0.0) for m in minutes]


def test_blocked_entries_are_visible_in_the_event_log_and_the_run_is_labelled_filtered():
    dr = daily_rows(CLOSE_POINTS + [55402])
    plain = run_hybrid_strategy(dr, rows(storm_minutes()))
    filt = run_hybrid_strategy(dr, rows(storm_minutes()), min_separation=5.0)
    s0, s1 = snapshot(plain, 0), snapshot(filt, 0)
    assert s0["filter"] == {"min_separation": None, "blocked": 0} and not any("FILTERED" in w for w in s0["warnings"])
    assert s1["filter"]["min_separation"] == 5.0 and s1["filter"]["blocked"] >= 1
    assert s1["warnings"][0].startswith("FILTERED RUN: Minimum System Point Separation = 5 index points")
    ev = [e for e in s1["event_log"] if e["type"] == "BLOCKED_SYSTEM_POINTS_TOO_CLOSE"]
    assert ev and ev[0]["text"].startswith("BLOCKED: SYSTEM_POINTS_TOO_CLOSE") and "3.25 points apart" in ev[0]["text"] and ev[0]["time"][11:16] >= "09:15"
    assert [t for t in s1["trades"] if not t["carried_in"]] == [] and len(s0["trades"]) > 12   # (the carried-in SELL's stop-out itself still happens)


@pytest.fixture
def client(tmp_path):
    replay_api.set_store(ReplayStore(tmp_path / "flt.db"))
    yield TestClient(app)
    replay_api.set_store(None)


def test_api_accepts_the_optional_threshold_and_defaults_to_off(client):
    c = client
    c.post("/api/replay/import", json={"csv_text": csv_text(daily_rows(CLOSE_POINTS + [55402]), "1D")})
    c.post("/api/replay/import", json={"csv_text": csv_text(rows(storm_minutes()), "1m")})
    off = c.post("/api/replay/sessions", json={"mode": "hybrid"}).json()
    assert off["config"]["min_separation"] is None and off["snapshot"]["filter"]["min_separation"] is None
    on = c.post("/api/replay/sessions", json={"mode": "hybrid", "min_separation": 5}).json()
    assert on["config"]["min_separation"] == 5 and on["snapshot"]["filter"]["min_separation"] == 5
    assert c.post("/api/replay/sessions", json={"mode": "hybrid", "min_separation": -1}).status_code == 422
    bt = c.post("/api/replay/backtest", json={"mode": "hybrid", "min_separation": 5}).json()
    assert [t for t in bt["trades"] if not t["carried_in"]] == [] and bt["filter"]["blocked"] >= 1
