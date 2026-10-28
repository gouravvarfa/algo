"""Live prices from Upstox with the same surface as the Angel One WebSocket feed: prices -> ticks -> 1-minute candles, health, status, and what it does with no login.
The Upstox call is faked: no network."""
import asyncio
import datetime as dt
import time

import pytest

from app.live import upstox_feed as uf
from app.live.ticks import STALE_AFTER_SECONDS, Tick
from app.providers import upstox_client
from app.providers.upstox_provider import UpstoxNotConnected

BANK, KEY = "NSE_INDEX|Nifty Bank", "NSE_FO|48699"


class Provider:
    def __init__(self):
        self.forgot = 0
        self.has_login = True

    def access_token(self):
        if not self.has_login:
            raise UpstoxNotConnected("Upstox is not connected yet. Open the Upstox page in the Developer console and connect it.")
        return "tok"

    def forget_login(self):
        self.forgot += 1


@pytest.fixture
def prices(monkeypatch):
    s = type("S", (), {})()
    s.calls, s.prices, s.fail = [], {BANK: 54279.2, KEY: 54300.0}, None

    async def get_ltp(token, keys):
        s.calls.append((token, tuple(keys)))
        if s.fail:
            raise s.fail
        return {k: v for k, v in s.prices.items() if k in keys}

    monkeypatch.setattr(upstox_client, "get_ltp", get_ltp)
    return s


def feed(provider=None):
    f = uf.UpstoxPollFeed(provider or Provider())
    f.subscribe("NSE", BANK)
    f.subscribe("NFO", KEY)
    return f


def run(coro):
    return asyncio.run(coro)


def test_one_poll_turns_prices_into_ticks_and_a_minute_candle(prices):
    f = feed()
    assert run(f.poll_once()) == 2
    t = f.latest[BANK]
    assert isinstance(t, Tick) and t.token == BANK and t.ltp == 54279.2 and t.exch_ts.tzinfo is not None and abs(t.recv_at - time.time()) < 5
    minutes = f.builder.minutes(KEY)
    assert len(minutes) == 1 and minutes[0][1:] == (54300.0, 54300.0, 54300.0, 54300.0)
    assert prices.calls == [("tok", tuple(sorted([BANK, KEY])))]                         # ONE request for every watched price, with the saved login


def test_the_minute_candle_follows_the_prices_inside_a_minute(prices):
    f = feed()
    for p in (54300.0, 54350.0, 54280.0, 54310.0):
        prices.prices[KEY] = p
        run(f.poll_once())
    (m,) = f.builder.minutes(KEY)[-1:]
    assert m[1:] == (54300.0, 54350.0, 54280.0, 54310.0) or len(f.builder.minutes(KEY)) > 1      # open, high, low, close (or the minute rolled over)


def test_subscriptions_are_kept_and_can_be_dropped(prices):
    f = feed()
    f.subscribe("NFO", KEY)                                                 # twice is the same
    assert f.keys() == sorted([BANK, KEY]) and f.status()["subscribed"] == 2
    f.unsubscribe_all_for(KEY)
    assert f.keys() == [BANK]
    run(f.poll_once())
    assert prices.calls[-1][1] == (BANK,)


def test_nothing_is_asked_when_nothing_is_watched(prices):
    f = uf.UpstoxPollFeed(Provider())
    assert run(f.poll_once()) == 0 and prices.calls == []


def test_a_zero_or_missing_price_is_not_a_tick(prices):
    f = feed()
    prices.prices = {BANK: 0.0}
    assert run(f.poll_once()) == 1 and BANK not in f.latest and KEY not in f.latest
    assert not f.updated.is_set()


def test_health_needs_a_connection_and_a_fresh_price(prices):
    f = feed()
    run(f.poll_once())
    assert not f.healthy() and not f.healthy(KEY)                           # not connected yet
    f.connected = True
    assert f.healthy() and f.healthy(KEY) and f.age(KEY) < 5
    f.latest[KEY] = Tick(KEY, 1, 1.0, dt.datetime.now(uf.IST), time.time() - STALE_AFTER_SECONDS - 5)
    assert not f.healthy(KEY) and f.healthy(BANK)                           # one stale price does not make the other one stale
    assert f.age("nope") is None and not f.healthy("nope")


