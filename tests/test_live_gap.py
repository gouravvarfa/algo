"""Live gap days: while today's first 15-minute candle is still forming, the gap protocol's decision WAITS (nothing fires), instead of the ordinary
stop 'reversing' the position at the open and the gap rule taking that back at 09:30 (a real account would have traded both).
Found 10 Nov 2026 by evaluating the same gap day at 09:20 and at 09:31. Candles are SYNTHETIC test fixtures."""
import datetime as dt

from app.strategy.top_bottom.gap_protocol import MCX
from app.strategy.top_bottom.intraday import Minute, run_intraday

T = lambda d, h, m: dt.datetime(2026, 3, d, h, m)
DATES = [dt.datetime(2026, 2, 20 + i) for i in range(6)] + [dt.datetime(2026, 3, 6)]
CLOSES = [100, 110, 100, 90, 100, 108, 111]          # Top 110, Bottom 90 confirmed; 6 Mar closes 111
BUY_DAY = [Minute(T(6, 9, 15), 105, 105, 105, 105), Minute(T(6, 9, 16), 109.95, 111, 109.95, 110.5), Minute(T(6, 9, 17), 111, 111, 111, 111)]   # BUY at 110, stop 90


def gap_down_day(after=86.5, low_after=85.5):
    """9 Mar opens at 85, below the BUY's stop 90; the 09:15-09:30 candle's low is 84; after 09:30 price trades at `after` (low `low_after`)."""
    first = [Minute(T(9, 9, 15), 85, 88, 84, 86)] + [Minute(T(9, 9, 15 + k), 86, 87.5, 85.5, 86.5) for k in range(1, 15)]
    rest = [Minute(T(9, 9, 30 + k), after, max(after, 87), low_after, after) for k in range(10)]
    return first + rest


def at(minutes, h, m):
    return [x for x in minutes if (x.ts.hour, x.ts.minute) < (h, m)]


def run(minutes, session=None):
    kw = {"session": session} if session else {}
    return run_intraday("BANKNIFTY", DATES, CLOSES, BUY_DAY + minutes, **kw)


def test_before_the_first_15_minute_candle_completes_a_gapped_position_is_held_and_waits():
    early = run(at(gap_down_day(), 9, 20))
    assert [(t.direction, t.exit_reason) for t in early.trades] == [("BUY", "END_OF_BACKTEST")]       # it used to be: BUY stopped out at 09:15 + a SELL
    assert early.trades[0].gap_state == "GAP_PENDING" and early.trades[0].active_stop == 90
    assert any(n["kind"] == "GAP_PENDING" for n in early.notes) and not any("NOT applied" in i for f in early.flags for i in f["issues"])


def test_the_decision_at_0920_is_the_one_still_standing_at_0931():
    days = gap_down_day()
    early, later = run(at(days, 9, 20)), run(at(days, 9, 31))
    assert [(t.direction, t.entry_date) for t in early.trades] == [(t.direction, t.entry_date) for t in later.trades]   # no signal is taken back
    t = later.trades[0]
    assert t.exit_reason == "END_OF_BACKTEST" and t.gap_state == "GAP_HELD" and t.protective_sl == 84


def test_a_gap_up_past_the_top_waits_for_the_15_minute_high_instead_of_entering_at_the_open():
    flat_closes = [100, 110, 100, 90, 100, 108, 108]                   # flat: Top 110 / Bottom 90, 6 Mar closes 108 (below the Top)
    day = [Minute(T(6, 9, 15), 105, 106, 104, 105)] + [Minute(T(6, 9, 15 + k), 105, 106, 104.5, 105) for k in range(1, 5)]
    gap = [Minute(T(9, 9, 15), 113, 114, 112.5, 113.5)] + [Minute(T(9, 9, 15 + k), 113.5, 113.9, 112.6, 113.2) for k in range(1, 15)]
    gap += [Minute(T(9, 9, 30), 113.2, 114.5, 113.0, 114.3)]          # 09:30 trades above the 15-min high 114 -> gap entry
    early = run_intraday("BANKNIFTY", DATES, flat_closes, day + at(gap, 9, 20))
    later = run_intraday("BANKNIFTY", DATES, flat_closes, day + gap)
    assert early.trades == [] and any(n["kind"] == "GAP_PENDING" for n in early.notes)               # it used to BUY at the 09:15 open
    assert [(t.direction, t.gap_state, t.entry_date) for t in later.trades] == [("BUY", "GAP_ENTRY", T(9, 9, 30))]


