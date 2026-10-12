"""Intraday engine: 2%-profit breakeven (Case A / Case B) and the no-re-entry waiting state.
SYNTHETIC fixtures; structure and helpers come from test_intraday.py (prices scaled x1000)."""
from app.replay.engine import backtest_report, run_intraday_strategy, snapshot
from app.strategy.top_bottom.intraday import BREAKEVEN_EVENT, NEW_SP_EVENT
from tests.replay.helpers import day_ts, daily_rows
from tests.replay.test_intraday import BUY_D, SELL_D, minute_day, ramp, run

BE_EXIT = "BREAKEVEN_EXIT_AFTER_2_PERCENT_PROFIT"


def buy_breakeven_path(extra=None):
    mv = {}
    ramp(mv, 0, 20, 35000, 40500)    # crosses the 40000 Top: BUY at 40000 (stop 10000)
    ramp(mv, 21, 40, 40500, 41200)   # +2% (40800) reached with no new System Point: stop -> 40000
    ramp(mv, 41, 80, 41200, 39500)   # falls back through the entry price
    for a, b, p0, p1 in (extra or []):
        ramp(mv, a, b, p0, p1)
    return mv


def test_1_buy_reaches_2_percent_then_reverses_to_entry_and_exits_at_breakeven():
    res = run(BUY_D, minute_day(6, 35000, buy_breakeven_path()))
    (t,) = res.trades
    assert (t.direction, t.entry_price, t.exit_price, t.exit_reason) == ("BUY", 40000, 40000, BE_EXIT)
    assert t.breakeven_date is not None and t.breakeven_date < t.exit_date  # threshold timestamp recorded, before the exit
    assert [e[1] for e in t.events if e[1] == BREAKEVEN_EVENT] == [BREAKEVEN_EVENT]
    assert t.stop_history[-1][1] == 40000 and t.new_sp_date is None


def test_2_sell_reaches_minus_2_percent_then_reverses_to_entry_and_exits_at_breakeven():
    mv = {}
    ramp(mv, 0, 20, 25000, 19800)    # SELL at 20000 (stop 50000)
    ramp(mv, 21, 40, 19800, 19500)   # -2% (19600) reached
    ramp(mv, 41, 80, 19500, 20500)   # back up through the entry price
    (t,) = run(SELL_D, minute_day(6, 25000, mv)).trades
    assert (t.direction, t.entry_price, t.exit_price, t.exit_reason) == ("SELL", 20000, 20000, BE_EXIT)
    assert t.stop_history[-1][1] == 20000 and t.breakeven_date is not None


def test_3_new_system_point_before_the_threshold_keeps_structural_trailing_only():
    d6 = {}
    ramp(d6, 0, 20, 35000, 40500)    # BUY at 40000; high stays below +2%
    d9 = {}
    ramp(d9, 0, 60, 43000, 46000)    # +2% reached on day 9 ...
    ramp(d9, 61, 120, 46000, 41000)  # ... and price falls back below 46000, still above the structural stop
    # daily: day 7 closes 44000 = a NEW Top beyond 40000, confirmed by day 8's close -> known on day 9
    res = run(BUY_D + [40500, 44000, 43000], minute_day(6, 35000, d6) + minute_day(9, 43000, d9))
    (t,) = res.trades
    assert t.breakeven_date is None and t.exit_reason == "END_OF_BACKTEST"
    assert t.new_sp_price == 44000 and t.new_sp_date == day_ts(8)  # confirmation timestamp recorded
    assert (t.new_sp_date, NEW_SP_EVENT) in t.events and t.stop_history[-1][1] == 10000  # structural stop untouched


def test_4_threshold_never_reached_means_no_breakeven_activation():
    mv = {}
    ramp(mv, 0, 20, 35000, 40700)    # peak 40700 < 40800
    ramp(mv, 21, 60, 40700, 39500)
    (t,) = run(BUY_D, minute_day(6, 35000, mv)).trades
    assert t.breakeven_date is None and t.active_stop == 10000 and t.exit_reason == "END_OF_BACKTEST"


def test_5_a_structural_stop_better_than_breakeven_is_never_loosened_and_exits_as_a_normal_stop():
    d6 = {}
    ramp(d6, 0, 20, 35000, 40500)
    ramp(d6, 21, 40, 40500, 41000)   # +2% on day 6: breakeven
    d10 = {}
    ramp(d10, 0, 60, 46000, 43000)   # day 10: through the trailed stop
    # daily: day 8 = 44000 is a Bottom (45000 > 44000 < 46000) confirmed on day 9 -> stop trails to 44000 (> entry)
    res = run(BUY_D + [41000, 45000, 44000, 46000], minute_day(6, 35000, d6) + minute_day(10, 46000, d10))
    t, rev = res.trades[0], res.trades[1]
    assert t.breakeven_date is not None
    assert t.stop_history[-1][1] == 44000 and all(b[1] >= a[1] for a, b in zip(t.stop_history, t.stop_history[1:]))  # never loosened
    assert (t.exit_reason, t.exit_price) == ("STOP_LOSS_REVERSAL", 44000)  # not a breakeven exit: the existing reversal applies
    assert rev.direction == "SELL" and rev.entry_price == 44000