def test_the_status_has_what_the_engine_and_the_developer_panel_read(prices):
    f = feed()
    run(f.poll_once())
    f.connected = True
    s = f.status()
    assert set(s) == {"connected", "last_tick_age_s", "reconnects", "subscribed", "error", "source"}
    assert s["connected"] is True and s["subscribed"] == 2 and s["source"] == "Upstox" and s["error"] is None and 0 <= s["last_tick_age_s"] < 5


def test_the_loop_connects_polls_and_stops(prices, monkeypatch):
    monkeypatch.setattr(uf, "POLL_OPEN_S", 0.05)
    monkeypatch.setattr(uf, "POLL_CLOSED_S", 0.05)

    async def go():
        f = feed()
        f.start()
        await asyncio.wait_for(f.updated.wait(), 5)
        assert f.connected and f.last_error is None and f.healthy(BANK)
        n = len(prices.calls)
        await asyncio.sleep(0.2)
        assert len(prices.calls) > n                                        # it keeps asking
        await f.stop()
        await asyncio.sleep(0.1)
        after = len(prices.calls)
        await asyncio.sleep(0.2)
        assert len(prices.calls) == after

    run(go())


def test_with_no_login_the_feed_says_so_and_connects_by_itself_once_there_is_one(prices, monkeypatch):
    monkeypatch.setattr(uf, "POLL_OPEN_S", 0.05)
    monkeypatch.setattr(uf, "POLL_CLOSED_S", 0.05)
    monkeypatch.setattr(uf, "NO_LOGIN_RETRY_S", 0.05)

    async def go():
        p = Provider()
        p.has_login = False
        f = feed(p)
        f.start()
        await asyncio.sleep(0.3)
        assert not f.connected and "UpstoxNotConnected" in f.last_error and "Developer console" in f.last_error and prices.calls == []
        p.has_login = True                                                   # the developer pressed Connect with Upstox
        await asyncio.wait_for(f.updated.wait(), 5)
        assert f.connected and f.last_error is None
        await f.stop()

    run(go())


def test_an_ended_login_is_forgotten_and_the_feed_recovers_from_a_blip(prices, monkeypatch):
    monkeypatch.setattr(uf, "POLL_OPEN_S", 0.05)
    monkeypatch.setattr(uf, "POLL_CLOSED_S", 0.05)

    async def go():
        p = Provider()
        f = feed(p)
        prices.fail = upstox_client.UpstoxApiError("Invalid token used to access API", "auth")
        f.start()
        await asyncio.sleep(0.4)
        assert not f.connected and p.forgot >= 1 and f.reconnects >= 1
        prices.fail = None
        await asyncio.wait_for(f.updated.wait(), 8)
        assert f.connected
        await f.stop()

    run(go())


def test_restart_starts_a_fresh_loop_and_keeps_the_subscriptions(prices, monkeypatch):
    monkeypatch.setattr(uf, "POLL_OPEN_S", 0.05)
    monkeypatch.setattr(uf, "POLL_CLOSED_S", 0.05)

    async def go():
        f = feed()
        f.start()
        first = f._task
        await asyncio.wait_for(f.updated.wait(), 5)
        await f.restart()
        assert f._task is not first and first.done() and f.keys() == sorted([BANK, KEY]) and f.last_error is None
        f.updated.clear()
        await asyncio.wait_for(f.updated.wait(), 5)
        await f.stop()

    run(go())


def test_the_market_hours_rule():
    d = dt.datetime
    assert uf._market_open(d(2026, 10, 27, 10, 0)) and uf._market_open(d(2026, 10, 27, 15, 30))
    assert not uf._market_open(d(2026, 10, 27, 9, 0)) and not uf._market_open(d(2026, 10, 27, 16, 0)) and not uf._market_open(d(2026, 10, 25, 11, 0))      # a Sunday
