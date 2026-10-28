"""Option contracts, the live option chain, the simulator's LIVE chain and the margin calculator on UPSTOX data (faked here: no network), in the same shapes the
Angel One versions give, so the pages need no change. Also: with Angel One as the data source nothing about these endpoints changes."""
import asyncio
import datetime as dt
import time
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.live.ticks import Tick
from app.live.upstox_feed import UpstoxPollFeed
from app.options import api, master, upstox_options as upx
from app.providers import upstox_client
from app.providers.upstox_provider import UpstoxNotConnected, UpstoxProvider

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
BANK = "NSE_INDEX|Nifty Bank"
EXPIRY, LATER = "2099-11-23", "2099-12-29"


def contract(key, exp, strike, typ, underlying="BANKNIFTY", lot=30):
    return {"instrument_key": key, "expiry": exp, "strike_price": strike, "instrument_type": typ, "lot_size": lot, "tick_size": 5.0, "underlying_symbol": underlying,
            "trading_symbol": f"{underlying} {int(strike)} {typ}"}


def leg(key, ltp, bid=None, ask=None, vol=5000, oi=50000, iv=15.0, delta=0.5):
    return {"instrument_key": key, "market_data": {"ltp": ltp, "volume": vol, "oi": oi, "bid_price": bid if bid is not None else ltp - 1, "ask_price": ask if ask is not None else ltp + 1},
            "option_greeks": {"iv": iv, "delta": delta, "gamma": 0.0002, "theta": -14.0, "vega": 50.0}}


STRIKES = [54000.0, 54100.0, 54200.0, 54300.0, 54400.0, 54500.0, 54600.0]
CONTRACT_ROWS = ([contract(f"NSE_FO|{int(k)}1", EXPIRY, k, "CE") for k in STRIKES] + [contract(f"NSE_FO|{int(k)}2", EXPIRY, k, "PE") for k in STRIKES]
                 + [contract("NSE_FO|9991", LATER, 54300.0, "CE"), contract("NSE_FO|9992", LATER, 54300.0, "PE"),
                    contract("NSE_FO|8881", EXPIRY, 54300.0, "CE", underlying="NIFTY"), {"instrument_key": "NSE_FO|7", "instrument_type": "FUT", "underlying_symbol": "BANKNIFTY", "expiry": EXPIRY}])


def chain_rows(spot=54310.0):
    rows = []
    for k in STRIKES:
        rows.append({"strike_price": k, "underlying_spot_price": spot, "call_options": leg(f"NSE_FO|{int(k)}1", max(5.0, spot - k + 100), delta=0.5),
                     "put_options": leg(f"NSE_FO|{int(k)}2", max(5.0, k - spot + 100), delta=-0.5)})
    return rows


class Fake:
    def __init__(self):
        self.contracts, self.chain, self.ltp, self.margin, self.calls, self.fail = list(CONTRACT_ROWS), chain_rows(), {"NSE_INDEX|India VIX": 16.25}, None, [], None


@pytest.fixture
def fake(monkeypatch):
    f = Fake()

    async def get_option_contracts(token, key):
        f.calls.append(("contracts", token, key))
        if f.fail:
            raise f.fail
        return f.contracts

    async def get_option_chain(token, key, expiry):
        f.calls.append(("chain", token, key, expiry))
        if f.fail:
            raise f.fail
        return f.chain

    async def get_ltp(token, keys):
        f.calls.append(("ltp", token, tuple(keys)))
        return {k: v for k, v in f.ltp.items() if k in keys}

    async def get_margin(token, instruments):
        f.calls.append(("margin", token, instruments))
        if f.fail:
            raise f.fail
        return f.margin or {"margins": [{"span_margin": 100.0, "exposure_margin": 20.0}, {"span_margin": 0.0, "exposure_margin": 21.5}], "required_margin": 141.5, "final_margin": 120.0}

    monkeypatch.setattr(upstox_client, "get_option_contracts", get_option_contracts)
    monkeypatch.setattr(upstox_client, "get_option_chain", get_option_chain)
    monkeypatch.setattr(upstox_client, "get_ltp", get_ltp)
    monkeypatch.setattr(upstox_client, "get_margin", get_margin)
    monkeypatch.setattr(upx, "_infos", {})
    return f


