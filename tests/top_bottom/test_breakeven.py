"""2% profit -> breakeven stop: intrabar threshold on the replay bar's own
timeframe, disabled when a new system point appears first, never worsens the
stop, conservative against a same-bar stop breach, gap protocol still wins
when the market opens through the breakeven stop."""
from app.strategy.top_bottom.backtest import BREAKEVEN_EVENT, NEW_SP_EVENT, run_top_bottom_backtest
from tests.top_bottom.test_gap_protocol import BASE, BUY_PRE, SELL_PRE, _bars, _dates, _intra, _opens


def _run(closes, highs=None, lows=None, opens=None, intraday=None, overrides=None):
    highs = highs or [c + 0.1 for c in closes]
    lows = lows or [c - 0.1 for c in closes]
    for idx, (h, l) in (overrides or {}).items():
        highs[idx], lows[idx] = h, l
    opens = opens or _opens(closes)
    return run_top_bottom_backtest(
        "X", "XFUT", "2026-09-29", "STOCK", 500, _dates(len(closes)), closes, opens, intraday, highs, lows
    )


BUY = BUY_PRE + [35, 40.5, 40.7, 39.9]  # BUY filled at 40 on idx6; threshold 40.8


def test_buy_two_percent_moves_stop_to_entry_then_exits_there():
    _, trades = _run(BUY, overrides={7: (41.0, 40.0)})
    t = trades[0]
    assert t.breakeven_date == _dates(9)[7] and t.events == [(_dates(9)[7], BREAKEVEN_EVENT)]
    assert t.initial_sl == 10 and t.stop_history[-1][1] == 40
    assert (t.exit_price, t.exit_date) == (40, _dates(9)[8])


def test_without_highs_and_lows_rule_is_inactive():
    _, trades = run_top_bottom_backtest("X", "XFUT", "e", "STOCK", 500, _dates(len(BUY)), BUY, _opens(BUY))
    assert trades[0].breakeven_date is None and trades[0].exit_reason == "END_OF_BACKTEST"


def test_buy_threshold_not_reached_keeps_structural_stop():
    _, trades = _run(BUY, overrides={7: (40.7, 40.0)})
    t = trades[0]
    assert t.breakeven_date is None and t.active_stop == 10 and t.exit_reason == "END_OF_BACKTEST"


def test_new_system_point_before_threshold_disables_rule():
    closes = BUY_PRE + [35, 40.5, 40.2, 40.3, 39.9]  # top 40.5 > 40 confirms on idx7
    _, trades = _run(closes, overrides={8: (41.0, 40.0)})
    t = trades[0]
    # Existing trailing rule governs: bottom 40.2 confirmed -> stop 40.2 (not breakeven 40).
    assert t.breakeven_date is None and t.stop_history[-1][1] == 40.2
    assert [e for e in t.events if e[1] == NEW_SP_EVENT] == [(t.new_sp_date, NEW_SP_EVENT)] and t.new_sp_price == 40.5  # the new System Point is timestamped


def test_sell_two_percent_mirror():
    closes = SELL_PRE + [25, 19.5, 19.3, 20.1]  # SELL filled at 20 on idx6; threshold 19.6
    _, trades = _run(closes, overrides={7: (19.8, 19.5)})
    t = trades[0]
    assert t.direction == "SELL" and t.breakeven_date == _dates(9)[7]
    assert t.initial_sl == 50 and t.stop_history[-1][1] == 20
    assert t.exit_price == 20


def test_same_bar_stop_breach_wins_over_breakeven():
    # idx7 reaches +2% but its close breaches the structural stop 10: no breakeven.
    closes = BUY_PRE + [35, 40.5, 5.0]
    _, trades = _run(closes, overrides={7: (41.0, 4.0)})
    assert trades[0].breakeven_date is None and trades[0].exit_price == 10


def test_gap_through_breakeven_stop_uses_gap_protocol_not_stop_price():
    intraday = _intra({8: _bars(8, (38, 39, 37, 38.5), [(38.5, 38.6, 36, 36.5)])})
    closes = BUY[:-1] + [36.5]
    opens = _opens(closes, d8=38)
    _, trades = _run(closes, opens=opens, intraday=intraday, overrides={7: (41.0, 40.0)})
    t = trades[0]
    assert t.breakeven_date is not None
    assert (t.exit_reason, t.exit_price) == ("GAP_EXIT_15M", 37)
