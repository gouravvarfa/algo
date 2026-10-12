"""15-minute gap protocol: system points are never shifted by a gap, gap-up
entries wait for the first completed 15m candle, and an existing position
that opens through its structural stop is resolved by the first candle's
extreme instead of the ordinary close-based stop. SELL mirrors BUY."""
import datetime as dt

from app.strategy.top_bottom.backtest import run_top_bottom_backtest
from app.strategy.top_bottom.gap_protocol import (
    Bar15,
    FirstCandle,
    first_candle,
    resolve_gap_entry,
    resolve_gap_exit,
)

BASE = dt.datetime(2026, 2, 1)


def _dates(n):
    return [BASE + dt.timedelta(days=i) for i in range(n)]


def _bars(day_idx, first, later):
    """first=(o,h,l,c) is the 09:15 candle; later=[(o,h,l,c), ...] 09:30+."""
    day = (BASE + dt.timedelta(days=day_idx)).replace(hour=9, minute=15)
    out = [Bar15(day, *first)]
    for k, ohlc in enumerate(later, start=1):
        out.append(Bar15(day + dt.timedelta(minutes=15 * k), *ohlc))
    return out


def _run(closes, opens, intraday=None):
    return run_top_bottom_backtest("X", "XFUT", "2026-09-29", "STOCK", 500, _dates(len(closes)), closes, opens, intraday)


def _opens(closes, **override):
    o = closes[:1] + closes[:-1]
    for k, v in override.items():
        o[int(k[1:])] = v
    return o


def _intra(d):
    return {(BASE + dt.timedelta(days=k)).date(): v for k, v in d.items()}


# BUY structure: bottom 10 (idx1), top 40 (idx3); pre-open levels from idx5 on.
BUY_PRE = [20, 10, 30, 40, 35]
# SELL structure: top 50 (idx1), bottom 20 (idx3).
SELL_PRE = [40, 50, 30, 20, 25]


# --- BUY setup ---------------------------------------------------------
def test_buy_gap_down_keeps_system_point_and_enters_on_cross():
    closes = BUY_PRE + [35, 41]
    opens = _opens(closes, d5=10)
    intraday = _intra({5: _bars(5, (10, 13, 9, 12), [(12, 13, 11, 12)])})
    _, trades = _run(closes, opens, intraday)
    assert len(trades) == 1
    t = trades[0]
    assert (t.direction, t.system_point, t.entry_price, t.initial_sl) == ("BUY", 40, 40, 10)


def test_buy_gap_down_never_crossing_gives_no_entry():
    closes = BUY_PRE + [35, 30]
    _, trades = _run(closes, _opens(closes, d5=10), _intra({5: _bars(5, (10, 13, 9, 12), [])}))
    assert trades == []


def test_buy_gap_up_enters_on_first_candle_high_breakout():
    closes = BUY_PRE + [50, 52]
    opens = _opens(closes, d5=45)
    intraday = _intra({5: _bars(5, (45, 47, 43, 46), [(46, 46.5, 44, 45), (45, 48, 45, 48)])})
    signals, trades = _run(closes, opens, intraday)
    t = trades[0]
    assert (t.direction, t.entry_price, t.system_point, t.initial_sl) == ("BUY", 47, 40, 10)
    assert t.entry_date == dt.datetime(2026, 2, 6, 9, 45)
    assert t.gap_state == "GAP_ENTRY" and (t.gap_first_high, t.gap_first_low) == (47, 43)
    assert len(signals) == 1


def test_buy_gap_up_without_breakout_skips_close_based_entry():
    closes = BUY_PRE + [46]
    opens = _opens(closes, d5=45)
    intraday = _intra({5: _bars(5, (45, 47, 43, 46), [(46, 46.9, 44, 46)])})
    _, trades = _run(closes, opens, intraday)
    assert trades == []


# --- Existing BUY, gap through SL ---------------------------------------
# BUY at 40 (idx6 close 41), SL 10; idx7 is the gap day.
BUY_OPEN = BUY_PRE + [35, 41]


def test_existing_buy_gap_down_through_sl_exits_on_first_low_break_no_reverse():
    closes = BUY_OPEN + [8, 30]
    opens = _opens(closes, d7=5)
    intraday = _intra({7: _bars(7, (5, 12, 4, 8), [(8, 9, 3.5, 4.5)])})
    signals, trades = _run(closes, opens, intraday)
    assert len(trades) == 1
    t = trades[0]
    assert (t.exit_reason, t.exit_price, t.gap_state) == ("GAP_EXIT_15M", 4, "GAP_EXITED")
    assert [s.direction for s in signals] == ["BUY"]  # no automatic reverse SELL


def test_existing_buy_first_low_holds_sets_protective_sl_and_suspends_ordinary_stop():
    closes = BUY_OPEN + [8, 20]
    opens = _opens(closes, d7=5)
    # Closes at 8 (< structural SL 10) but never trades below first low 4.
    intraday = _intra({7: _bars(7, (5, 12, 4, 8), [(8, 9, 4.5, 8)])})
    _, trades = _run(closes, opens, intraday)
    assert len(trades) == 1 and trades[0].exit_reason == "END_OF_BACKTEST"
    t = trades[0]
    assert (t.gap_state, t.protective_sl, t.protective_sl_date) == ("GAP_HELD", 4, dt.date(2026, 2, 8))
    assert t.active_stop == 10  # structural stop untouched for later days


