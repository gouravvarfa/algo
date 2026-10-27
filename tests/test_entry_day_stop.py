"""Entry-day 1% stop (optional rule of the live Top-Bottom engine). Candles are SYNTHETIC test fixtures."""
import datetime as dt

from app.strategy.top_bottom.intraday import ENTRY_DAY_STOP_EXIT, Minute, run_intraday

D = lambda d: dt.datetime(2026, 3, d, 0, 0)
T = lambda d, h, m: dt.datetime(2026, 3, d, h, m)


def daily_history():
    """Closes 100,110,100,90,100,108: Bottom 90 and Top 110 are confirmed; the next daily candle is 6 Mar."""
    closes = [100, 110, 100, 90, 100, 108]
    return [D(2 + i) for i in range(len(closes))] if False else [dt.datetime(2026, 2, 20 + i) for i in range(len(closes))], closes


def mins(day, path):
    """path items: (hour, minute, price) = a flat candle, or (hour, minute, open, high, low, close)."""
    out = []
    for it in path:
        h, m, *v = it
        o, hi, lo, c = (v[0],) * 4 if len(v) == 1 else v
        out.append(Minute(T(day, h, m), o, hi, lo, c))
    return out


def run(minute_list, pct=0.01):
    dates, closes = daily_history()
    return run_intraday("BANKNIFTY", dates, closes, minute_list, entry_day_stop_pct=pct)


def breakout_day(extra):
    """09:15 flat at 105, 09:16 trades up through the Top (110) -> BUY filled at 110 (structural stop = Bottom 90), then `extra`."""
    return mins(6, [(9, 15, 105.0), (9, 16, 109.95, 111.0, 109.95, 110.5)] + extra)


def test_buy_entry_gets_a_one_percent_stop_not_the_structural_one():
    res = run(breakout_day([(9, 17, 111.0)]))
    t = res.trades[-1]
    assert t.direction == "BUY" and t.entry_price == 110.0
    assert t.entry_day_sl == 108.9 and t.active_stop == 108.9 and t.initial_sl == 90.0          # 1% below entry, structural kept for tomorrow
    assert any("ENTRY-DAY STOP 1%" in e[1] for e in t.events)


def test_hit_on_the_entry_day_exits_only_and_never_reverses():
    res = run(breakout_day([(9, 17, 109.5), (9, 18, 109.0, 109.0, 108.0, 108.5), (9, 19, 90.0), (9, 20, 85.0)]))
    assert [t.exit_reason for t in res.trades] == [ENTRY_DAY_STOP_EXIT]
    t = res.trades[0]
    assert t.exit_price == 108.9 and t.exit_date == T(6, 9, 18)                                  # filled at the 1% level, not at the structural stop
    assert len(res.trades) == 1                                                                  # no SELL: even though price fell through the Bottom later


def test_gap_through_the_entry_day_stop_fills_at_the_open():
    res = run(breakout_day([(9, 17, 109.5), (9, 18, 108.0)]))                                    # 09:18 opens below the 108.9 stop
    assert res.trades[0].exit_reason == ENTRY_DAY_STOP_EXIT and res.trades[0].exit_price == 108.0


def test_without_the_rule_the_same_candles_reverse_at_the_structural_stop():
    res = run(breakout_day([(9, 17, 109.5), (9, 18, 108.0), (9, 19, 90.0), (9, 20, 85.0)]), pct=None)
    assert [t.exit_reason for t in res.trades[:1]] == ["STOP_LOSS_REVERSAL"] and res.trades[1].direction == "SELL"


def test_stop_between_one_percent_and_structural_is_not_triggered_by_the_rule():
    res = run(breakout_day([(9, 17, 109.0), (9, 18, 109.0)]))                                    # 0.9% against the entry: inside the 1% stop
    assert res.trades[-1].exit_reason == "END_OF_BACKTEST" and res.trades[-1].direction == "BUY"


