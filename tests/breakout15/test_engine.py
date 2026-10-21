import datetime as dt

from app.breakout15 import engine as E

D = dt.date(2025, 1, 7)


def t(h, m):
    return dt.datetime.combine(D, dt.time(h, m))


def starts():
    out, x = [], 9 * 60 + 15
    while x < 15 * 60 + 30:
        out.append((x // 60, x % 60))
        x += 15
    return out


def day(first=(100, 110, 90, 105), body=None, skip=(), until=(15, 15)):
    """Full session of 15-minute candles (bar START times). Unlisted candles sit flat at 100."""
    body, bars = body or {}, []
    for hm in starts():
        if hm in skip or hm > until:
            continue
        o, h, l, c = first if hm == (9, 15) else body.get(hm, (100, 100, 100, 100))
        bars.append((t(*hm), o, h, l, c))
    return bars


def test_session_has_25_candles():
    assert len(starts()) == 25 == E.EXPECTED_BARS


def test_first_candle_stats_and_no_entry_day_is_kept():
    r = E.run_day(D, day())
    assert (r.first_open, r.first_high, r.first_low, r.first_close) == (100, 110, 90, 105)
    assert r.first_range_pct == 20.0 and r.first_direction == "Green"
    assert r.exit_reason == E.NO_ENTRY and r.entry_price is None and r.pnl_points is None


def test_red_and_doji_direction():
    assert E.run_day(D, day(first=(100, 110, 90, 95))).first_direction == "Red"
    assert E.run_day(D, day(first=(100, 110, 90, 100))).first_direction == "Doji"


def test_breakout_enters_at_first_high_and_exits_at_market_close():
    r = E.run_day(D, day(body={(10, 0): (108, 111, 108, 110), (15, 15): (115, 116, 114, 120)}))
    assert r.breakout_time == t(10, 0) and r.entry_price == 110
    assert r.exit_reason == E.MARKET_CLOSE and r.exit_price == 120 and r.pnl_points == 10
    assert round(r.pnl_pct, 4) == round(10 / 110 * 100, 4)


def test_opening_gap_enters_at_candle_open():
    r = E.run_day(D, day(body={(9, 30): (112, 113, 111, 112)}))
    assert r.entry_price == 112 and "gap entry at bar open" in r.flags


def test_stop_loss_exit_at_first_candle_low():
    r = E.run_day(D, day(body={(10, 0): (108, 111, 108, 110), (11, 0): (100, 100, 89, 95)}))
    assert r.exit_reason == E.STOP_LOSS and r.exit_price == 90 and r.exit_time == t(11, 0) and r.pnl_points == -20


def test_stop_gap_down_fills_at_open():
    r = E.run_day(D, day(body={(10, 0): (108, 111, 108, 110), (11, 0): (85, 86, 84, 85)}))
    assert r.exit_reason == E.STOP_LOSS and r.exit_price == 85


def test_same_candle_breakout_and_stop_is_flagged_and_assumed_stopped():
    r = E.run_day(D, day(body={(10, 0): (100, 111, 89, 100)}))
    assert r.exit_reason == E.STOP_LOSS and r.exit_price == 90 and any(f.startswith("AMBIGUOUS") for f in r.flags)


def test_low_before_breakout_does_not_cancel_a_later_call():
    r = E.run_day(D, day(body={(10, 0): (100, 100, 85, 90), (11, 0): (100, 112, 100, 111)}))
    assert r.breakout_time == t(11, 0) and r.entry_price == 110


def test_missing_first_candle_is_data_incomplete():
    r = E.run_day(D, day(skip={(9, 15)}))
    assert r.exit_reason == E.DATA_INCOMPLETE and r.entry_price is None and r.first_open is None


def test_missing_candle_hides_possible_breakout_flag_on_no_entry_day():
    r = E.run_day(D, day(skip={(12, 0)}))
    assert r.exit_reason == E.NO_ENTRY and any("cannot be ruled out" in f for f in r.flags)


def test_data_ending_early_is_not_labelled_market_close():
    r = E.run_day(D, day(body={(10, 0): (108, 111, 108, 110)}, until=(14, 0)))
    assert r.exit_reason == E.DATA_ENDED and any("not a verified market close" in f for f in r.flags)


def test_second_last_candle_percentage():
    r = E.run_day(D, day(body={(15, 0): (100, 103, 99, 102)}))
    assert r.second_last_time == t(15, 0) and r.second_last_close == 102 and round(r.second_last_change_pct, 2) == 2.0


def test_summary_counts_and_drawdown():
    days = [E.DayResult(D + dt.timedelta(days=i), entry_price=100, pnl_points=p,
                        exit_reason=E.MARKET_CLOSE if p > 0 else E.STOP_LOSS) for i, p in enumerate([10, -30, 5, -5])]
    days.append(E.DayResult(D + dt.timedelta(days=9)))
    s = E.summarize(days)
    assert s["trading_days"] == 5 and s["call_entries"] == 4 and s["no_entry_days"] == 1
    assert s["winning_trades"] == 2 and s["losing_trades"] == 2 and s["win_rate"] == 50.0
    assert s["net_points"] == -20 and s["max_drawdown_points"] == 30 and s["market_close_exits"] == 2


def test_run_backtest_one_row_per_day():
    d2 = D + dt.timedelta(days=1)
    b2 = [(x.replace(day=d2.day), *rest) for (x, *rest) in day()]
    rows, s = E.run_backtest(day() + b2)
    assert [r.date for r in rows] == [D, d2] and s["trading_days"] == 2
