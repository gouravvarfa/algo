"""Option chain module: Black-Scholes fallback, strike helpers, and every data-quality rule.
The broker payloads below are SYNTHETIC test fixtures (the live script uses real Angel One data)."""
import asyncio
import datetime as dt

import pytest

from app.options import bs, chain as oc
from app.options.master import IST, OptionContract, UnderlyingInfo, expiries, pick_expiry

EXP = dt.date(2026, 10, 27)


def contract(strike, typ, token=None):
    return OptionContract(f"BANKNIFTY27OCT26{int(strike)}{typ}", token or f"{int(strike)}{typ}", "BANKNIFTY", strike, EXP, typ, 30, 0.05)


def info(strikes=(55400, 55500, 55600)):
    cs = [contract(k, t) for k in strikes for t in ("CE", "PE")]
    return UnderlyingInfo("BANKNIFTY", "99926009", "Nifty Bank", "99926017", cs, "test")


def quote(token, ltp=100.0, bid=99.0, ask=101.0, vol=5000, oi=50000, feed="19-Oct-2026 11:00:00"):
    return {"symbolToken": token, "ltp": ltp, "tradeVolume": vol, "opnInterest": oi, "exchFeedTime": feed, "exchTradeTime": feed,
            "depth": {"buy": [{"price": bid, "quantity": 1, "orders": 1}], "sell": [{"price": ask, "quantity": 1, "orders": 1}]}}


class FakeBroker:
    """Stands in for oc._post: index quotes, option quotes and Greeks."""
    def __init__(self, option_quotes, greeks=None, spot=55500.0, feed="19-Oct-2026 11:00:00"):
        self.option_quotes, self.greeks, self.spot, self.feed = option_quotes, greeks or [], spot, feed

    async def __call__(self, client, session, path, body):
        if path == oc.GREEK_PATH:
            return {"status": True, "data": self.greeks}
        toks = body["exchangeTokens"]
        if "NSE" in toks:
            return {"data": {"fetched": [quote("99926009", self.spot, feed=self.feed), quote("99926017", 14.5, feed=self.feed)], "unfetched": []}}
        return {"data": {"fetched": [q for q in self.option_quotes if q["symbolToken"] in toks["NFO"]], "unfetched": []}}


def run_chain(monkeypatch, option_quotes, now, greeks=None, **kw):
    monkeypatch.setattr(oc, "_post", FakeBroker(option_quotes, greeks, feed=kw.pop("feed", "19-Oct-2026 11:00:00")))
    monkeypatch.setattr(oc, "MIN_SECONDS_BETWEEN_CALLS", 0)
    return asyncio.run(oc.fetch_chain(None, info(), EXP, 1, now=now, **kw))


OPEN = dt.datetime(2026, 10, 19, 11, 0, 3, tzinfo=IST)      # Monday, market hours
CLOSED = dt.datetime(2026, 10, 19, 21, 0, 0, tzinfo=IST)


def all_quotes(**over):
    return [quote(f"{k}{t}", **over) for k in (55400, 55500, 55600) for t in ("CE", "PE")]


# ---------------------------------------------------------------- Black-Scholes fallback
def test_put_call_parity_and_iv_round_trip():
    s, k, t, r, q = 55500.0, 55600.0, 7 / 365, 0.065, 0.0
    c, p = bs.price(s, k, t, r, q, 0.14, True), bs.price(s, k, t, r, q, 0.14, False)
    assert c - p == pytest.approx(s * 1 - k * pow(2.718281828459045, -r * t), abs=1e-6)
    assert bs.implied_vol(c, s, k, t, r, q, True) == pytest.approx(14.0, abs=0.01)
    assert bs.implied_vol(p, s, k, t, r, q, False) == pytest.approx(14.0, abs=0.01)
    assert bs.implied_vol(100.0, s, 54000.0, t, r, q, True) is None    # deep-ITM call priced below intrinsic: no fake IV
    g = bs.greeks(s, k, t, r, q, 0.14, True)
    assert 0 < g["delta"] < 1 and g["gamma"] > 0 and g["theta"] < 0 and g["vega"] > 0


# ---------------------------------------------------------------- master helpers
def test_only_tradable_expiries_and_explicit_pick():
    i = info()
    assert expiries(i, dt.datetime(2026, 10, 27, 15, 29, tzinfo=IST)) == [EXP]
    assert expiries(i, dt.datetime(2026, 10, 27, 15, 31, tzinfo=IST)) == []       # expiry day, after 15:30: gone
    assert pick_expiry(i, "27OCT2026", OPEN) == EXP and pick_expiry(i, "nearest", OPEN) == EXP
    with pytest.raises(ValueError):
        pick_expiry(i, "03NOV2026", OPEN)


def test_window_is_atm_plus_minus_n_of_the_strikes_actually_listed():
    ladder = [54000, 55000, 55100, 55200, 55300, 56000]
    assert oc.window_strikes(ladder, 55120, 1) == [55000, 55100, 55200]
    assert oc.window_strikes(ladder, 54000, 2) == [54000, 55000, 55100]