def test_next_day_returns_to_the_structural_stop():
    d1 = breakout_day([(9, 17, 111.0)])
    d2 = mins(7, [(9, 15, 109.5), (9, 16, 100.0), (9, 17, 95.0)])                                # far below the old 1% stop, above the Bottom 90
    t = run(d1 + d2).trades[-1]
    assert t.exit_reason == "END_OF_BACKTEST" and t.direction == "BUY"                           # not stopped on day 2: stop is back to 90
    assert t.active_stop <= 108.9 and t.active_stop != t.entry_day_sl


def test_a_stop_and_reverse_entry_also_gets_the_one_percent_stop():
    # BUY open from the history at 100 with structural stop 90 is simulated by an entry at the Top, then a structural-stop day
    d1 = breakout_day([(9, 17, 111.0)])
    d2 = mins(7, [(9, 15, 100.0), (9, 16, 89.0), (9, 17, 89.5)])                                 # breaks the Bottom 90 -> BUY stopped, SELL entered at 90
    res = run(d1 + d2)
    sell = res.trades[-1]
    assert [t.exit_reason for t in res.trades[:1]] == ["STOP_LOSS_REVERSAL"] and sell.direction == "SELL"
    assert sell.entry_day_sl == round(sell.entry_price * 1.01, 2) and sell.active_stop == sell.entry_day_sl


def test_waiting_state_needs_a_newly_confirmed_system_point():
    flat = breakout_day([(9, 17, 108.0), (9, 18, 111.0), (9, 19, 112.0)])                        # re-crossing the SAME Top the next minute: still consumed
    res = run(flat)
    assert [t.exit_reason for t in res.trades] == [ENTRY_DAY_STOP_EXIT, "END_OF_BACKTEST"] or [t.exit_reason for t in res.trades] == [ENTRY_DAY_STOP_EXIT]
    assert len(res.trades) == 1                                                                  # the consumed Top does not re-enter


REENTRY = dt.date(2026, 3, 1)       # the next-day re-entry rule switched on for these (synthetic March) days


def run_re(minute_list, start=REENTRY):
    return run_intraday("BANKNIFTY", *daily_history(), minute_list, entry_day_stop_pct=0.01, next_day_reentry_from=start)


def test_the_stop_out_day_itself_stays_flat_even_if_the_same_point_is_crossed_again():
    res = run_re(breakout_day([(9, 17, 108.0), (9, 18, 111.0), (9, 19, 112.0)]))                # stopped at 09:17, Top 110 crossed again at 09:18: nothing today
    assert [t.exit_reason for t in res.trades] == [ENTRY_DAY_STOP_EXIT]


def test_next_day_the_same_point_crossed_again_gives_a_new_entry():
    d1 = breakout_day([(9, 17, 108.0)])
    d2 = mins(7, [(9, 15, 109.0), (9, 16, 111.0), (9, 17, 112.0)])                               # the already-used Top 110 crossed on the NEXT day
    res = run_re(d1 + d2)
    assert [t.exit_reason for t in res.trades] == [ENTRY_DAY_STOP_EXIT, "END_OF_BACKTEST"]
    again = res.trades[1]
    assert again.direction == "BUY" and again.entry_date == T(7, 9, 16) and again.entry_price == 111.0 and again.entry_day_sl == round(111.0 * 0.99, 2)


def test_only_the_next_day_gets_it_and_it_never_reverses():
    d1 = breakout_day([(9, 17, 108.0)])
    quiet = mins(7, [(9, 15, 105.0), (9, 16, 106.0)])
    later = mins(8, [(9, 15, 109.0), (9, 16, 111.0)])                                            # two days after: the old rule, the used Top stays consumed
    assert [t.exit_reason for t in run_re(d1 + quiet + later).trades] == [ENTRY_DAY_STOP_EXIT]
    down = mins(7, [(9, 15, 100.0), (9, 16, 85.0)])                                              # through the Bottom 90 next day: no SELL
    assert [t.exit_reason for t in run_re(d1 + down).trades] == [ENTRY_DAY_STOP_EXIT]