class Provider(UpstoxProvider):
    """The real provider, with the saved Upstox login replaced by a fixed token."""

    def access_token(self):
        return "tok"


def run(coro):
    return asyncio.run(coro)


def info():
    return run(upx.load_underlying(Provider(), "BANKNIFTY"))


# ---- the contract list -------------------------------------------------------------------------------------------------------------------------------
def test_contracts_become_the_same_unit_the_angel_one_master_gives(fake):
    i = info()
    assert (i.name, i.spot_token, i.vix_token) == ("BANKNIFTY", BANK, "NSE_INDEX|India VIX")
    c = next(x for x in i.contracts if x.token == "NSE_FO|540001")
    assert (c.symbol, c.strike, c.option_type, c.lot_size, c.expiry) == ("BANKNIFTY23NOV9954000CE", 54000.0, "CE", 30, dt.date(2099, 11, 23))
    assert c.tick_size == 0.05                                          # Upstox sends paise
    assert len(i.contracts) == 16 and all(x.name == "BANKNIFTY" and x.option_type in ("CE", "PE") for x in i.contracts)          # the NIFTY row and the future are left out
    assert [x.expiry for x in i.contracts] == sorted(x.expiry for x in i.contracts)
    assert [e.isoformat() for e in master.expiries(i)] == [EXPIRY, LATER]       # the page's own expiry helpers work on it unchanged
    assert master.pick_expiry(i, "nearest").isoformat() == EXPIRY and master.pick_expiry(i, "23NOV2099").isoformat() == EXPIRY


def test_the_contract_list_is_asked_once_and_kept(fake):
    info()
    info()
    assert [c[0] for c in fake.calls].count("contracts") == 1


def test_only_nifty_and_banknifty_are_offered_and_an_empty_list_is_an_error(fake):
    with pytest.raises(ValueError, match="NIFTY or BANKNIFTY"):
        run(upx.load_underlying(Provider(), "FINNIFTY"))
    fake.contracts = [CONTRACT_ROWS[-1]]
    with pytest.raises(ValueError, match="no option contracts"):
        run(upx.load_underlying(Provider(), "BANKNIFTY"))


def test_one_odd_contract_row_does_not_lose_the_list(fake):
    fake.contracts = CONTRACT_ROWS + [{"instrument_type": "CE", "underlying_symbol": "BANKNIFTY"}, {"instrument_type": "CE", "underlying_symbol": "BANKNIFTY", "expiry": "junk", "instrument_key": "x", "strike_price": "a"}]
    assert len(info().contracts) == 16


# ---- the chain ---------------------------------------------------------------------------------------------------------------------------------------
def chain(**kw):
    i = info()
    return run(upx.fetch_chain(Provider(), i, dt.date.fromisoformat(EXPIRY), n_strikes=kw.pop("n", 2), now=kw.pop("now", dt.datetime(2099, 11, 20, 12, 0, tzinfo=IST)), **kw))


def test_the_chain_has_the_atm_window_with_everything_the_page_reads(fake):
    ch = chain()
    assert ch.underlying == "BANKNIFTY" and ch.spot == 54310.0 and ch.vix == 16.25 and ch.expiry == dt.date(2099, 11, 23) and ch.market_open is True
    assert sorted(ch.rows) == [54100.0, 54200.0, 54300.0, 54400.0, 54500.0]                     # ATM 54300 and two strikes each side
    ce = ch.rows[54300.0]["CE"]
    assert (ce.ltp, ce.bid, ce.ask, ce.volume, ce.oi) == (110.0, 109.0, 111.0, 5000, 50000)
    assert (ce.iv, ce.delta, ce.gamma, ce.theta, ce.vega, ce.greeks_source) == (15.0, 0.5, 0.0002, -14.0, 50.0, "broker")
    assert ce.contract.symbol == "BANKNIFTY23NOV9954300CE" and ce.contract.lot_size == 30 and ce.mid == 110.0
    assert ch.violations == [] and ch.stale == [] and ce.flags == []
    assert [c[0] for c in fake.calls if c[0] in ("chain", "ltp")] == ["chain", "ltp"]            # ONE call for the chain, one for the VIX


