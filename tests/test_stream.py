"""Live tick push to browsers (SSE hub): conflation, token filtering, snapshot first, heartbeat, sign-out cut-off, cleanup. Synthetic ticks only; no network."""
import asyncio
import datetime as dt
import json
import time

import pytest

from app.live import stream
from app.live.ticks import Tick, TickFeed

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def tick(token, ltp, secs=0.0):
    t = dt.datetime(2026, 10, 27, 10, 0, 0, tzinfo=IST) + dt.timedelta(seconds=secs)
    return Tick(token, 2, ltp, t, time.time())


class FakeFeed:
    def __init__(self):
        self.latest, self.subs, self.connected = {}, [], True

    def subscribe(self, exch, token):
        self.subs.append((exch, token))


def parse(chunk: bytes):
    """SSE bytes -> (event, data) or ('comment', text)."""
    s = chunk.decode()
    if s.startswith(":"):
        return "comment", s.strip()
    if s.startswith("retry:"):
        return "retry", s.strip()
    ev, data = s.split("\n")[0][7:], json.loads(s.split("\n")[1][6:])
    return ev, data


def test_specs_are_validated_not_guessed():
    assert stream.parse_specs("NFO:61466, nse:99926009 ,NFO:61466") == [("NFO", "61466"), ("NSE", "99926009")]
    for bad in ("", "61466", "NFO:abc", "NFO:1;DROP", "TOOLONG:1"):
        with pytest.raises(stream.StreamError):
            stream.parse_specs(bad)
    with pytest.raises(stream.StreamError):
        stream.parse_specs(",".join(f"NFO:{i}" for i in range(stream.MAX_TOKENS_PER_CLIENT + 1)))


def test_opening_subscribes_the_feed_and_closing_cleans_up():
    feed = FakeFeed()
    hub = stream.StreamHub(feed)
    c = hub.open("u1", [("NFO", "1"), ("NSE", "2")])
    assert feed.subs == [("NFO", "1"), ("NSE", "2")] and hub.by_token["1"] == {c.id}
    hub.close(c)
    hub.close(c)                                        # twice is harmless
    assert not hub.clients and not hub.by_token and hub.disconnects == 1


def test_snapshot_first_then_only_changed_tokens_with_the_latest_price():
    async def go():
        feed = FakeFeed()
        feed.latest["1"] = tick("1", 100.0)
        hub = stream.StreamHub(feed)
        c = hub.open("u1", [("NFO", "1"), ("NFO", "2")])
        g = hub.events(c, lambda: True)
        assert parse(await g.__anext__())[0] == "retry"
        ev, snap = parse(await g.__anext__())
        assert ev == "snap" and snap["p"] == {"1": [100.0, snap["p"]["1"][1]]} and snap["feed"] is True      # token 2 has no price yet: absent, not invented
        hub.on_tick(tick("1", 101.0)); hub.on_tick(tick("1", 102.5)); hub.on_tick(tick("2", 7.0)); hub.on_tick(tick("9", 1.0))      # 9 is nobody's token
        ev, data = parse(await g.__anext__())
        assert ev == "ticks" and data["p"]["1"][0] == 102.5 and data["p"]["2"][0] == 7.0 and "9" not in data["p"]      # conflated to the latest, and filtered
        await g.aclose()
        assert not hub.clients                           # the generator's finally unregistered the client
    asyncio.run(go())


def test_two_clients_get_only_their_own_tokens():
    async def go():
        hub = stream.StreamHub(FakeFeed())
        a, b = hub.open("a", [("NFO", "1")]), hub.open("b", [("NFO", "2")])
        ga, gb = hub.events(a, lambda: True), hub.events(b, lambda: True)
        for g in (ga, gb):
            await g.__anext__(); await g.__anext__()
        hub.on_tick(tick("1", 5.0)); hub.on_tick(tick("2", 6.0))
        assert list(parse(await ga.__anext__())[1]["p"]) == ["1"] and list(parse(await gb.__anext__())[1]["p"]) == ["2"]
        await ga.aclose(); await gb.aclose()
    asyncio.run(go())


def test_a_quiet_stream_sends_a_heartbeat(monkeypatch):
    monkeypatch.setattr(stream, "HEARTBEAT_SECONDS", 0.05)
    async def go():
        hub = stream.StreamHub(FakeFeed())
        g = hub.events(hub.open("u", [("NFO", "1")]), lambda: True)
        await g.__anext__(); await g.__anext__()
        assert parse(await g.__anext__()) == ("comment", ": hb")
        await g.aclose()
    asyncio.run(go())


def test_a_signed_out_browser_is_cut_off(monkeypatch):
    monkeypatch.setattr(stream, "HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr(stream, "RECHECK_SECONDS", 0.0)
    async def go():
        hub = stream.StreamHub(FakeFeed())
        g = hub.events(hub.open("u", [("NFO", "1")]), lambda: False)       # the login is no longer valid
        await g.__anext__(); await g.__anext__()
        seen = []
        async for chunk in g:
            seen.append(parse(chunk))
        assert ("bye", {"reason": "signed out"}) in seen and not hub.clients
    asyncio.run(go())


def test_the_stream_ends_after_its_lifetime_so_the_browser_reconnects(monkeypatch):
    monkeypatch.setattr(stream, "HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr(stream, "MAX_LIFETIME_SECONDS", -1)
    async def go():
        hub = stream.StreamHub(FakeFeed())
        g = hub.events(hub.open("u", [("NFO", "1")]), lambda: True)
        out = [parse(c) async for c in g]
        assert out[-1] == ("bye", {"reason": "refresh"})
    asyncio.run(go())


def test_stats_report_clients_rates_and_lag():
    async def go():
        hub = stream.StreamHub(FakeFeed())
        c = hub.open("user-1234567890", [("NFO", "1")], "TestBrowser/1")
        g = hub.events(c, lambda: True)
        await g.__anext__(); await g.__anext__()
        hub.on_tick(tick("1", 1.0))
        await g.__anext__()
        s = hub.stats()
        assert s["clients"] == 1 and s["tokens_watched"] == 1 and s["msgs_per_s"] > 0 and s["lag_samples"] == 1 and s["lag_ms_avg"] >= 0
        assert s["list"][0]["user"] == "user-123" and s["list"][0]["ua"] == "TestBrowser/1" and s["connects"] == 1
        await g.aclose()
        assert hub.stats()["clients"] == 0 and hub.stats()["disconnects"] == 1
    asyncio.run(go())


def test_the_feed_calls_every_listener_and_a_failing_one_does_not_stop_the_others():
    feed = TickFeed(lambda: None)
    got = []
    feed.listeners.append(lambda t: 1 / 0)
    feed.listeners.append(lambda t: got.append(t.token))
    feed._notify(tick("7", 1.0))                       # the same call the receive loop makes for every accepted tick
    feed._notify(tick("8", 1.0))
    assert got == ["7", "8"] and feed._listener_errors == 2