def test_reentry_is_off_before_its_start_day():
    d1 = breakout_day([(9, 17, 108.0)])
    d2 = mins(7, [(9, 15, 109.0), (9, 16, 111.0), (9, 17, 112.0)])
    assert [t.exit_reason for t in run_re(d1 + d2, start=dt.date(2026, 10, 27)).trades] == [ENTRY_DAY_STOP_EXIT]     # earlier history is unchanged

def test_rule_is_off_by_default():
    res = run_intraday("X", *daily_history(), breakout_day([(9, 17, 111.0)]))
    assert res.trades[-1].entry_day_sl is None and res.trades[-1].active_stop == 90.0


# ================================================================ capped-stop rule: structural stop >= 2% from entry -> keep 1%
from app.strategy.top_bottom.intraday import CAPPED_STOP_EXIT

DAYS = [dt.datetime(2026, 3, 1) + dt.timedelta(days=k) for k in range(12)]


def run_cap(closes, minute_list, cap=0.02):
    return run_intraday("BANKNIFTY", DAYS[:len(closes)], closes, minute_list, entry_day_stop_pct=0.01, struct_cap_distance=cap)


FAR = [100, 110, 100, 90, 100, 108]          # Top 110, Bottom 90 confirmed: structural stop for a BUY at 110 is 90 (18% away)


def day(d, *prices):
    return mins(d, [(9, 15 + i, p) for i, p in enumerate(prices)] if prices and not isinstance(prices[0], tuple) else list(prices))


def test_far_structural_stop_keeps_the_one_percent_stop_on_day_two():
    d1 = mins(7, [(9, 15, 105.0), (9, 16, 109.95, 111.0, 109.95, 110.5), (9, 17, 111.0)])        # 7 Mar: BUY at 110
    d2 = mins(8, [(9, 15, 110.5), (9, 16, 110.0), (9, 17, 109.0)])                                # 8 Mar: ordinary 109.0, stop is 108.9
    t = run_cap(FAR + [111.0], d1 + d2).trades[-1]
    assert t.exit_reason == "END_OF_BACKTEST" and t.active_stop == 108.9 and t.capped and t.struct_stop == 90.0
    assert any("STOP CAPPED AT 1%" in e[1] for e in t.events)


def test_capped_stop_hit_on_a_later_day_exits_only_and_waits_for_a_fresh_system_point():
    d1 = mins(7, [(9, 15, 105.0), (9, 16, 109.95, 111.0, 109.95, 110.5), (9, 17, 111.0)])
    d2 = mins(8, [(9, 15, 110.5), (9, 16, 109.0), (9, 17, 108.0), (9, 18, 85.0)])                 # falls through 108.9 AND through the old Bottom 90
    res = run_cap(FAR + [111.0], d1 + d2)
    assert [t.exit_reason for t in res.trades] == [CAPPED_STOP_EXIT] and res.trades[0].exit_price == 108.0       # gap-style fill at the open below the stop
    assert len(res.trades) == 1                                                                   # no SELL even though the Bottom (90) broke later


def test_close_structural_stop_takes_over_and_the_one_percent_rule_is_not_used():
    near = [100, 110, 108.5, 109.5, 110.5, 112.0]                                                  # Top 110 ... Bottom 108.5 (1.4% from a BUY at 110)
    d1 = mins(7, [(9, 15, 109.2), (9, 16, 109.95, 111.0, 109.95, 110.5), (9, 17, 111.0)])
    d2 = mins(8, [(9, 15, 110.5), (9, 16, 109.0), (9, 17, 108.95)])                                # below 108.9 but above the structural 108.5
    t = run_cap(near + [111.0], d1 + d2).trades[-1]
    assert t.exit_reason == "END_OF_BACKTEST" and t.active_stop == 108.5 and not t.capped