def test_a_past_day_with_missing_first_minutes_still_uses_the_ordinary_rules():
    days = [m for m in gap_down_day() if m.ts.minute != 20]             # 09:20 missing: that window never completes
    nxt = [Minute(T(10, 9, 15), 86, 86, 86, 86)]                         # a later day exists, so 9 Mar is not forming
    res = run_intraday("BANKNIFTY", DATES + [dt.datetime(2026, 3, 9)], CLOSES + [86.5], BUY_DAY + days + nxt)
    assert res.trades[0].exit_reason == "STOP_LOSS_REVERSAL" and res.trades[0].exit_date == T(9, 9, 15)
    assert any("NOT applied" in i for f in res.flags for i in f["issues"])


def test_mcx_waits_until_its_own_first_candle_ends_at_0915():
    mcx_buy = [Minute(T(6, 9, 0), 105, 105, 105, 105), Minute(T(6, 9, 1), 109.95, 111, 109.95, 110.5), Minute(T(6, 9, 2), 111, 111, 111, 111)]
    gap = [Minute(T(9, 9, 0), 85, 88, 84, 86)] + [Minute(T(9, 9, k), 86, 87.5, 85.5, 86.5) for k in range(1, 8)]
    res = run_intraday("GOLDM", DATES, CLOSES, mcx_buy + gap, session=MCX)
    assert [(t.direction, t.exit_reason, t.gap_state) for t in res.trades] == [("BUY", "END_OF_BACKTEST", "GAP_PENDING")]


# ---------------------------------------------------------------- rules decided with the user, 10 Nov 2026
def test_a_gap_exit_reverses_at_once_at_the_exit_price():
    days = gap_down_day(after=83.5, low_after=83.0)                     # 09:30 trades below the 15-min low 84
    res = run(at(days, 9, 32))
    t1, t2 = res.trades[0], res.trades[1]
    assert (t1.direction, t1.exit_reason, t1.exit_date) == ("BUY", "GAP_EXIT_15M", T(9, 9, 30))
    assert (t2.direction, t2.entry_date, t2.entry_price) == ("SELL", T(9, 9, 30), t1.exit_price)     # same minute, same price: exit first, then the entry


def test_after_a_one_percent_stop_the_old_points_stay_usable_and_two_trades_a_day_at_most():
    from app.strategy.top_bottom import intraday as im
    day = BUY_DAY[:2] + [Minute(T(6, 9, 17), 110.5, 110.6, 108.5, 109.0),     # BUY at 110, its 1% stop 108.9 hit
                         Minute(T(6, 9, 18), 109.0, 111.0, 109.0, 110.8),     # Top 110 crossed again: 2nd trade of the day (no waiting)
                         Minute(T(6, 9, 19), 110.8, 110.9, 108.0, 108.2),     # its 1% stop hit again
                         Minute(T(6, 9, 20), 108.2, 111.5, 108.2, 111.2)]     # crossed again: a 3rd entry is NOT taken (2 a day)
    res = run_intraday("BANKNIFTY", DATES[:6], CLOSES[:6], day, entry_day_stop_pct=0.01)
    assert [t.exit_reason for t in res.trades] == ["ENTRY_DAY_STOP_EXIT", "ENTRY_DAY_STOP_EXIT"] and im.MAX_TRADES_PER_DAY == 2
