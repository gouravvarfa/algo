"""
Verifies the Top-Bottom System's trailing-stop-and-reverse engine against
the exact worked examples and TEST 1-8 acceptance cases from the spec:
always CLOSE-price based, no minimum swing size, no 2/2-or-wider pivot
confirmation, continuously trailing stop, automatic reversal on breach,
and no look-ahead bias.
"""
import datetime as dt

from app.strategy.top_bottom.backtest import run_top_bottom_backtest


def _dates(n, start="2026-02-01"):
    base = dt.datetime.strptime(start, "%Y-%m-%d")
    return [base + dt.timedelta(days=i) for i in range(n)]


def _run(closes):
    dates = _dates(len(closes))
    opens = closes[:1] + closes[:-1]
    return run_top_bottom_backtest("X", "XFUT", "2026-09-29", "STOCK", 500, dates, closes, opens)


def test_1_buy_triggers_above_latest_top_with_sl_at_latest_bottom():
    # 10 -> 40 -> 30 -> 40.01 : BUY, System Point=40, Initial SL=30.
    signals, trades = _run([10, 40, 30, 40.01])
    buys = [t for t in trades if t.direction == "BUY"]
    assert len(buys) == 1
    # Triggered by the 40.01 close, but filled AT the System Point 40.
    assert buys[0].entry_price == 40
    assert buys[0].system_point == 40
    assert buys[0].initial_sl == 30


def test_2_small_reversal_confirms_a_top():
    # 35 -> 40 -> 39.8 : 40 becomes latest TOP immediately (no size filter).
    from app.strategy.top_bottom.swing_detection import detect_confirmed_swings

    dates = _dates(3)
    swings = detect_confirmed_swings(dates, [35, 40, 39.8], 1, 1)
    tops = [s for s in swings if s.kind == "TOP"]
    assert len(tops) == 1
    assert tops[0].price == 40


def test_3_small_reversal_confirms_a_bottom():
    # 35 -> 30 -> 30.2 : 30 becomes latest BOTTOM immediately.
    from app.strategy.top_bottom.swing_detection import detect_confirmed_swings

    dates = _dates(3)
    swings = detect_confirmed_swings(dates, [35, 30, 30.2], 1, 1)
    bottoms = [s for s in swings if s.kind == "BOTTOM"]
    assert len(bottoms) == 1
    assert bottoms[0].price == 30


def test_4_full_worked_example_trailing_stop_and_reversal_to_sell():
    # 10 -> 40 -> 30 -> 40 -> 70 -> 100 -> 90 -> 110 -> 90 -> 80
    # Expected: BUY opens on the breakout above 40; the trailing stop
    # eventually reaches 90 (the latest confirmed Bottom); it then exits
    # at 90 and immediately reverses into a SELL at 90.
    signals, trades = _run([10, 40, 30, 40, 70, 100, 90, 110, 90, 80])

    buys = [t for t in trades if t.direction == "BUY"]
    assert len(buys) == 1
    buy = buys[0]
    assert buy.system_point == 40
    assert buy.exit_price == 90
    assert buy.exit_reason == "TRAILING_STOP_REVERSAL"
    assert buy.active_stop == 90  # trailed forward from the initial 30 to 90

    sells = [t for t in trades if t.direction == "SELL"]
    assert len(sells) == 1
    sell = sells[0]
    assert sell.entry_price == 90
    assert sell.system_point == 90
    assert sell.initial_sl == 110  # latest confirmed Top at the moment of reversal
    assert sell.reversal_event_id == buy.reversal_event_id  # same reversal event


def test_5_sell_trailing_stop_follows_latest_top_downward():
    # Continuing the full worked example: 90 -> 80 -> 60 -> 70 -> 50 ->
    # 70 -> 80. SELL's trailing stop must reach 70 (the latest confirmed
    # Top after 60->70), then exit+reverse to BUY at 70.
    signals, trades = _run([10, 40, 30, 40, 70, 100, 90, 110, 90, 80, 60, 70, 50, 70, 80])

    sells = [t for t in trades if t.direction == "SELL"]
    assert len(sells) == 1
    sell = sells[0]
    assert sell.active_stop == 70
    assert sell.exit_price == 70
    assert sell.exit_reason == "TRAILING_STOP_REVERSAL"

    buys = [t for t in trades if t.direction == "BUY"]
    assert len(buys) == 2  # the original BUY@40, and the final reversal BUY@70
    final_buy = buys[-1]
    assert final_buy.entry_price == 70
    assert final_buy.system_point == 70
    assert final_buy.initial_sl == 50  # latest confirmed Bottom at reversal
    assert final_buy.reversal_event_id == sell.reversal_event_id
    # This last trade is still open at the end of the (short) series.
    assert final_buy.exit_reason == "END_OF_BACKTEST"
    assert final_buy.exit_price == 80  # closed at the final available price


