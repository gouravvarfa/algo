"""The scrolling market bar (indices, display only) and its chart: never waits, never wakes the strategy loop, previous close once a day."""
import asyncio
import datetime as dt
from types import SimpleNamespace

import pandas as pd
import pytest

from app.live.ticks import TickFeed
from app.paper_trading import engine as em

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def no_sharing(monkeypatch):
    monkeypatch.setattr(em, "CANDLE_SHARE_SECONDS", 0)
    monkeypatch.setattr(em, "CANDLE_CALL_GAP", 0.0)


def _eng():
    e = em.PaperEngine.__new__(em.PaperEngine)
    e.feed, e._prev_close, e.refreshes, e.calls = TickFeed(lambda: None), {}, [], []
    e._refresh_later = lambda key, load: e.refreshes.append(key)
    return e


def test_every_index_token_is_known_and_the_bar_never_waits():
    assert len(em.TICKER_INDICES) == 19 and em.TICKER_INDICES["SENSEX"][:2] == ("BSE", "99919000")
    e = _eng()
    items = e.ticker()                                                   # nothing loaded yet: answers at once, loads in the background
    assert len(items) == 19 and all(i["price"] is None for i in items) and len(e.refreshes) == 19


def test_change_is_against_the_previous_close_and_loaded_once_a_day(monkeypatch):
    e = _eng()
    now = em.now_ist()
    y = (now - dt.timedelta(days=1)).date()
    e._prev_close["NIFTY IT"] = (now, [(y - dt.timedelta(days=1), 37900.0), (y, 38000.0)])
    e.feed.latest["99926008"] = SimpleNamespace(ltp=38380.0, exch_ts=now)
    monkeypatch.setattr(e.feed, "healthy", lambda tok=None: True)
    it = next(i for i in e.ticker() if i["key"] == "NIFTY IT")
    assert it["price"] == 38380.0 and it["change"] == 380.0 and it["pct"] == 1.0 and it["live"]
    assert ("i", "NIFTY IT") not in e.refreshes                         # loaded today already: no candle request
    prev = em._last_session_before(now.date())
    e._prev_close["NIFTY IT"] = (dt.datetime.combine(prev, dt.time(11, 0)), [(prev - dt.timedelta(days=1), 37900.0), (prev, 38000.0)])
    e.ticker()
    assert ("i", "NIFTY IT") in e.refreshes                             # loaded while that session still ran (its close unfinished): asked again


def test_index_ticks_never_wake_the_strategy_loop():
    f = TickFeed(lambda: None)
    f.display_only.add("99926008")
    assert "99926008" in f.display_only


def test_the_chart_is_cached_and_carries_the_live_price():
    e = _eng()
    idx = pd.date_range("2026-11-02", periods=5, freq="B", tz=IST)
    df = pd.DataFrame({"open": [1.0] * 5, "high": [2.0] * 5, "low": [0.5] * 5, "close": [1.5] * 5, "volume": 0}, index=idx)

    async def candles(*a, **k):
        e.calls.append(a)
        return df
    e._candles = candles
    out = asyncio.run(e.index_chart("NIFTY IT", "1D"))
    asyncio.run(e.index_chart("NIFTY IT", "1D"))
    assert len(out) == 5 and len(e.calls) == 1 and e.calls[0][:3] == ("NSE", "99926008", "ONE_DAY")
    assert out[0]["volume"] == 0.0 and {"time", "open", "high", "low", "close", "volume"} <= set(out[0])


def test_the_sparkline_is_todays_tick_minutes_without_any_api_call():
    e = _eng()
    now = em.now_ist()
    base = now.replace(hour=9, minute=15, second=0, microsecond=0, tzinfo=IST)
    for k in range(120):
        e.feed.builder.bars.setdefault("99926000", {})[base + dt.timedelta(minutes=k)] = [100.0 + k, 100.0 + k, 100.0 + k, 100.0 + k]
    e.feed.builder.bars["99926000"][base - dt.timedelta(days=1)] = [1.0, 1.0, 1.0, 1.0]           # yesterday: not today's line
    it = next(i for i in e.ticker() if i["key"] == "NIFTY")
    assert 40 <= len(it["spark"]) <= 49 and it["spark"][0] == 100.0 and it["spark"][-1] == 219.0 and e.calls == []



def test_on_a_day_without_trading_the_last_sessions_close_and_its_change_are_shown():
    # 10 Nov 2026 (NSE holiday): the bar showed 0.00% for every index; brokers show the last session's change
    e = _eng()
    now = em.now_ist()
    last = (now - dt.timedelta(days=1)).date()
    e._prev_close["NIFTY"] = (now, [(last - dt.timedelta(days=1), 24900.0), (last, 25044.65)])   # last session's close 25,044.65; the one before 24,900
    e.feed.latest["99926000"] = SimpleNamespace(ltp=25044.65, exch_ts=now - dt.timedelta(days=1))   # the last tick is from that session
    it = next(i for i in e.ticker() if i["key"] == "NIFTY")
    assert it["price"] == 25044.65 and it["prev_close"] == 24900.0 and it["change"] == 144.65 and it["pct"] == 0.58
    assert it["today"] is False and it["session"] == last.isoformat() and it["live"] is False