def test_6_same_candle_threshold_and_stop_assumes_the_stop_first():
    mv = {}
    ramp(mv, 0, 20, 35000, 40500)
    # one candle reaches +2% (41000) AND the structural stop (9500): the adverse move is assumed to come first
    res = run(BUY_D, minute_day(6, 35000, {**mv, 30: 20000}, wicks={30: (41000, 9500)}))
    t = res.trades[0]
    assert t.exit_reason == "STOP_LOSS_REVERSAL" and t.exit_price == 10000 and t.breakeven_date is None


def test_7_gap_through_the_breakeven_stop_uses_the_unchanged_15_minute_protocol():
    d6 = {}
    ramp(d6, 0, 20, 35000, 41000)    # +2%: stop at 40000
    d7 = {}
    ramp(d7, 15, 30, 30000, 28000)
    res = run(BUY_D + [41000, 41000], minute_day(6, 35000, d6) + minute_day(7, 30000, d7, wicks={3: (30500, 29500)}))
    t = res.trades[0]
    assert t.breakeven_date is not None and t.exit_reason == "GAP_EXIT_15M" and t.exit_price == 29500  # not a breakeven exit
    assert res.notes[0]["kind"] == "GAP_EXITED" and len(res.trades) == 1


# ---------------------------------------------------------------- section 6: no immediate re-entry
def test_breakeven_exit_leaves_the_strategy_flat_and_old_system_points_stay_consumed():
    path = buy_breakeven_path([(81, 140, 39500, 41500), (141, 200, 41500, 9000)])
    res = run(BUY_D, minute_day(6, 35000, path))
    # price re-crossed the consumed Top (41500 > 40000) and the consumed Bottom (9000 < 10000): nothing re-entered
    assert len(res.trades) == 1 and res.trades[0].exit_reason == BE_EXIT


def test_a_new_system_point_and_its_trigger_resume_trading():
    d6 = buy_breakeven_path([(81, 140, 39500, 41500)])
    d9 = {}
    ramp(d9, 0, 60, 42000, 43500)    # crosses the NEW Top 43000 (day 7 close 43000, confirmed by day 8's close)
    res = run(BUY_D + [41500, 43000, 42000], minute_day(6, 35000, d6) + minute_day(9, 42000, d9))
    first, second = res.trades
    assert first.exit_reason == BE_EXIT
    assert (second.direction, second.signal_price, second.system_point) == ("BUY", 43000, 43000)
    assert second.entry_date.date() == day_ts(9).date() and second.initial_sl == 10000


def test_replay_and_backtest_agree_and_the_waiting_state_is_visible_then_cleared():
    d6 = buy_breakeven_path([(81, 140, 39500, 41500)])
    d9 = {}
    ramp(d9, 0, 60, 42000, 43500)
    mins = minute_day(6, 35000, d6) + minute_day(9, 42000, d9)
    rows = [(m.ts, m.open, m.high, m.low, m.close, 0.0) for m in mins]
    r = run_intraday_strategy(daily_rows(BUY_D + [41500, 43000, 42000]), rows)
    strip = lambda s: {k: v for k, v in s.items() if k not in ("total", "at_end", "window_start")}
    assert strip(snapshot(r, len(r.candles) - 1, window=len(r.candles))) == strip(backtest_report(r))
    exit_idx = next(i for i, c in enumerate(r.candles) if c.ts == r.trades[0].exit_date)
    waiting = snapshot(r, exit_idx + 5)
    assert waiting["position"] == "FLAT" and waiting["waiting"] and "NEW System Point" in waiting["waiting"]["text"]
    assert snapshot(r, exit_idx - 1)["waiting"] is None                      # not yet known before the exit
    assert snapshot(r, len(r.candles) - 1)["waiting"] is None and snapshot(r, len(r.candles) - 1)["position"] == "BUY"


def test_user_example_intraday_stop_trails_to_the_new_bottom_after_breakeven_and_exits_there():
    """BUY at 100; +2% (stop 100); runs far higher; a NEW Bottom 400 confirms; next session the stop is 400, not 100."""
    d6 = {}
    ramp(d6, 0, 20, 90, 101)       # crosses the Top 100: BUY at 100 (stop 40)
    ramp(d6, 21, 60, 101, 120)     # +2% (102) reached: breakeven, stop 100
    ramp(d6, 61, 100, 120, 110)
    d10 = {}
    ramp(d10, 0, 60, 450, 420)     # still above 400: held
    ramp(d10, 61, 120, 420, 390)   # falls through 400
    daily = [60, 40, 80, 100, 90, 90, 110, 500, 400, 450]   # day 8 = 400 is a Bottom, confirmed by day 9's close
    res = run(daily, minute_day(6, 90, d6) + minute_day(10, 450, d10))
    t = res.trades[0]
    assert t.breakeven_date is not None
    assert [v for _, v in t.stop_history] == [40, 100, 400]       # initial -> breakeven -> structural
    assert (t.exit_reason, t.exit_price) == ("STOP_LOSS_REVERSAL", 400)  # exits at 400, never at 100
