"""Live option chain subscriptions: positions' legs are never dropped, other users of a token are respected, idle chains give tokens back."""
import asyncio
import json

from app.live import ticks
from app.ops import context as ops_context
from app.options import api


class Feed(ticks.TickFeed):
    def __init__(self):
        super().__init__(get_session=None)


class Hub:
    def __init__(self, toks):
        self.by_token = {t: {1} for t in toks}


class Engine:
    def __init__(self, toks):
        self.instruments = {i: type("I", (), {"token": t})() for i, t in enumerate(toks)}


def setup(monkeypatch, hub=(), paper=()):
    monkeypatch.setattr(api, "_CHAINS", {})
    monkeypatch.setattr(api, "_PINS", {})
    monkeypatch.setattr(api, "_OPTION_TOKENS", set())
    monkeypatch.setattr(api, "live_engine", Engine(paper))
    monkeypatch.setattr(ops_context, "stream_hub", Hub(hub), raising=False)
    return Feed()


def chain(sym, exp, toks, at):
    api._OPTION_TOKENS.update(toks)
    api._CHAINS[(sym, exp)] = (at, list(toks))


def test_every_strike_of_a_chain_is_subscribed(monkeypatch):
    f = setup(monkeypatch)
    toks = [str(i) for i in range(246)]
    chain("NIFTY", "e1", toks, 100.0)
    api._sync_subs(f, now=100.0)
    assert f.subscribed("NFO") == set(toks)


def test_an_idle_chain_gives_its_tokens_back_but_a_position_leg_stays(monkeypatch):
    f = setup(monkeypatch)
    chain("NIFTY", "e1", ["1", "2", "3"], 100.0)
    api._PINS["2"] = 100.0
    api._sync_subs(f, now=100.0)
    api._sync_subs(f, now=100.0 + api.CHAIN_IDLE_SECONDS + 1)
    assert f.subscribed("NFO") == {"2"}


def test_position_legs_come_first_and_are_kept_past_the_cap(monkeypatch):
    f = setup(monkeypatch)
    monkeypatch.setattr(api, "LIVE_MAX_TOKENS", 4)
    for t in ("p1", "p2", "p3"):
        api._PINS[t] = 100.0
        api._OPTION_TOKENS.add(t)
    chain("NIFTY", "e1", ["a", "b", "c", "d"], 100.0)       # nearest the ATM first
    api._sync_subs(f, now=100.0)
    assert f.subscribed("NFO") == {"p1", "p2", "p3", "a"}


def test_tokens_a_browser_stream_or_paper_trading_uses_are_never_dropped(monkeypatch):
    f = setup(monkeypatch, hub=["s"], paper=["fut"])
    api._OPTION_TOKENS.update({"s", "fut", "x"})
    f.subscribe_many("NFO", ["s", "fut", "x"])
    api._sync_subs(f, now=100.0)
    assert f.subscribed("NFO") == {"s", "fut"}


def test_tokens_that_are_not_index_options_are_left_alone(monkeypatch):
    f = setup(monkeypatch)
    f.subscribe_many("NFO", ["future-of-someone"])
    api._sync_subs(f, now=100.0)
    assert f.subscribed("NFO") == {"future-of-someone"}


def test_a_dropped_token_forgets_its_last_price(monkeypatch):
    f = setup(monkeypatch)
    f.subscribe_many("NFO", ["x"])
    f.latest["x"] = object()
    f.unsubscribe_many("NFO", ["x"])
    assert "x" not in f.latest and "x" not in f.subscribed("NFO")


def test_messages_carry_at_most_50_tokens_and_unsubscribe_is_action_0():
    sent = []

    class WS:
        async def send(self, m):
            sent.append(json.loads(m))

    f = Feed()
    f._ws, f.connected = WS(), True
    asyncio.run(f._send_subscribe({2: {str(i) for i in range(120)}}))
    asyncio.run(f._send_subscribe({2: {"1"}}, action=0))
    sizes = [len(m["params"]["tokenList"][0]["tokens"]) for m in sent]
    assert sizes == [50, 50, 20, 1]
    assert [m["action"] for m in sent] == [1, 1, 1, 0]
