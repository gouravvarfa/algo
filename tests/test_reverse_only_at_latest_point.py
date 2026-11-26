"""Owner, 26 Nov 2026 (CANBK): a stop-out reverses only when the stop IS the latest opposite System Point; a stop at an older level, or a
breakeven stop, exits FLAT and the next entry needs the latest Top / Bottom crossed. The 15-minute gap rule only applies to a stop that is
the latest System Point; a breakeven (or older-level) stop gapped through exits at the open. Candles are SYNTHETIC test fixtures."""
import datetime as dt

from app.strategy.top_bottom.intraday import BREAKEVEN_EXIT, STOP_EXIT_NO_REVERSAL, IntradayInitial, Minute, run_intraday
from app.strategy.top_bottom.models import Trade

T = lambda d, h, m: dt.datetime(2026, 3, d, h, m)


def daily():
    """Closes 100,110,100,90,100,108: Bottom 90 and Top 110 confirmed; the next daily candle is 6 Mar."""
    closes = [100, 110, 100, 90, 100, 108]
    return [dt.datetime(2026, 2, 20 + i) for i in range(len(closes))], closes


def mins(day, path):
    out = []
    for h, m, *v in path:
        o, hi, lo, c = (v[0],) * 4 if len(v) == 1 else v
        out.append(Minute(T(day, h, m), o, hi, lo, c))
    return out


def sell(stop, entry=100.0, breakeven=False):
    t = Trade(direction="SELL", symbol="X", trading_symbol="X", expiry="", instrument_class="FUTURE", lot_size=1, signal_date=dt.datetime(2026, 2, 24),
              entry_date=dt.datetime(2026, 2, 24), entry_price=entry, system_point=entry, initial_sl=stop, active_stop=stop, reversal_event_id="r")
    t.entry_index = 3
    if breakeven:
        t.breakeven_date = dt.datetime(2026, 2, 25)
    return IntradayInitial(trade=t)


def run(path, init):
    d, c = daily()
    return run_intraday("X", d, c, mins(6, path), initial=init)


def test_a_stop_at_an_older_level_exits_flat_and_does_not_reverse():
    # SELL with its stop at 104 (an older Top); the latest Top is 110. 09:20 trades 104.5 -> exit at 104, no BUY.
    res = run([(9, 15, 101.0), (9, 20, 103.0, 104.5, 103.0, 104.2), (9, 25, 104.0)], sell(104.0))
    assert res.trades[0].exit_reason == STOP_EXIT_NO_REVERSAL and res.trades[0].exit_price == 104.0
    assert len(res.trades) == 1                                                       # flat: no BUY at 104


def test_after_such_an_exit_the_buy_comes_only_above_the_latest_top():
    res = run([(9, 15, 101.0), (9, 20, 103.0, 104.5, 103.0, 104.2), (9, 25, 108.0), (9, 30, 109.0, 110.5, 109.0, 110.2)], sell(104.0))
    buy = res.trades[-1]
    assert buy.direction == "BUY" and buy.entry_price == 110.0 and buy.entry_date == T(6, 9, 30)


def test_a_stop_at_the_latest_top_still_reverses():
    res = run([(9, 15, 101.0), (9, 20, 109.0, 110.5, 109.0, 110.2), (9, 25, 110.3)], sell(110.0))
    assert res.trades[0].exit_reason == "STOP_LOSS_REVERSAL" and res.trades[1].direction == "BUY" and res.trades[1].entry_price == 110.0


def test_a_breakeven_stop_gapped_through_exits_at_the_open_without_the_15_minute_rule():
    # SELL entered at 100, breakeven stop 100; market opens 103 (above the stop): exit at the open 103, flat, no waiting for 09:30
    res = run([(9, 15, 103.0), (9, 16, 102.0), (9, 31, 101.0)], sell(100.0, entry=100.0, breakeven=True))
    t = res.trades[0]
    assert t.exit_reason == BREAKEVEN_EXIT and t.exit_price == 103.0 and t.exit_date == T(6, 9, 15)
    assert len(res.trades) == 1


def test_a_gap_through_a_stop_at_the_latest_top_waits_for_the_15_minute_candle():
    # stop = latest Top 110; open 112: no exit at the open, the 15-minute rule decides after 09:30
    path = [(9, 15, 112.0, 112.5, 111.0, 111.5)] + [(9, m, 111.5) for m in range(16, 30)] + [(9, 30, 111.4), (9, 35, 111.2)]   # a complete first 15 minutes
    res = run(path, sell(110.0))
    t = res.trades[0]
    assert t.exit_date is None or t.exit_date >= T(6, 9, 30)
    assert t.exit_reason != BREAKEVEN_EXIT