def test_a_zero_price_and_a_crossed_quote_are_hard_violations(fake):
    fake.chain[3]["call_options"] = leg("NSE_FO|543001", 0.0, bid=5.0, ask=6.0)
    fake.chain[2]["put_options"] = leg("NSE_FO|542002", 50.0, bid=60.0, ask=40.0)
    ch = chain()
    assert any(v.startswith("BAD_PRICE") for v in ch.violations) and any(v.startswith("CROSSED") for v in ch.violations)
    assert "BAD_PRICE" in ch.rows[54300.0]["CE"].flags and "CROSSED" in ch.rows[54200.0]["PE"].flags


def test_thin_strikes_are_flagged_illiquid_but_not_a_violation(fake):
    fake.chain[3]["call_options"] = leg("NSE_FO|543001", 100.0, bid=90.0, ask=130.0, vol=5, oi=10)
    ch = chain()
    flags = ch.rows[54300.0]["CE"].flags
    assert len(flags) == 1 and flags[0].startswith("ILLIQ(") and "low OI" in flags[0] and "low volume" in flags[0] and "spread" in flags[0]
    assert ch.violations == []
    from app.options import chain as oc
    assert oc.liquid(ch.rows[54300.0]["CE"]) is False and oc.liquid(ch.rows[54300.0]["PE"]) is True


def test_no_bid_or_ask_means_no_two_sided_quote(fake):
    fake.chain[3]["call_options"] = leg("NSE_FO|543001", 100.0, bid=0.0, ask=0.0)
    flags = chain().rows[54300.0]["CE"].flags
    assert any("no two-sided quote" in f for f in flags)


def test_when_upstox_has_no_iv_for_a_strike_it_is_calculated_and_flagged(fake):
    fake.chain[3]["call_options"] = leg("NSE_FO|543001", 150.0, iv=0.0, delta=0.0)
    ce = chain().rows[54300.0]["CE"]
    assert ce.greeks_source == "calc" and ce.iv and 0.3 < ce.delta < 0.8 and any(f.startswith("GREEKS_CALC") for f in ce.flags)


def test_a_missing_side_is_reported(fake):
    fake.chain[3]["put_options"] = None
    ch = chain()
    assert "PE" not in ch.rows[54300.0] and any(v.startswith("MISSING_STRIKE") and "54300PE" in v for v in ch.violations)


def test_the_warnings_say_what_cannot_be_checked(fake):
    open_ = chain(now=dt.datetime(2099, 11, 20, 12, 0, tzinfo=IST))
    assert any("does not send a feed time" in w for w in open_.warnings) and open_.stale == []
    closed = chain(now=dt.datetime(2099, 11, 21, 12, 0, tzinfo=IST))                                 # a Saturday
    assert closed.market_open is False and any("Market is closed" in w for w in closed.warnings)
    for r in fake.chain:
        r["call_options"]["option_greeks"] = {}
        r["put_options"]["option_greeks"] = {}
    assert any("returned no Greeks" in w for w in chain().warnings)


def test_the_vix_is_a_nicety_and_never_stops_the_chain(fake):
    fake.ltp = {}
    assert chain().vix is None


def test_an_empty_chain_and_an_unknown_expiry_are_errors_not_made_up_data(fake):
    fake.chain = []
    with pytest.raises(RuntimeError, match="no chain"):
        chain()
    with pytest.raises(ValueError, match="no contracts for expiry"):
        run(upx.fetch_chain(Provider(), info(), dt.date(2099, 1, 1)))


def test_without_a_login_the_chain_says_what_to_do():
    class NoLogin(UpstoxProvider):
        pass

    with pytest.raises(UpstoxNotConnected, match="Upstox"):
        run(upx.load_underlying(NoLogin(), "BANKNIFTY"))


# ---- the margin calculator -----------------------------------------------------------------------------------------------------------------------------
def test_margin_sums_the_legs_and_shows_the_hedge_benefit(fake):
    m = run(upx.margin(Provider(), [{"instrument_key": "NSE_FO|1", "quantity": 30, "side": "SELL"}, {"instrument_key": "NSE_FO|2", "quantity": 30, "side": "SELL"}]))
    assert (m["total"], m["span"], m["exposure"], m["hedge_benefit"]) == (120.0, 100.0, 41.5, 21.5)
    sent = [c for c in fake.calls if c[0] == "margin"][0][2]
    assert sent == [{"instrument_key": "NSE_FO|1", "quantity": 30, "transaction_type": "SELL", "product": "D"}, {"instrument_key": "NSE_FO|2", "quantity": 30, "transaction_type": "SELL", "product": "D"}]