def test_stop_switches_to_structural_when_a_new_bottom_brings_it_within_two_percent():
    closes = FAR + [112, 109, 111, 111]                                                            # new Bottom 109 forms (confirmed after 111)
    d1 = mins(7, [(9, 15, 105.0), (9, 16, 109.95, 111.0, 109.95, 110.5), (9, 17, 111.0)])
    d2 = mins(10, [(9, 15, 111.0), (9, 16, 111.0)])                                                 # 10 Mar: Bottom 109 (pivot 8 Mar) is confirmed, only 0.9% from the entry
    mid = mins(8, [(9, 15, 111.0), (9, 16, 111.0)])                                                 # 8 Mar: still capped at 108.9
    res = run_cap(closes, d1 + mid + d2)
    t = res.trades[-1]
    assert any('STOP CAPPED AT 1%' in e[1] for e in t.events)
    assert t.active_stop == 109.0 and not t.capped
    assert any("STOP BACK TO STRUCTURAL" in e[1] for e in t.events)


def test_breakeven_stop_is_never_loosened_by_the_cap():
    d1 = mins(7, [(9, 15, 105.0), (9, 16, 109.95, 111.0, 109.95, 110.5), (9, 17, 111.0), (9, 18, 112.5, 113.0, 112.5, 112.9)])    # +2.3% on the entry day
    d2 = mins(8, [(9, 15, 112.0), (9, 16, 111.0)])
    t = run_cap(FAR + [113.0], d1 + d2).trades[-1]
    assert t.breakeven_date is not None and t.active_stop >= t.entry_price and not t.capped


def test_rule_off_when_no_cap_distance_is_given():
    d1 = mins(7, [(9, 15, 105.0), (9, 16, 109.95, 111.0, 109.95, 110.5), (9, 17, 111.0)])
    d2 = mins(8, [(9, 15, 110.5), (9, 16, 109.0)])
    res = run_intraday("BANKNIFTY", DAYS[:7], FAR + [111.0], d1 + d2, entry_day_stop_pct=0.01)
    assert res.trades[-1].active_stop == 90.0 and not res.trades[-1].capped                         # original behaviour: structural stop from day 2


# ================================================================ roll_in: a position continued in the next contract
def test_roll_in_opens_a_fresh_entry_with_this_contracts_own_levels_and_rules():
    d1 = mins(7, [(9, 15, 109.0), (9, 16, 109.5), (9, 17, 109.5)])                                  # no crossing of the Top (110) at all
    res = run_intraday("NOV", DAYS[:6], FAR, d1, entry_day_stop_pct=0.01, struct_cap_distance=0.02, roll_in="BUY")
    t = res.trades[-1]
    assert t.direction == "BUY" and t.rolled_in and t.entry_price == 109.0 and t.entry_date == T(7, 9, 15)
    assert t.initial_sl == 90.0 and t.entry_day_sl == round(109.0 * 0.99, 2) and t.active_stop == t.entry_day_sl      # Nov's own Bottom, entry-day 1% stop
    assert any("ROLL IN" in e[1] for e in t.events)


def test_roll_in_entry_day_one_percent_stop_exits_only():
    d1 = mins(7, [(9, 15, 109.0), (9, 16, 107.5), (9, 17, 85.0)])
    res = run_intraday("NOV", DAYS[:6], FAR, d1, entry_day_stop_pct=0.01, struct_cap_distance=0.02, roll_in="BUY")
    assert [t.exit_reason for t in res.trades] == [ENTRY_DAY_STOP_EXIT] and len(res.trades) == 1


def test_roll_in_without_a_system_point_stays_flat_and_says_so():
    res = run_intraday("NOV", DAYS[:3], [100, 105, 104], mins(7, [(9, 15, 104.0), (9, 16, 104.0)]), entry_day_stop_pct=0.01, roll_in="BUY")
    assert res.trades == [] and any(n["kind"] == "ROLL_NO_ANCHOR" for n in res.notes)


def test_roll_in_is_ignored_when_a_position_already_exists_and_only_happens_once():
    d = mins(7, [(9, 15, 109.0), (9, 16, 109.0)]) + mins(8, [(9, 15, 109.0), (9, 16, 109.0)])
    res = run_intraday("NOV", DAYS[:6], FAR + [109.0], d, entry_day_stop_pct=0.01, struct_cap_distance=0.02, roll_in="BUY")
    assert len([t for t in res.trades if t.rolled_in]) == 1