def test_protective_sl_is_one_day_only_structural_stop_returns_next_day():
    closes = BUY_OPEN + [8, 8]
    opens = _opens(closes, d7=5)
    intraday = _intra({7: _bars(7, (5, 12, 4, 8), [(8, 9, 4.5, 8)])})
    _, trades = _run(closes, opens, intraday)
    # Day 8 has no intraday data, so the ordinary close < SL 10 rule applies.
    assert trades[0].exit_reason == "TRAILING_STOP_REVERSAL"


# --- SELL mirror ----------------------------------------------------------
def test_sell_point_stays_when_open_is_on_the_wrong_side():
    closes = SELL_PRE + [25, 19]
    opens = _opens(closes, d5=55)
    intraday = _intra({5: _bars(5, (55, 56, 54, 55), [])})
    _, trades = _run(closes, opens, intraday)
    t = trades[0]
    assert (t.direction, t.system_point, t.initial_sl) == ("SELL", 20, 50)


def test_sell_gap_down_enters_on_first_candle_low_breakdown():
    closes = SELL_PRE + [14, 10, 8]
    opens = _opens(closes, d5=15)
    intraday = _intra({5: _bars(5, (15, 17, 14, 15), [(15, 15, 13, 14)])})
    _, trades = _run(closes, opens, intraday)
    t = trades[0]
    assert (t.direction, t.entry_price, t.system_point, t.initial_sl) == ("SELL", 14, 20, 50)


SELL_OPEN = SELL_PRE + [25, 19]


def test_existing_sell_gap_up_through_sl_exits_on_first_high_break_no_reverse():
    closes = SELL_OPEN + [60, 45]
    opens = _opens(closes, d7=55)
    intraday = _intra({7: _bars(7, (55, 56, 52, 55), [(55, 57, 54, 56)])})
    signals, trades = _run(closes, opens, intraday)
    t = trades[0]
    assert (t.exit_reason, t.exit_price) == ("GAP_EXIT_15M", 56)
    assert [s.direction for s in signals] == ["SELL"]


def test_existing_sell_first_high_holds_protective_sl():
    closes = SELL_OPEN + [52, 30]
    opens = _opens(closes, d7=55)
    intraday = _intra({7: _bars(7, (55, 56, 52, 52), [(52, 55.5, 50, 52)])})
    _, trades = _run(closes, opens, intraday)
    t = trades[0]
    assert (t.gap_state, t.protective_sl, t.exit_reason) == ("GAP_HELD", 56, "END_OF_BACKTEST")


# --- Pure-function guards --------------------------------------------------
def test_no_premature_trigger_before_candles_complete():
    bars = _bars(0, (5, 12, 4, 8), [(8, 9, 3, 4)])
    d = BASE.replace(hour=9)
    assert first_candle(bars, d.replace(minute=29)) is None
    assert resolve_gap_exit("BUY", bars, d.replace(minute=29)).status == "GAP_EXIT_PENDING"
    # The 09:30 candle is not complete until 09:45: its low must not fire at 09:44.
    assert resolve_gap_exit("BUY", bars, d.replace(minute=44)).status == "GAP_EXIT_PENDING"
    assert resolve_gap_exit("BUY", bars, d.replace(minute=45)).status == "GAP_EXITED"
    entry = _bars(0, (45, 47, 43, 46), [(46, 50, 45, 49)])
    assert resolve_gap_entry("BUY", entry, d.replace(minute=44)).status == "WAITING"
    assert resolve_gap_entry("BUY", entry, d.replace(minute=45)).status == "TRIGGERED"


def test_first_candle_is_frozen_when_supplied():
    bars = _bars(0, (5, 12, 4, 8), [(8, 9, 3.8, 4)])
    res = resolve_gap_exit("BUY", bars, frozen=FirstCandle(high=12, low=3, open=5))
    assert res.status == "GAP_HELD" and res.protective_sl == 3  # 3.8 never breaks frozen low 3


def test_exit_reports_first_breach_only():
    bars = _bars(0, (5, 12, 4, 8), [(8, 9, 3, 4), (4, 5, 2, 3)])
    res = resolve_gap_exit("BUY", bars)
    assert res.exit_time == BASE.replace(hour=9, minute=30)


# --- Existing behaviour unchanged --------------------------------------------
def test_no_gap_days_identical_with_or_without_intraday_data():
    closes = BUY_OPEN + [30, 20, 5, 4]
    opens = _opens(closes)
    quiet = _intra({k: _bars(k, (closes[k - 1],) * 4, []) for k in range(1, len(closes))})
    key = lambda r: [(t.direction, t.entry_price, t.exit_price, t.exit_reason) for t in r[1]]
    a = _run(closes, opens)
    assert key(a) == key(_run(closes, opens, quiet))
    assert key(a) == key(_run(closes, opens, {}))