# ---- the HTTP API ----------------------------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def client(fake, monkeypatch):
    monkeypatch.setattr(api, "provider", Provider())
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_the_expiries_come_from_upstox(client):
    r = client.get("/api/options/expiries?symbol=BANKNIFTY")
    assert r.status_code == 200 and r.json() == {"symbol": "BANKNIFTY", "expiries": ["23NOV2099", "29DEC2099"]}
    assert client.get("/api/options/expiries?symbol=FINNIFTY").status_code == 400


def test_the_chain_endpoint_has_the_shape_the_page_reads(client):
    r = client.get("/api/options/chain?symbol=BANKNIFTY&expiry=nearest&strikes=2")
    assert r.status_code == 200
    b = r.json()
    assert set(b) >= {"symbol", "expiry", "spot", "vix", "atm", "lot_size", "market_open", "pcr", "fetched_at", "violations", "stale", "warnings", "helpers", "rows"}
    assert (b["symbol"], b["expiry"], b["spot"], b["vix"], b["atm"], b["lot_size"]) == ("BANKNIFTY", "23NOV2099", 54310.0, 16.25, 54300.0, 30)
    assert len(b["rows"]) == 5 and b["pcr"] == pytest.approx(1.0)
    atm = next(x for x in b["rows"] if x["strike"] == 54300.0)
    assert atm["CE"]["greeks_source"] == "broker" and atm["CE"]["liquid"] is True and atm["PE"]["delta"] == -0.5
    assert set(b["helpers"]["CE"]) == {"otm_100", "delta_30", "premium_100"}


def test_a_failure_is_a_502_with_a_sentence_not_made_up_data(client, fake):
    fake.fail = upstox_client.UpstoxApiError("Invalid date range", "data")
    r = client.get("/api/options/chain?symbol=BANKNIFTY")
    assert r.status_code == 502 and "chain fetch failed" in r.json()["detail"]
    r = client.get("/api/options/expiries?symbol=BANKNIFTY")
    assert r.status_code == 502 and "expiries unavailable" in r.json()["detail"]


def test_margin_for_sold_legs_comes_from_upstox_and_buy_only_needs_no_call(client, fake):
    pos = {"side": "SELL", "opt": "CE", "strike": 54300.0, "expiry": EXPIRY, "lots": 1, "lot_size": 30, "entry": 100.0, "entry_date": "2099-11-01"}
    r = client.post("/api/options/sim/margin", json={"symbol": "BANKNIFTY", "positions": [pos]}).json()
    assert r["margin"] == 120.0 and r["span"] == 100.0 and r["exposure"] == 41.5 and r["hedge_benefit"] == 21.5 and r["source"] == "Upstox margin calculator (today's margin)"
    assert [c for c in fake.calls if c[0] == "margin"][0][2] == [{"instrument_key": "NSE_FO|543001", "quantity": 30, "transaction_type": "SELL", "product": "D"}]
    fake.calls.clear()
    buy = client.post("/api/options/sim/margin", json={"symbol": "BANKNIFTY", "positions": [{**pos, "side": "BUY"}]}).json()
    assert buy["margin"] == 3000.0 and "buy-only" in buy["source"] and not [c for c in fake.calls if c[0] == "margin"]
    gone = client.post("/api/options/sim/margin", json={"symbol": "BANKNIFTY", "positions": [{**pos, "strike": 12345.0}]}).json()
    assert gone["margin"] is None and gone["unlisted"] and "Upstox" in gone["note"]
    fake.fail = upstox_client.UpstoxApiError("x", "network")
    down = client.post("/api/options/sim/margin", json={"symbol": "BANKNIFTY", "positions": [pos]}).json()
    assert down["margin"] is None and "unavailable" in down["note"]


# ---- the simulator's LIVE chain through the shared Upstox feed -------------------------------------------------------------------------------------------
class Engine:
    """The part of the live engine the LIVE endpoints use, with the Upstox feed."""

    def __init__(self):
        self.provider = Provider()
        self.feed = UpstoxPollFeed(self.provider)
        self.index_tokens = UpstoxProvider.index_tokens

    async def index_quote(self, name):
        return {"symbol": name, "price": 54310.0, "prev_close": 54200.0, "change": 110.0, "pct": 0.2, "live": True, "age_s": 1.0}


