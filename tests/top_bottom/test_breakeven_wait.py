"""Daily-close engine: 2%-profit breakeven exit -> FLAT, no reversal, wait for a NEW System Point.

Synthetic series (BUY_PRE: bottom 10 @idx1, top 40 @idx3; entry 40 on idx6).
"""
from app.strategy.top_bottom.backtest import BREAKEVEN_EXIT, run_top_bottom_backtest
from tests.top_bottom.test_breakeven import BUY_PRE, SELL_PRE, _dates, _opens, _run

BE_EXIT = "BREAKEVEN_EXIT_AFTER_2_PERCENT_PROFIT"


def test_buy_breakeven_exit_goes_flat_and_never_reverses():
    closes = BUY_PRE + [35, 40.5, 40.7, 39.9, 39.5, 41]
    _, trades = _run(closes, overrides={7: (41.0, 40.0)})
    assert BREAKEVEN_EXIT == BE_EXIT
    t = trades[0]
    assert (t.direction, t.exit_reason, t.exit_price, t.entry_price) == ("BUY", BE_EXIT, 40, 40)
    assert len(trades) == 1  # no SELL at 40, and the later close back above the consumed Top (41 > 40.7) bought nothing


def test_sell_breakeven_exit_goes_flat_and_never_reverses():
    closes = SELL_PRE + [25, 19.5, 19.3, 20.1, 20.4, 19.0, 18.5]  # SELL @20, +2% at idx7, back to 20 at idx8
    _, trades = _run(closes, overrides={7: (19.8, 19.5)})
    assert [(t.direction, t.exit_reason, t.exit_price) for t in trades] == [("SELL", BE_EXIT, 20)]  # closes below the consumed Bottom buy/sell nothing


def test_resumes_only_when_a_new_system_point_forms_and_is_crossed():
    # exit on idx8; idx10 close 41 > consumed Top 40.7 (blocked); a NEW Top 45 confirms on idx12 and idx13 crosses it
    closes = BUY_PRE + [35, 40.5, 40.7, 39.9, 39.5, 41, 45, 43, 50]
    signals, trades = _run(closes, overrides={7: (41.0, 40.0)})
    assert [t.direction for t in trades] == ["BUY", "BUY"]
    first, second = trades[0], trades[1]
    assert first.exit_reason == BE_EXIT and first.exit_date == _dates(14)[8]
    assert second.entry_price == 45 and second.system_point == 45 and second.entry_date == _dates(14)[13]
    assert len(signals) == 2  # no signal was created by the breakeven exit itself


def test_a_structural_stop_better_than_breakeven_is_a_normal_stop_exit_and_still_reverses():
    # BE activates on idx7 (stop 40), then a Bottom at 41.8 trails the stop ABOVE entry; hitting it is NOT a breakeven exit
    closes = BUY_PRE + [35, 40.5, 40.7, 42, 41.8, 42.5, 41]
    _, trades = _run(closes, overrides={7: (41.0, 40.0)})
    t = trades[0]
    assert t.breakeven_date is not None and t.exit_reason == "TRAILING_STOP_REVERSAL" and t.exit_price == 41.8
    assert trades[1].direction == "SELL" and trades[1].entry_price == 41.8


def test_other_stop_exits_keep_the_existing_always_reversing_behaviour():
    closes = BUY_PRE + [35, 40.5, 5.0, 4.0]  # structural stop (10) hit, +2% never reached
    _, trades = _run(closes)
    assert trades[0].exit_reason == "TRAILING_STOP_REVERSAL" and trades[1].direction == "SELL"


def test_without_highs_and_lows_nothing_changes():
    closes = BUY_PRE + [35, 40.5, 40.7, 39.9]
    _, trades = run_top_bottom_backtest("X", "X", "", "STOCK", 1, _dates(len(closes)), closes, _opens(closes))
    assert trades[0].breakeven_date is None and trades[0].exit_reason == "END_OF_BACKTEST"


def test_user_example_structural_trailing_keeps_improving_after_the_2_percent_rule_activated():
    """BUY at 100; +2% reached (breakeven, stop 100); price runs to 500; a NEW Bottom 400 confirms;
    next day 450: the stop is 400 (structural), not 100, and it never gets looser."""
    closes = [60, 40, 80, 100, 90, 90, 101, 110, 500, 400, 450, 450]
    _, trades = _run(closes, overrides={7: (120.0, 105.0)})
    (t,) = trades
    assert (t.direction, t.entry_price, t.initial_sl) == ("BUY", 100, 40)
    assert t.breakeven_date == _dates(12)[7] and t.exit_reason == "END_OF_BACKTEST"
    stops = [v for _, v in t.stop_history]
    assert stops == [40, 100, 400]  # initial -> breakeven -> structural Bottom
    assert t.active_stop == 400 and stops == sorted(stops)  # never less protective


def test_a_wick_through_the_stop_that_the_close_does_not_confirm_is_flagged_but_changes_no_decision():
    """The 16-Jan-2026 pattern: a SELL held with stop 50 has a bar whose HIGH is 51 but whose CLOSE is 49."""
    closes = SELL_PRE + [25, 19.9, 19.9, 49, 20, 19.9]
    sig, trades = _run(closes, highs=[c + 0.1 for c in closes], overrides={8: (51.0, 19.8)})
    t = trades[0]
    wick = [e for e in t.events if e[1].startswith("WICK THROUGH STOP 50.00")]
    assert len(wick) == 1 and wick[0][0] == _dates(len(closes))[8]  # flagged on that bar
    assert t.exit_reason != "TRAILING_STOP_REVERSAL" or t.exit_date != _dates(len(closes))[8]  # the close-based engine did NOT exit on the wick
