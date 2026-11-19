"""Yesterday's daily candle must be the FINAL one before today's rules use it (17-18 Nov 2026: SUNPHARMA's 17 Nov bar in the store was a mid-afternoon
copy, it hid this morning's gap and a wrong paper SELL was journaled at 09:15). Pure helpers; candles are SYNTHETIC."""
import calendar
import datetime as dt

import pandas as pd

from app.live import candle_store
from app.paper_trading.engine import MCX_DAY_CLOSE, NSE_DAY_CLOSE, complete_daily, daily_chart_candles, day_close_for, final_daily, unconfirmed_day
from app.strategy.top_bottom.intraday import Minute


def bars(*days):
    idx = pd.DatetimeIndex([dt.datetime(2026, 11, d) for d in days])
    return pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 0.0}, index=idx)


def test_the_session_close_of_each_exchange():
    assert day_close_for("MCX") == MCX_DAY_CLOSE == dt.time(23, 55) and day_close_for("NFO") == NSE_DAY_CLOSE == dt.time(15, 30)


def test_a_forming_bar_is_never_stored_but_a_finished_one_is():
    df = bars(16, 17)
    assert list(complete_daily(df, dt.datetime(2026, 11, 17, 15, 21), NSE_DAY_CLOSE).index.day) == [16]      # 15:21 on the 17th: the 17th still forming
    assert list(complete_daily(df, dt.datetime(2026, 11, 17, 15, 31), NSE_DAY_CLOSE).index.day) == [16, 17]  # after the close: final
    assert list(complete_daily(df, dt.datetime(2026, 11, 17, 23, 0), MCX_DAY_CLOSE).index.day) == [16]       # MCX trades until 23:55


def test_a_copy_fetched_before_yesterdays_close_does_not_pass_yesterdays_bar_as_final():
    df = bars(16, 17)
    morning = dt.datetime(2026, 11, 18, 9, 15)
    assert list(final_daily(df, dt.datetime(2026, 11, 17, 15, 10), morning, NSE_DAY_CLOSE).index.day) == [16]      # fetched mid-afternoon
    assert list(final_daily(df, dt.datetime(2026, 11, 17, 18, 0), morning, NSE_DAY_CLOSE).index.day) == [16, 17]   # fetched after the close
    assert list(final_daily(df, dt.datetime(2026, 11, 18, 8, 0), morning, NSE_DAY_CLOSE).index.day) == [16, 17]    # fetched this morning
    assert list(final_daily(df, dt.datetime(2026, 11, 17, 23, 50), morning, MCX_DAY_CLOSE).index.day) == [16]      # MCX: 23:50 is before its close


def test_a_missing_yesterday_is_found_from_the_minutes():
    mins = [Minute(dt.datetime(2026, 11, d, 9, 15), 1, 1, 1, 1) for d in (16, 17, 18)]
    today = dt.date(2026, 11, 18)
    assert unconfirmed_day(mins, [dt.date(2026, 11, 16)], today) == dt.date(2026, 11, 17)
    assert unconfirmed_day(mins, [dt.date(2026, 11, 16), dt.date(2026, 11, 17)], today) is None
    assert unconfirmed_day([], [dt.date(2026, 11, 16)], today) is None
    assert unconfirmed_day([Minute(dt.datetime(2026, 11, 18, 9, 15), 1, 1, 1, 1)], [], today) is None          # only today's minutes: nothing to check


def test_start_up_removes_todays_stored_daily_bars_only(tmp_path):
    db = tmp_path / "c.db"
    candle_store.put("1", "ONE_DAY", bars(17, 18), db=db)
    candle_store.put("1", "ONE_MINUTE", pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0.0},
                                                       index=pd.DatetimeIndex([dt.datetime(2026, 11, 18, 9, 15)])), db=db)
    assert candle_store.drop_day("ONE_DAY", dt.date(2026, 11, 18), db=db) == 1
    assert list(candle_store.get("1", "ONE_DAY", db=db).index.day) == [17]
    assert len(candle_store.get("1", "ONE_MINUTE", db=db)) == 1                                                   # minutes untouched


def test_the_live_price_only_ever_moves_todays_candle():
    """19 Nov 2026 09:20-09:49: Angel One refused BANKNIFTY's daily candles, the stored copy ended on 18 Nov and the tick was written into the
    18 Nov candle (it showed today's price, today had no candle). Today's candle is built from today's minutes instead; yesterday stays final."""
    now = dt.datetime(2026, 11, 19, 9, 30)
    mins = [Minute(dt.datetime(2026, 11, 18, 15, 29), 9.0, 9.0, 9.0, 9.0),
            Minute(dt.datetime(2026, 11, 19, 9, 15), 102.0, 103.0, 101.5, 102.5), Minute(dt.datetime(2026, 11, 19, 9, 16), 102.5, 104.0, 102.0, 103.5)]
    out = daily_chart_candles(bars(17, 18), 103.0, now, mins)
    assert [(c["close"], c["high"], c["low"]) for c in out[:2]] == [(100.5, 101.0, 99.0)] * 2                       # 17 and 18 Nov untouched
    assert out[-1] == {"time": calendar.timegm((2026, 11, 19, 0, 0, 0)), "open": 102.0, "high": 104.0, "low": 101.5, "close": 103.0, "src": "ticks"}
    out = daily_chart_candles(bars(18, 19), 103.0, now, mins)                       # Angel One's forming bar of today is there: it follows the tick
    assert len(out) == 2 and (out[-1]["close"], out[-1]["high"], out[-1]["low"]) == (103.0, 103.0, 99.0) and "src" not in out[-1]
    assert daily_chart_candles(bars(17, 18), None, now, mins)[-1]["close"] == 100.5  # no live tick: nothing is added
    assert daily_chart_candles(bars(17, 18), 103.0, now, [])[-1]["open"] == 103.0     # no minute of today yet: the tick alone
