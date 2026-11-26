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