@pytest.fixture
def live(client, fake, monkeypatch):
    e = Engine()
    monkeypatch.setattr(api, "live_engine", e)
    monkeypatch.setattr(api, "_LIVE_SUBS", set())
    monkeypatch.setattr(api, "_prev_oi", lambda sym, day, expiry: ({}, None, None))
    monkeypatch.setattr(api, "_idb", lambda: __import__("contextlib").nullcontext(object()))
    monkeypatch.setattr(api.intraday, "chain_at", lambda *a, **k: {})
    return e


def test_the_live_chain_subscribes_upstox_keys_never_angel_tokens(client, live):
    r = client.get("/api/options/live/chain?symbol=BANKNIFTY")
    assert r.status_code == 200
    keys = live.feed.keys()
    assert BANK in keys and all("|" in k for k in keys) and "99926009" not in keys
    assert len(keys) > 10 and r.json()["live_legs"] == 0                   # subscribed now; the first prices arrive with the next poll


def test_once_the_feed_has_prices_the_live_chain_shows_them(client, live):
    client.get("/api/options/live/chain?symbol=BANKNIFTY")
    now = dt.datetime.now(IST)
    for k in live.feed.keys():
        live.feed.latest[k] = Tick(k, 1, 111.5, now, time.time())
    live.feed.connected = True
    b = client.get("/api/options/live/chain?symbol=BANKNIFTY").json()
    atm = next(r for r in b["rows"] if r["strike"] == b["atm"])
    assert b["live_legs"] > 0 and atm["CE"]["ltp"] == 111.5 and atm["CE"]["src"] == "live" and b["feed_connected"] is True
    assert b["straddle"] == 223.0 and b["lot_size"] == 30 and b["expiry"] == EXPIRY


def test_live_mark_values_an_open_leg_at_its_upstox_price(client, live):
    pos = {"side": "SELL", "opt": "CE", "strike": 54300.0, "expiry": EXPIRY, "lots": 1, "lot_size": 30, "entry": 120.0, "entry_date": "2099-11-01"}
    first = client.post("/api/options/live/mark", json={"symbol": "BANKNIFTY", "positions": [pos]}).json()
    assert first["positions"][0]["ltp"] is None and "waiting" in first["positions"][0]["note"]
    live.feed.latest["NSE_FO|543001"] = Tick("NSE_FO|543001", 1, 100.0, dt.datetime.now(IST), time.time())
    second = client.post("/api/options/live/mark", json={"symbol": "BANKNIFTY", "positions": [pos]}).json()
    assert second["positions"][0]["ltp"] == 100.0 and second["positions"][0]["pnl"] == 600.0               # sold at 120, now 100: +20 x 30


def test_the_live_endpoints_say_so_when_upstox_cannot_give_contracts(client, live, fake):
    fake.fail = upstox_client.UpstoxApiError("The Upstox login has ended", "auth")
    r = client.get("/api/options/live/chain?symbol=BANKNIFTY")
    assert r.status_code in (400, 502)


# ---- Angel One as the data source: untouched --------------------------------------------------------------------------------------------------------------
def test_with_angel_one_the_endpoints_still_use_its_master_and_session(monkeypatch):
    seen = {}

    class AngelProvider:
        async def get_session(self):
            seen["session"] = True
            return object()

    fake_info = master.UnderlyingInfo("BANKNIFTY", "99926009", "Nifty Bank", None, [master.OptionContract("BANKNIFTY23NOV9954300CE", "1", "BANKNIFTY", 54300.0, dt.date(2099, 11, 23), "CE", 30, 0.05)], "t")
    monkeypatch.setattr(master, "load_underlying", lambda name: fake_info)
    monkeypatch.setattr(api, "provider", AngelProvider())
    assert api._upstox() is False
    app = FastAPI()
    app.include_router(api.router)
    r = TestClient(app).get("/api/options/expiries?symbol=BANKNIFTY")
    assert r.json() == {"symbol": "BANKNIFTY", "expiries": ["23NOV2099"]}
