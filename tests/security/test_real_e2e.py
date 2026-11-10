"""End to end over HTTP: the app's real Angel One code (login with TOTP, headers, order JSON, answer parsing, timeouts) against tests/security/fake_angel.py.
Only the network is replaced; every line of app code between the strategy's signal and the HTTP request runs as it would for real. Made-up data."""
import asyncio
import datetime as dt
import functools

import httpx
import pytest

from app.broker import orders, store, tb_real
from app.ops import flags
from tests.security.fake_angel import FakeAngel
from tests.security.helpers import make_user

IID, SYM, CONTRACT = "i-bank", "BANKNIFTY", "BANKNIFTY25NOV26FUT"
MON = dt.datetime(2026, 11, 9, 10, 0, 0)
INST = {"trading_symbol": CONTRACT, "token": "555", "exch_seg": "NFO", "lot_size": 30, "lots": 1, "quantity": 30, "session": "NSE", "tick_size": 0.05}
PAPER_BUY = {"direction": "BUY", "entry_price": 50000.0, "active_stop": 49500.0}


def async_test(fn):
    @functools.wraps(fn)
    def run(*a, **k):
        return asyncio.run(fn(*a, **k))
    return run


def secs(n):
    return MON + dt.timedelta(seconds=n)


@pytest.fixture
def e2e(db, monkeypatch):
    fa = FakeAngel()
    real_client = httpx.AsyncClient

    class Routed(real_client):
        def __init__(self, *a, **k):
            k["transport"] = fa.transport()
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", Routed)
    monkeypatch.setattr(store, "MIN_GAP_S", 0.0)
    orders._sessions.clear()
    flags._cache = None
    user = make_user(db, "e2etrader", "user")
    flags.set_flag("real.enabled", True)
    store.save(db, user.id, "A123456", "KEY12345", "1234", "JBSWY3DPEHPK3PXP", user.id)
    db.execute("UPDATE user_broker SET status='connected' WHERE user_id=?", (user.id,))
    db.commit()
    store.grant(db, user.id, True, 1, None, None, "admin")
    store.set_mode(db, user.id, "real", "e2etrader")
    store.instrument_set(db, user.id, IID, SYM, True, "e2etrader")
    db.execute("UPDATE user_instrument_real SET changed_at=changed_at-120")
    db.commit()
    rt = tb_real.RealTB()                                                     # the production broker: login -> AngelBroker -> HTTP

    def sync(position, now=MON):
        rt.sync(IID, user.id, SYM, {"instrument": dict(INST), "ltp": 50001.0, "position": position}, now)

    def ev(kind, direction, now=MON, **kv):
        d = {"at": (dt.datetime.now(tb_real.IST) + dt.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M"), "owner": user.id, "instrument_id": IID, "symbol": SYM, "event": kind,
             "direction": direction, "entry_date": now.strftime("%Y-%m-%d %H:%M"), "exit_date": now.strftime("%Y-%m-%d %H:%M"), "entry_price": 50000.0, "quantity": 30,
             "active_stop": 49500.0, "contract": CONTRACT, "backfill": False}
        d.update(kv)
        return d

    yield type("E", (), {"fa": fa, "rt": rt, "uid": user.id, "db": db, "sync": staticmethod(sync), "ev": staticmethod(ev), "state": lambda self=None: rt.state[(user.id, IID)]})()
    orders._sessions.clear()
    flags._cache = None


def places(fa, ordertype=None):
    return [b for k, b in fa.requests if k == "place" and (ordertype is None or b["ordertype"] == ordertype)]


@async_test
async def test_a_whole_trade_over_http_entry_stop_trail_reversal(e2e):
    fa, rt = e2e.fa, e2e.rt
    e2e.sync(None)
    rt.handle([e2e.ev("ENTRY", "BUY")], {}, MON)
    await rt.tick(MON)
    assert [k for k, _ in fa.requests][:1] == ["login"]                                                   # logged in with the person's own TOTP
    [entry] = places(fa)
    assert entry == {"variety": "NORMAL", "tradingsymbol": CONTRACT, "symboltoken": "555", "transactiontype": "BUY", "exchange": "NFO", "ordertype": "LIMIT",
                     "producttype": "CARRYFORWARD", "duration": "DAY", "quantity": "30", "ordertag": "ALGOTB", "price": "50003.00", "squareoff": "0", "stoploss": "0"}
    fa.fill(fa.open_orders()[0]["orderid"], 50002.5)
    e2e.sync(PAPER_BUY)
    await rt.tick(secs(5))
    [stop] = places(fa, "STOPLOSS_LIMIT")
    assert stop["variety"] == "STOPLOSS" and stop["transactiontype"] == "SELL" and stop["triggerprice"] == "49499.95" and stop["price"] == "49489.95"
    assert fa.net(CONTRACT) == 30 and e2e.state()["position"]["entry_price"] == 50002.5
    e2e.sync({"direction": "BUY", "entry_price": 50000.0, "active_stop": 50002.5}, secs(10))              # break-even: the stop moves
    await rt.tick(secs(10))
    [mod] = [b for k, b in fa.requests if k == "modify"]
    assert mod["triggerprice"] == "50002.45" and "transactiontype" not in mod and mod["orderid"] == fa.open_orders("STOPLOSS_LIMIT")[0]["orderid"]
    e2e.sync(None, secs(60))                                                                              # the strategy reverses
    rt.handle([e2e.ev("EXIT", "BUY", now=secs(60), reason="TRAILING_STOP_REVERSAL", exit_price=50002.5),
               e2e.ev("ENTRY", "SELL", now=secs(60), entry_price=50000.0, active_stop=50500.0)], {}, secs(60))
    await rt.tick(secs(60))
    assert len(places(fa, "LIMIT")) == 1                                                                  # no SELL entry while the long is open
    fa.fill(fa.open_orders("STOPLOSS_LIMIT")[0]["orderid"], 50002.0)
    await rt.tick(secs(65))
    sells = [b for b in places(fa, "LIMIT") if b["transactiontype"] == "SELL"]
    assert len(sells) == 1 and fa.net(CONTRACT) == 0                                                      # exit first, then the entry straight away
    assert e2e.state()["halted"] is None


@async_test
async def test_an_unfilled_entry_is_really_cancelled_at_angel_one(e2e):
    fa, rt = e2e.fa, e2e.rt
    e2e.sync(None)
    rt.handle([e2e.ev("ENTRY", "BUY")], {}, MON)
    await rt.tick(MON)
    oid = fa.open_orders()[0]["orderid"]
    await rt.tick(secs(31))
    assert [b for k, b in fa.requests if k == "cancel"] == [{"variety": "NORMAL", "orderid": oid}] and fa.orders[oid]["status"] == "cancelled"
    await rt.tick(secs(32))
    assert len(places(fa)) == 2                                                                           # attempt 2


@async_test
async def test_a_timeout_on_place_is_never_resent_and_halts(e2e):
    fa, rt = e2e.fa, e2e.rt
    e2e.sync(None)
    fa.timeout_paths.add("/rest/secure/angelbroking/order/v1/placeOrder")
    rt.handle([e2e.ev("ENTRY", "BUY")], {}, MON)
    await rt.tick(MON)
    assert len(places(fa)) == 1 and len(fa.orders) == 1                                                   # it reached Angel One once...
    a = e2e.state()
    assert a["halted"] and "no clear answer" in a["halted"]                                               # ...and the app stops instead of guessing
    for t in (5, 10, 40):
        await rt.tick(secs(t))
    assert len(places(fa)) == 1


@async_test
async def test_an_order_angel_one_refuses_is_recorded_and_not_retried_blindly(e2e):
    fa, rt = e2e.fa, e2e.rt
    e2e.sync(None)
    fa.reject_next = "RMS:Margin Exceeds"
    rt.handle([e2e.ev("ENTRY", "BUY")], {}, MON)
    await rt.tick(MON)
    a = e2e.state()
    assert a["pending_entry"] is None and "Margin Exceeds" in a["missed"][-1]["reason"]
    assert e2e.db.execute("SELECT status, message FROM real_tb_orders").fetchone()["status"] == "rejected"
    await rt.tick(secs(5))
    assert len(places(fa)) == 1


@async_test
async def test_wrong_details_are_refused_by_the_login_and_nothing_is_sent(e2e):
    fa, rt = e2e.fa, e2e.rt
    fa.totp_secret = "GEZDGNBVGY3TQOJQ"                                                                    # Angel One expects another authenticator
    e2e.sync(None)
    rt.handle([e2e.ev("ENTRY", "BUY")], {}, MON)
    await rt.tick(MON)
    assert places(fa) == []
    r = store.row(e2e.db, e2e.uid)
    assert r["status"] == "error" and "totp" in (r["status_message"] or "").lower()
    assert store.effective_mode(r) == "paper"                                                             # a failed login takes the account off real


@async_test
async def test_margin_short_at_angel_one_blocks_the_entry_over_http(e2e):
    fa, rt = e2e.fa, e2e.rt
    fa.margin, fa.cash = 150000.0, 100000.0
    e2e.sync(None)
    rt.handle([e2e.ev("ENTRY", "BUY")], {}, MON)
    await rt.tick(MON)
    assert places(fa) == [] and "margin needed" in e2e.state()["missed"][-1]["reason"]