def test_6_sell_exit_and_buy_entry_share_the_same_reversal_event():
    signals, trades = _run([10, 40, 30, 40, 70, 100, 90, 110, 90, 80, 60, 70, 50, 70, 80])
    sell = next(t for t in trades if t.direction == "SELL")
    final_buy = trades[trades.index(sell) + 1]
    assert final_buy.direction == "BUY"
    assert final_buy.reversal_event_id == sell.reversal_event_id


def test_7_never_two_consecutive_trades_in_the_same_direction():
    signals, trades = _run([10, 40, 30, 40, 70, 100, 90, 110, 90, 80, 60, 70, 50, 70, 80, 90, 60, 100, 40])
    for a, b in zip(trades, trades[1:]):
        assert a.direction != b.direction


def test_8_no_look_ahead_a_pivot_is_never_visible_before_its_confirmation_bar():
    from app.strategy.top_bottom.swing_detection import swings_confirmed_by
    from app.strategy.top_bottom.backtest import SWING_LEFT_BARS, SWING_RIGHT_BARS
    from app.strategy.top_bottom.swing_detection import detect_confirmed_swings

    closes = [10, 40, 30, 40, 70, 100, 90, 110, 90, 80]
    dates = _dates(len(closes))
    swings = detect_confirmed_swings(dates, closes, SWING_LEFT_BARS, SWING_RIGHT_BARS)

    for s in swings:
        # The pivot must not be visible on any bar strictly before its own
        # confirmation index.
        assert swings_confirmed_by(swings, s.confirmed_index - 1).count(s) == 0
        assert s in swings_confirmed_by(swings, s.confirmed_index)


def test_close_price_only_no_high_low_parameter_exists():
    import inspect

    sig = inspect.signature(run_top_bottom_backtest)
    assert "high" not in sig.parameters and "low" not in sig.parameters


def test_never_wider_than_1_1_confirmation_no_hidden_larger_window():
    # A tiny series that would fire immediately under 1/1 but could never
    # confirm anything under any 2-or-wider window — proves 1/1 really is
    # what's running, not a silently wider default.
    signals, trades = _run([35, 40, 39.8, 40.1])
    assert len(trades) == 1
    assert trades[0].entry_price == 40  # filled at the System Point


def test_flat_start_requires_both_a_top_and_a_bottom_before_any_entry():
    # A pure uptrend with no confirmed Bottom yet must never enter a BUY
    # with a fabricated stop.
    signals, trades = _run([10, 20, 30, 40, 50, 60])
    assert trades == []


def test_strategy_run_backtest_uses_exactly_the_series_it_is_given():
    # run_backtest no longer clips internally — per explicit user
    # direction, the CALLER (top_bottom_backtest_service.py) is
    # responsible for clipping to [from_date, to_date] before this runs,
    # so the backtest starts FLAT at from_date with no warm-up bleed-in
    # from earlier history. This just confirms the pure function honors
    # whatever series it's handed.
    from app.strategy.top_bottom.strategy import run_backtest

    closes = [20, 40, 30, 40.5]
    dates = _dates(len(closes))
    opens = closes[:1] + closes[:-1]

    result = run_backtest(
        "X", "XFUT", "2026-09-29", "STOCK", 500, "NFO", "1D",
        dates, closes, opens, dates[0], dates[-1],
        data_available_from=dates[0], data_available_to=dates[-1],
    )
    buys = [t for t in result.trades if t.direction == "BUY"]
    assert len(buys) == 1
    assert buys[0].entry_price == 40  # filled at the System Point
    assert len(result.price_series) == len(closes)  # every bar handed in is reported
