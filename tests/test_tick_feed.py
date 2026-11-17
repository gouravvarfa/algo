"""Live tick feed pieces. Frames are built with struct exactly like the real Angel One binary format (verified on the live feed)."""
import asyncio
import datetime as dt
import json
import struct
import time
from types import SimpleNamespace

from app.live import ticks

IST = ticks.IST


def frame(token="48699", ltp=54939.8, ts=dt.datetime(2026, 10, 21, 13, 5, 26, 673000, tzinfo=IST), mode=1, exch=2):
    return (struct.pack("<bb", mode, exch) + token.encode().ljust(25, b"\x00") + struct.pack("<q", 38233027)
            + struct.pack("<q", int(ts.timestamp() * 1000)) + struct.pack("<i", int(round(ltp * 100))))


def test_parse_tick_reads_token_price_and_exchange_time():
    t = ticks.parse_tick(frame())
    assert (t.token, t.exchange_type, t.ltp) == ("48699", 2, 54939.8) and t.exch_ts == dt.datetime(2026, 10, 21, 13, 5, 26, 673000, tzinfo=IST)


def test_parse_tick_rejects_garbage_instead_of_guessing():
    assert ticks.parse_tick(b"x" * 10) is None
    assert ticks.parse_tick(frame(ltp=0.0)) is None
    assert ticks.parse_tick(frame(token="")) is None


def test_minute_builder_ohlc_from_ticks_only():
    b = ticks.MinuteBuilder()
    base = dt.datetime(2026, 10, 21, 10, 12, tzinfo=IST)
    for sec, px in ((5, 100.0), (20, 103.0), (40, 99.0), (59, 101.0)):
        b.add(ticks.Tick("1", 2, px, base + dt.timedelta(seconds=sec), time.time()))
    b.add(ticks.Tick("1", 2, 105.0, base + dt.timedelta(minutes=1, seconds=1), time.time()))
    m = b.minutes("1")
    assert m[0] == (base, 100.0, 103.0, 99.0, 101.0) and m[1][1:] == (105.0, 105.0, 105.0, 105.0) and b.minutes("2") == []


def test_minute_builder_makes_candles_only_inside_the_trading_session():
    """17 Nov 2026: on every (re)subscribe before the open Angel One sent each stock future's last price stamped 06:55 (04:05 after an earlier restart);
    the builder made 'today's first minute' of it and the strategy entered SELL at 06:55 with NSE shut. Off-session ticks make no candle now."""
    b = ticks.MinuteBuilder()
    tue = dt.date(2026, 11, 17)

    def add(token, exch, hh, mm, ss=0, day=tue):
        b.add(ticks.Tick(token, exch, 100.0, dt.datetime.combine(day, dt.time(hh, mm, ss), tzinfo=IST), time.time()))

    for hh, mm in ((4, 5), (6, 55), (9, 0), (9, 8), (9, 14), (15, 30), (15, 39), (18, 15)):
        add("nfo", 2, hh, mm)                                         # NFO: off-hours snapshots, the F&O pre-open, after the close, a Muhurat-like evening
    add("nfo", 2, 10, 0, day=dt.date(2026, 11, 21))                   # a Saturday
    assert b.minutes("nfo") == []
    add("nfo", 2, 9, 15)
    add("nfo", 2, 15, 29, 59)
    assert [m[0].time() for m in b.minutes("nfo")] == [dt.time(9, 15), dt.time(15, 29)]
    for hh, mm in ((6, 55), (8, 59), (23, 55), (23, 59)):
        add("mcx", 5, hh, mm)
    assert b.minutes("mcx") == []
    for hh, mm in ((9, 0), (17, 0), (23, 54)):                        # MCX opens 09:00; on an NSE holiday it trades from 17:00
        add("mcx", 5, hh, mm)
    assert [m[0].time() for m in b.minutes("mcx")] == [dt.time(9, 0), dt.time(17, 0), dt.time(23, 54)]


def test_the_feed_keeps_the_latest_price_of_an_off_session_tick_but_makes_no_candle():
    feed = ticks.TickFeed(get_session=lambda: None)
    t = ticks.parse_tick(frame(ts=dt.datetime(2026, 11, 17, 6, 55, 1, tzinfo=IST)))
    feed.latest[t.token] = t                                          # what the socket loop does for every frame
    feed.builder.add(t)
    assert feed.latest["48699"].ltp == 54939.8 and feed.builder.minutes("48699") == []


class M:
    def __init__(self, ts, o, h, l, c):
        self.ts, self.open, self.high, self.low, self.close = ts, o, h, l, c


def test_merge_minutes_rest_wins_ticks_only_fill_gaps():
    t0 = dt.datetime(2026, 10, 21, 10, 0, tzinfo=IST)
    rest = [M(t0, 1, 2, 0.5, 1.5), M(t0 + dt.timedelta(minutes=1), 1.5, 2.5, 1.0, 2.0)]
    tk = [(t0 + dt.timedelta(minutes=1), 9, 9, 9, 9), (t0 + dt.timedelta(minutes=2), 2.0, 3.0, 1.9, 2.8)]
    out = ticks.merge_minutes(rest, tk)
    assert [m.ts for m in out] == [t0, t0 + dt.timedelta(minutes=1), t0 + dt.timedelta(minutes=2)]
    assert out[1].close == 2.0 and out[2].high == 3.0 and isinstance(out[2], M)           # REST minute untouched; the missing minute filled from ticks
    assert ticks.merge_minutes(rest, []) == rest and ticks.merge_minutes([], tk) == []


def test_merge_minutes_handles_naive_rest_timestamps():
    t0 = dt.datetime(2026, 10, 21, 10, 0)
    out = ticks.merge_minutes([M(t0, 1, 1, 1, 1)], [(t0.replace(tzinfo=IST) + dt.timedelta(minutes=1), 2, 2, 2, 2)])
    assert out[-1].ts == t0 + dt.timedelta(minutes=1) and out[-1].ts.tzinfo is None


def test_feed_health_requires_connection_and_a_recent_tick():
    f = ticks.TickFeed(lambda: None)
    f.latest["1"] = ticks.Tick("1", 2, 100.0, dt.datetime.now(IST), time.time())
    assert not f.healthy("1")                                    # tick is fresh but the socket is not connected
    f.connected = True
    assert f.healthy("1") and f.healthy() and not f.healthy("2")
    f.latest["1"] = ticks.Tick("1", 2, 100.0, dt.datetime.now(IST), time.time() - ticks.STALE_AFTER_SECONDS - 1)
    assert not f.healthy("1")                                    # silent feed is treated as down, not trusted
    assert f.status()["connected"] is True and f.status()["last_tick_age_s"] >= ticks.STALE_AFTER_SECONDS


def test_subscribe_message_groups_tokens_by_exchange():
    f = ticks.TickFeed(lambda: None)
    f.subscribe("NFO", "48699"); f.subscribe("NSE", "99926009"); f.subscribe("NFO", "1")
    msg = json.loads(f._sub_message(f.subs))
    assert msg["action"] == 1 and msg["params"]["mode"] == 1
    assert {(x["exchangeType"], tuple(x["tokens"])) for x in msg["params"]["tokenList"]} == {(2, ("1", "48699")), (1, ("99926009",))}
    try:
        f.subscribe("XYZ", "1")
        raise AssertionError("unknown exchange must be rejected")
    except ValueError:
        pass