def first15(open_, high, low, close):
    """A complete first 15 minutes (09:15-09:29): the 09:15 candle carries the high / low, the rest sit at `close`."""
    return [(9, 15, open_, high, low, close)] + [(9, m, close) for m in range(16, 30)]


def test_a_gap_through_an_older_system_point_stop_uses_the_15_minute_rule():
    # stop 104 is an older Top (latest Top 110): it IS a System Point, so the gap open 106 does not exit at the open; the first 15 minutes
    # (high 106.5) hold, so the SELL is still open after 09:30 (CANBK 25 Nov 2026: opened 135.71 above its stop 134.41, gap held at 136.5)
    res = run(first15(106.0, 106.5, 105.0, 105.5) + [(9, 30, 105.4), (9, 40, 105.2)], sell(104.0))
    t = res.trades[0]
    assert t.exit_reason == "END_OF_BACKTEST" and len(res.trades) == 1


def test_the_15_minute_exit_of_an_older_system_point_stop_does_not_reverse():
    # same gap; at 09:40 price breaks the 15-minute high 106.5: exit at 106.5, FLAT (the stop 104 was not the latest Top 110), no BUY
    res = run(first15(106.0, 106.5, 105.0, 105.5) + [(9, 30, 105.4), (9, 40, 106.0, 107.0, 106.0, 106.8), (9, 45, 107.0)], sell(104.0))
    t = res.trades[0]
    assert t.exit_reason == "GAP_EXIT_15M_NO_REVERSAL" and t.exit_price == 106.5 and len(res.trades) == 1


def test_the_15_minute_exit_of_a_latest_system_point_stop_still_reverses():
    res = run(first15(112.0, 112.5, 111.0, 111.5) + [(9, 30, 111.4), (9, 40, 112.0, 113.0, 112.0, 112.8)], sell(110.0))
    assert res.trades[0].exit_reason == "GAP_EXIT_15M" and res.trades[1].direction == "BUY"


def test_a_capped_one_percent_stop_gapped_through_exits_at_the_open():
    # SELL at 100 whose structural stop 110 is 10% away: capped at 101 (entry + 1%). Opens 103: exit at the open, no 15-minute wait
    init = sell(110.0)
    init.trade.entry_day_sl = 101.0
    d, c = daily()
    res = run_intraday("X", d, c, mins(6, first15(103.0, 103.5, 102.0, 102.5) + [(9, 30, 102.4)]), initial=init,
                       entry_day_stop_pct=0.01, struct_cap_distance=0.03)
    t = res.trades[0]
    assert t.exit_reason == "CAPPED_1_PERCENT_STOP_EXIT" and t.exit_price == 103.0 and t.exit_date == T(6, 9, 15)


# ---- owner, 26 Nov 2026 (SILVERMIC): after an exit to FLAT a new entry needs a real crossing of the Top / Bottom --------------------------------
def sell_rolled(entry):
    """A SELL taken over at `entry` below the latest Bottom 90 (a roll), with its 2% breakeven reached: stop = entry."""
    init = sell(entry, entry=entry, breakeven=True)
    return init


def test_after_a_breakeven_exit_below_the_bottom_no_sell_is_taken_without_a_fresh_crossing():
    # breakeven stop 85 hit at 09:20 (price 85.5); the latest Bottom 90 is ABOVE the price: price staying below it is no crossing
    res = run([(9, 15, 84.0), (9, 20, 84.5, 85.5, 84.5, 85.2), (9, 21, 86.0), (9, 22, 86.0, 86.5, 85.0, 85.5), (9, 23, 87.0)], sell_rolled(85.0))
    assert res.trades[0].exit_reason == BREAKEVEN_EXIT and res.trades[0].exit_price == 85.0
    assert len(res.trades) == 1                                                                   # before: SELL again at 09:21


def test_the_bottom_counts_again_after_price_closes_back_above_it_then_crosses_down():
    res = run([(9, 15, 84.0), (9, 20, 84.5, 85.5, 84.5, 85.2), (9, 21, 88.0), (9, 22, 89.0, 91.0, 89.0, 90.6),     # 09:22 closes above 90: armed
               (9, 23, 90.5, 90.6, 89.5, 89.8)], sell_rolled(85.0))                                            # 09:23 trades below 90: SELL
    s = res.trades[-1]
    assert s.direction == "SELL" and s.entry_date == T(6, 9, 23) and s.entry_price == 90.0


def test_after_a_breakeven_exit_a_buy_comes_when_the_top_above_is_crossed():
    res = run([(9, 15, 84.0), (9, 20, 84.5, 85.5, 84.5, 85.2), (9, 21, 100.0), (9, 22, 109.0, 110.5, 109.0, 110.2)], sell_rolled(85.0))
    b = res.trades[-1]
    assert b.direction == "BUY" and b.entry_price == 110.0 and b.entry_date == T(6, 9, 22)