# ---------------------------------------------------------------- data quality
def test_clean_chain_has_no_violations_and_uses_broker_greeks(monkeypatch):
    g = [{"strikePrice": f"{k}.000000", "optionType": t, "delta": "0.5", "gamma": "0.0004", "theta": "-30", "vega": "30", "impliedVolatility": "13.5"}
         for k in (55400, 55500, 55600) for t in ("CE", "PE")]
    ch = run_chain(monkeypatch, all_quotes(), OPEN, g)
    assert not ch.violations and not ch.stale and set(ch.rows) == {55400, 55500, 55600}
    assert ch.rows[55500]["CE"].greeks_source == "broker" and ch.rows[55500]["CE"].iv == 13.5 and ch.vix == 14.5


def test_missing_greeks_fall_back_to_a_flagged_black_scholes_calculation(monkeypatch):
    ch = run_chain(monkeypatch, [quote(f"{k}{t}", ltp=300.0, bid=299.0, ask=301.0) for k in (55400, 55500, 55600) for t in ("CE", "PE")], OPEN)
    leg = ch.rows[55500]["CE"]
    assert leg.greeks_source == "calc" and leg.iv and any(f.startswith("GREEKS_CALC(r=0.065,q=0") for f in leg.flags)
    assert any("returned no Greeks" in w for w in ch.warnings)


def test_zero_negative_price_crossed_book_and_missing_strike_fail_loudly(monkeypatch):
    qs = all_quotes()
    qs[0] = quote("55400CE", ltp=0.0)                       # zero price
    qs[1] = quote("55400PE", bid=105.0, ask=101.0)           # bid > ask
    qs = [q for q in qs if q["symbolToken"] != "55600PE"]    # a listed strike that was not returned
    ch = run_chain(monkeypatch, qs, OPEN)
    assert any(v.startswith("BAD_PRICE") and "55400CE" in v for v in ch.violations)
    assert any(v.startswith("CROSSED") and "55400PE" in v for v in ch.violations)
    assert any(v.startswith("MISSING") and "55600PE" in v for v in ch.violations)
    assert not oc.liquid(ch.rows[55400]["CE"]) and not oc.liquid(ch.rows[55400]["PE"]) and not oc.liquid(ch.rows[55600]["PE"])


def test_stale_ticks_are_rejected_only_while_the_market_is_open(monkeypatch):
    qs = all_quotes(feed="19-Oct-2026 10:59:50")            # 13 s old at 11:00:03
    open_ch = run_chain(monkeypatch, qs, OPEN, max_age=5.0)
    assert len(open_ch.stale) == 6 and "STALE" in open_ch.rows[55500]["CE"].flags and not oc.liquid(open_ch.rows[55500]["CE"])
    closed = run_chain(monkeypatch, qs, CLOSED, max_age=5.0)
    assert closed.stale == [] and any("Market is closed" in w for w in closed.warnings)


def test_illiquid_legs_are_flagged_not_forward_filled_and_excluded_from_selection(monkeypatch):
    qs = all_quotes()
    qs[2] = quote("55500CE", oi=10, vol=1)                  # thin
    qs[3] = quote("55500PE", bid=50.0, ask=150.0)            # wide spread
    ch = run_chain(monkeypatch, qs, OPEN, min_oi=1000, min_volume=100, max_spread_pct=10.0)
    ce, pe = ch.rows[55500]["CE"], ch.rows[55500]["PE"]
    assert any(f.startswith("ILLIQ") and "low OI" in f for f in ce.flags) and any("spread" in f for f in pe.flags)
    assert ce.ltp == 100.0                                    # the real last price, never replaced by a fill
    assert not oc.liquid(ce) and not oc.liquid(pe) and oc.liquid(ch.rows[55400]["CE"])


def test_stale_clock_open_but_no_fresh_ticks_is_called_out(monkeypatch):
    ch = run_chain(monkeypatch, all_quotes(feed="19-Oct-2026 09:00:00"), OPEN)
    assert any("holiday or feed problem" in w for w in ch.warnings)


# ---------------------------------------------------------------- strike selection helpers
def test_strike_helpers_pick_only_liquid_strikes(monkeypatch):
    g = [{"strikePrice": f"{k}.000000", "optionType": t, "delta": str(d), "gamma": "0.0004", "theta": "-30", "vega": "30", "impliedVolatility": "13.5"}
         for k, d in ((55400, 0.6), (55500, 0.5), (55600, 0.3)) for t in ("CE", "PE")]
    ch = run_chain(monkeypatch, [quote(f"{k}{t}", ltp=p, bid=p - 1, ask=p + 1) for k, p in ((55400, 600), (55500, 480), (55600, 110)) for t in ("CE", "PE")], OPEN, g)
    assert oc.atm_strike(ch) == 55500
    assert oc.otm_by_points(ch, "CE", 100) == 55600 and oc.otm_by_points(ch, "PE", 100) == 55400
    assert oc.by_delta(ch, "CE", 0.30) == 55600 and oc.nearest_premium(ch, "CE", 100) == 55600
    ch.rows[55600]["CE"].flags.append("ILLIQ(low OI)")       # an illiquid strike must never be selected
    assert oc.by_delta(ch, "CE", 0.30) != 55600 and oc.nearest_premium(ch, "CE", 100) != 55600
