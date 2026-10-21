"""Capital / margin of simulated legs: buy-only = premium paid; with SELL legs = Angel One's calculator; unlisted legs = no margin (never estimated)."""
import asyncio
import datetime as dt

from app.options import api
from app.options.master import OptionContract, UnderlyingInfo

C = OptionContract("NIFTY27OCT2623000CE", "111", "NIFTY", 23000.0, dt.date(2026, 10, 27), "CE", 65, 0.05)
P = OptionContract("NIFTY27OCT2623000PE", "222", "NIFTY", 23000.0, dt.date(2026, 10, 27), "PE", 65, 0.05)
INFO = UnderlyingInfo("NIFTY", "26000", "Nifty 50", None, [C, P], "test")


def leg(side, opt, expiry="2026-10-27", entry=100.0):
    return api.Position(side=side, opt=opt, strike=23000.0, expiry=expiry, lots=1, lot_size=65, entry=entry, entry_date="2026-10-21")


class FakeProvider:
    async def get_session(self):
        return object()


def run(monkeypatch, legs, calls):
    monkeypatch.setattr(api.master, "load_underlying", lambda s: INFO)

    async def fake_margin(session, basket):
        calls.append(basket)
        return {"totalMarginRequired": 123456.5, "marginComponents": {"spanMargin": 100000.0, "marginBenefit": 5000.0}}
    monkeypatch.setattr(api, "get_margin", fake_margin)
    monkeypatch.setattr(api, "provider", FakeProvider())
    return asyncio.run(api.sim_margin(api.MarginReq(symbol="NIFTY", positions=legs)))


def test_buy_only_basket_needs_its_premium(monkeypatch):
    calls = []
    r = run(monkeypatch, [leg("BUY", "CE", entry=120.0), leg("BUY", "PE", entry=80.0)], calls)
    assert r["margin"] == (120 + 80) * 65 and not calls


def test_sell_basket_uses_the_broker_calculator_with_real_tokens(monkeypatch):
    calls = []
    r = run(monkeypatch, [leg("SELL", "CE"), leg("SELL", "PE")], calls)
    assert r["margin"] == 123456.5 and r["hedge_benefit"] == 5000.0 and r["premium_received"] == 2 * 100 * 65
    assert [(b["token"], b["tradeType"], b["qty"]) for b in calls[0]] == [("111", "SELL", 65), ("222", "SELL", 65)]


def test_expired_leg_gives_no_margin_instead_of_a_guess(monkeypatch):
    calls = []
    r = run(monkeypatch, [leg("SELL", "CE", expiry="2026-09-29")], calls)
    assert r["margin"] is None and r["unlisted"] and not calls