def test_a_chart_seen_before_opens_at_once_and_refreshes_in_the_background():
    e = _eng()
    idx = pd.date_range("2026-11-02", periods=5, freq="B", tz=IST)
    df = pd.DataFrame({"open": [1.0] * 5, "high": [2.0] * 5, "low": [0.5] * 5, "close": [1.5] * 5, "volume": 0}, index=idx)

    async def candles(*a, **k):
        e.calls.append(a)
        return df
    e._candles = candles
    asyncio.run(e.index_chart("NIFTY", "5m"))                                     # first look: waits for Angel One
    e._index_charts[("NIFTY", "5m")] = (em.now_ist() - dt.timedelta(minutes=10), df)     # an old copy
    out = asyncio.run(e.index_chart("NIFTY", "5m"))
    assert len(out) == 5 and len(e.calls) == 1 and ("ic", "NIFTY", "5m") in e.refreshes   # served at once, refreshed in the background



def test_after_a_restart_the_bar_shows_the_stored_closes_at_once():
    # 10 Nov 2026: after a deploy the bar showed blanks for 1-2 minutes, its closes queued behind the strategy's startup candle requests
    e = _eng()
    idx = pd.date_range("2026-11-05", periods=3, freq="B", tz=IST)
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": [100.0, 110.0, 121.0], "volume": 0}, index=idx)

    async def candles(*a, **k):
        return df
    e._candles = candles
    asyncio.run(e._load_index_close("NIFTY IT"))                 # stored on disk
    fresh = _eng()                                                 # a new process: nothing in memory, Angel One not asked yet
    it = next(i for i in fresh.ticker() if i["key"] == "NIFTY IT")
    assert it["price"] == 121.0 and it["change"] == 11.0 and ("i", "NIFTY IT") in fresh.refreshes   # shown at once, refreshed in the background



def test_with_ticks_today_the_reference_is_the_last_close_before_today_even_from_an_old_load():
    # loaded last evening (its last session is yesterday); this morning's first ticks must be compared with yesterday, not the day before
    e = _eng()
    now = em.now_ist()
    y = (now - dt.timedelta(days=1)).date()
    e._prev_close["NIFTY"] = (now - dt.timedelta(hours=12), [(y - dt.timedelta(days=1), 24900.0), (y, 25000.0)])
    e.feed.latest["99926000"] = SimpleNamespace(ltp=25100.0, exch_ts=now)
    it = next(i for i in e.ticker() if i["key"] == "NIFTY")
    assert it["prev_close"] == 25000.0 and it["change"] == 100.0 and it["today"] is True


def test_the_sidebar_and_top_bottom_index_quote_use_the_same_rule():
    e = _eng()
    now = em.now_ist()
    last = (now - dt.timedelta(days=1)).date()
    e._prev_close["BANKNIFTY"] = (now, [(last - dt.timedelta(days=1), 58850.65), (last, 59741.25)])
    q = asyncio.run(e.index_quote("BANKNIFTY"))
    assert q["price"] == 59741.25 and q["change"] == 890.6 and q["pct"] == 1.51 and q["today"] is False      # was +0.00% on the holiday



def test_the_first_closes_file_layout_is_still_read():
    import json
    em.INDEX_CLOSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    em.INDEX_CLOSE_PATH.write_text(json.dumps({"NIFTY": ["2026-11-10T20:45:00", 24986.4, 25044.65, 24986.4, "2026-11-09"]}), encoding="utf-8")
    e = _eng()
    it = next(i for i in e.ticker() if i["key"] == "NIFTY")
    assert it["price"] == 25044.65 and it["change"] == 58.25 and it["session"] == "2026-11-09"


def test_index_closes_are_asked_only_when_incomplete_and_never_on_every_poll():
    # 11 Nov 2026: after a restart in market hours the 19 indices were asked again on every poll of the bar and the strategy's own candles waited ~5 min
    e = _eng()
    now = em.now_ist()
    prev = em._last_session_before(now.date())
    closes = [(prev - dt.timedelta(days=1), 37900.0), (prev, 38000.0)]
    e._prev_close["NIFTY IT"] = (dt.datetime.combine(prev, dt.time(16, 0)), closes)          # loaded after the last session ended: complete
    for _ in range(5):
        items = e.ticker()
    assert ("i", "NIFTY IT") not in e.refreshes                                               # Angel One is not asked at all
    it = next(i for i in items if i["key"] == "NIFTY IT")
    assert it["token"] == "99926008" and it["ref_today"] == 38000.0                           # what the browser needs for pushed ticks
    assert sum(1 for k in e.refreshes if k[0] == "i") == 18                                   # the other 18: once each, not once per poll
    e._prev_close["NIFTY IT"] = (dt.datetime.combine(prev, dt.time(11, 0)), closes)           # loaded DURING that session: its close was unfinished
    for _ in range(5):
        e.ticker()
    assert e.refreshes.count(("i", "NIFTY IT")) == 1


def test_the_last_session_before_a_day_skips_weekends_and_calendar_holidays(monkeypatch):
    from app.ops import market_calendar
    monkeypatch.setattr(market_calendar, "entry", lambda d, ex="NSE": {"kind": "holiday"} if d == dt.date(2026, 11, 10) else None)
    assert em._last_session_before(dt.date(2026, 11, 11)) == dt.date(2026, 11, 9)            # Tuesday 10 Nov 2026 was an NSE holiday
    assert em._last_session_before(dt.date(2026, 11, 9)) == dt.date(2026, 11, 6)             # Monday -> the Friday before

