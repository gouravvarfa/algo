"""Real-money hardening, 11 Nov 2026, over HTTP against tests/security/fake_angel.py: the situations a calm test never shows.
  * a cancel Angel One accepts while the order had ALREADY filled at the exchange (the old code then sent a MARKET entry: two lots; or an exit after a
    stop that had filled: a naked opposite position);
  * Angel One's session expiring between two calls (one session per API key and client: another login ends the earlier one). The old code took
    "Invalid Token" on a stop order as a refusal and exited the position;
  * exits refused again and again, a position Angel One still shows after the app closed it, a busy funds call, and Angel One's limit of about one
    order-book / positions read a second with several instruments on.
Made-up data only."""
import asyncio
import datetime as dt

import pytest

from app.broker import account, orders, store, tb_real
from app.providers import angelone_client as ao
from tests.security.helpers import make_user
from tests.security.test_real_e2e import CONTRACT, MON, PAPER_BUY, async_test, e2e, places, secs  # noqa: F401  (e2e is the fixture)

BOOK, POSITIONS, RMS = "/rest/secure/angelbroking/order/v1/getOrderBook", "/rest/secure/angelbroking/order/v1/getPosition", "/rest/secure/angelbroking/user/v1/getRMS"


def entry_orders(fa):
    return [b for b in places(fa) if b["transactiontype"] == "BUY" and b["ordertype"] in ("LIMIT", "MARKET")]


def exit_orders(fa):
    return [b for b in places(fa) if b["transactiontype"] == "SELL" and b["ordertype"] in ("LIMIT", "MARKET")]


async def open_long(e2e, t0=0):
    fa, rt = e2e.fa, e2e.rt
    e2e.sync(None, secs(t0))
    rt.handle([e2e.ev("ENTRY", "BUY", now=secs(t0))], {}, secs(t0))
    await rt.tick(secs(t0))
    fa.fill(fa.open_orders()[0]["orderid"], 50002.5)
    e2e.sync(PAPER_BUY, secs(t0 + 5))
    await rt.tick(secs(t0 + 5))
    assert len(fa.open_orders("STOPLOSS_LIMIT")) == 1 and fa.net(CONTRACT) == 30


# ---- a cancel that comes too late ------------------------------------------------------------------------------------------------------------
@async_test
async def test_an_entry_that_fills_while_it_is_cancelled_is_never_followed_by_a_second_entry(e2e):
    fa, rt = e2e.fa, e2e.rt
    e2e.sync(None)
    rt.handle([e2e.ev("ENTRY", "BUY")], {}, MON)
    await rt.tick(MON)
    assert len(entry_orders(fa)) == 1
    e2e.sync(PAPER_BUY, secs(5))
    fa.late_cancel = True                                  # not filled in 3 s: the app cancels, but the exchange had filled it in that instant
    await rt.tick(secs(5))
    await rt.tick(secs(10))
    assert len(entry_orders(fa)) == 1                      # no MARKET order after a "cancel" that had filled (that made two lots before)
    assert fa.net(CONTRACT) == 30 and e2e.state()["position"]["qty"] == 30
    assert len(fa.open_orders("STOPLOSS_LIMIT")) == 1 and e2e.state()["halted"] is None


@async_test
async def test_a_stop_that_fills_while_it_is_cancelled_is_never_followed_by_an_exit_order(e2e):
    fa, rt = e2e.fa, e2e.rt
    await open_long(e2e)
    e2e.sync(None, secs(60))
    rt.handle([e2e.ev("EXIT", "BUY", now=secs(60), reason="GAP_EXIT_15M", exit_price=49400.0)], {}, secs(60))
    fa.late_cancel = True                                  # the app takes the stop away to exit at once, but the stop had just filled
    await rt.tick(secs(60))
    await rt.tick(secs(65))
    assert exit_orders(fa) == []                           # an exit order now would have opened a naked short
    assert fa.net(CONTRACT) == 0 and e2e.state()["position"] is None and e2e.state()["halted"] is None


@async_test
async def test_a_confirmed_cancel_is_followed_by_the_market_entry_at_once(e2e):
    fa, rt = e2e.fa, e2e.rt
    e2e.sync(None)
    rt.handle([e2e.ev("ENTRY", "BUY")], {}, MON)
    await rt.tick(MON)
    await rt.tick(secs(5))                                 # the LIMIT is cancelled, the order book confirms it, then the MARKET goes in the same round
    assert [b["ordertype"] for b in entry_orders(fa)] == ["LIMIT", "MARKET"]
    st = {o["state"] for o in e2e.state()["orders"]}
    assert "cancelling" not in st and "cancelled" in st


# ---- an expired session is not a refusal -------------------------------------------------------------------------------------------------------
@async_test
async def test_an_expired_session_never_turns_the_stop_into_an_exit(e2e):
    fa, rt = e2e.fa, e2e.rt
    e2e.sync(None)
    rt.handle([e2e.ev("ENTRY", "BUY")], {}, MON)
    await rt.tick(MON)
    fa.fill(fa.open_orders()[0]["orderid"], 50002.5)
    e2e.sync(PAPER_BUY, secs(5))
    fa.expire_on_place = True                              # the stop order finds the session ended by another login: Angel One received nothing
    await rt.tick(secs(5))
    a = e2e.state()
    assert a["position"] and a["exit_since"] is None and not [o for o in a["orders"] if o["state"] == "rejected"]
    await rt.tick(secs(10))                                # one fresh login, then the stop goes out
    assert len(fa.open_orders("STOPLOSS_LIMIT")) == 1 and fa.net(CONTRACT) == 30
    assert exit_orders(fa) == [] and fa.logins == 2


@async_test
async def test_an_expired_session_does_not_drop_or_skip_the_entry(e2e):
    fa, rt = e2e.fa, e2e.rt
    e2e.sync(None)
    rt.handle([e2e.ev("ENTRY", "BUY")], {}, MON)
    fa.expire_on_place = True
    await rt.tick(MON)
    a = e2e.state()
    assert places(fa) == [] and a["pending_entry"] and not a["missed"]
    await rt.tick(secs(5))
    assert [b["ordertype"] for b in entry_orders(fa)] == ["LIMIT"]          # still the first try: the lost one was no attempt


# ---- exits Angel One keeps refusing; a position it still shows -------------------------------------------------------------------------------
@async_test
async def test_exits_angel_one_keeps_refusing_halt_after_three_instead_of_an_endless_stream(e2e):
    fa, rt = e2e.fa, e2e.rt
    await open_long(e2e)
    e2e.sync(None, secs(60))
    rt.handle([e2e.ev("EXIT", "BUY", now=secs(60), reason="GAP_EXIT_15M", exit_price=49400.0)], {}, secs(60))
    fa.reject_all = "RMS:Rule: Check circuit limit including square off order exceeds"
    for t in range(60, 90, 5):
        await rt.tick(secs(t))
    a = e2e.state()
    assert len(exit_orders(fa)) == 3 and a["halted"] and "refused 3 exit orders" in a["halted"] and "still OPEN" in a["halted"]


@async_test
async def test_angel_one_still_showing_a_position_after_the_app_closed_it_halts(e2e):
    fa, rt = e2e.fa, e2e.rt
    await open_long(e2e)
    fa.fill(fa.open_orders("STOPLOSS_LIMIT")[0]["orderid"], 49499.95)       # the stop closes the long ...
    fa.orders["9999"] = {"orderid": "9999", "tradingsymbol": CONTRACT, "transactiontype": "SELL", "quantity": "30", "status": "complete", "ordertype": "LIMIT",
                         "averageprice": "49490", "filledshares": "30", "text": ""}   # ... and one more sell filled (an exit that went twice)
    e2e.sync(None, secs(30))
    await rt.tick(secs(30))
    a = e2e.state()
    assert a["position"] is None and a["halted"] is None    # the positions report may lag a fill: a few seconds' grace
    await rt.tick(secs(55))
    a = e2e.state()
    assert a["halted"] and "still shows a net quantity of -30" in a["halted"]   # the old code took the -30 as the person's own position, silently


# ---- busy Angel One --------------------------------------------------------------------------------------------------------------------------
@async_test
async def test_a_busy_funds_read_does_not_drop_the_entry(e2e):
    fa, rt = e2e.fa, e2e.rt
    e2e.sync(None)
    rt.handle([e2e.ev("ENTRY", "BUY")], {}, MON)
    fa.fail_rms = 1
    await rt.tick(MON)
    a = e2e.state()
    assert places(fa) == [] and a["pending_entry"] and not a["missed"]
    await rt.tick(secs(5))
    assert [b["ordertype"] for b in entry_orders(fa)] == ["LIMIT"]


@async_test
async def test_funds_that_cannot_be_read_for_two_minutes_drop_the_entry(e2e):
    fa, rt = e2e.fa, e2e.rt
    e2e.sync(None)
    rt.handle([e2e.ev("ENTRY", "BUY")], {}, MON)
    fa.fail_rms = 1000
    for t in (0, 60, 125):
        await rt.tick(secs(t))
    a = e2e.state()
    assert places(fa) == [] and a["pending_entry"] is None and a["missed"][-1]["reason"] == tb_real.GUARD_FUNDS


@async_test
async def test_one_order_book_and_one_positions_read_per_round_for_all_instruments(e2e):
    fa, rt = e2e.fa, e2e.rt
    iid2, sym2, contract2 = "i-nifty", "NIFTY", "NIFTY25NOV26FUT"
    fa.lot_sizes["777"] = 65
    store.instrument_set(e2e.db, e2e.uid, iid2, sym2, True, "e2etrader")
    e2e.db.execute("UPDATE user_instrument_real SET changed_at=changed_at-120 WHERE instrument_id=?", (iid2,))
    e2e.db.commit()
    inst2 = {"trading_symbol": contract2, "token": "777", "exch_seg": "NFO", "lot_size": 65, "lots": 1, "quantity": 65, "session": "NSE", "tick_size": 0.1}
    paper2 = {"direction": "BUY", "entry_price": 25000.0, "active_stop": 24800.0}
    await open_long(e2e)
    rt.sync(iid2, e2e.uid, sym2, {"instrument": inst2, "ltp": 25001.0, "position": None}, secs(10))
    ev2 = {**e2e.ev("ENTRY", "BUY", now=secs(10)), "instrument_id": iid2, "symbol": sym2, "entry_price": 25000.0, "quantity": 65, "active_stop": 24800.0, "contract": contract2}
    rt.handle([ev2], {}, secs(10))
    await rt.tick(secs(10))
    fa.fill([o for o in fa.open_orders() if o["tradingsymbol"] == contract2][0]["orderid"], 25001.0)
    rt.sync(iid2, e2e.uid, sym2, {"instrument": inst2, "ltp": 25001.0, "position": paper2}, secs(15))
    await rt.tick(secs(15))
    assert fa.net(contract2) == 65 and len(fa.open_orders("STOPLOSS_LIMIT")) == 2
    fa.hits.clear()
    await rt.tick(secs(20))                                # two instruments with open positions: one round
    assert fa.hits.get(BOOK) == 1 and fa.hits.get(POSITIONS) == 1


# ---- one session per person and day ----------------------------------------------------------------------------------------------------------
def _person(db):
    user = make_user(db, "sessuser", "user")
    store.save(db, user.id, "A123456", "KEY12345", "1234", "JBSWY3DPEHPK3PXP", user.id)
    db.execute("UPDATE user_broker SET status='connected' WHERE user_id=?", (user.id,))
    db.commit()
    return user


def test_one_session_per_person_and_day_renewed_only_when_angel_one_says_it_ended(db, monkeypatch):
    user = _person(db)
    made = []

    async def fake_login(**k):
        made.append(1)
        return ao.AngelOneSession(k["api_key"], f"jwt{len(made)}", "", "", k["client_code"])

    monkeypatch.setattr(ao, "login", fake_login)
    orders._sessions.clear()
    s1 = asyncio.run(orders._session(db, user.id))
    assert asyncio.run(orders._session(db, user.id)) is s1 and len(made) == 1                     # all day: no login every 10 minutes
    assert not orders.drop_if_dead(user.id, ao.AngelOneApiError("Angel One returned a non-JSON response (HTTP 403).", "data", retryable=True))
    assert not orders.drop_if_dead(user.id, ao.AngelOneApiError("RMS:Margin Exceeds", "data"))
    assert asyncio.run(orders._session(db, user.id)) is s1                                         # a rate-limit page or a refusal keeps it
    assert orders.drop_if_dead(user.id, ao.AngelOneApiError("Invalid Token (AG8001)", "data"))
    s2 = asyncio.run(orders._session(db, user.id))
    assert s2 is not s1 and len(made) == 2
    monkeypatch.setattr(orders, "now_ist", lambda: dt.datetime.now(orders.IST) + dt.timedelta(days=1))
    assert asyncio.run(orders._session(db, user.id)) is not s2 and len(made) == 3                   # a new day: a new login (sessions end at midnight)
    orders._sessions.clear()


def test_the_session_dead_answers_are_recognised_and_nothing_else():
    dead = ["Invalid Token (AG8001)", "Token Expired (AG8002)", "Invalid Session or Session Expired Please Re-Login (AB1010)", "Token missing"]
    alive = ["RMS:Margin Exceeds", "Quantity is invalid. It should be in multiples of lot size", "Something Went Wrong, Please Try After Sometime (AB1004)"]
    assert all(ao.session_dead(ao.AngelOneApiError(m, "data")) for m in dead)
    assert not any(ao.session_dead(ao.AngelOneApiError(m, "data")) for m in alive)
    assert not ao.session_dead(ao.AngelOneApiError("Could not reach Angel One", "network", retryable=True))


def test_a_rate_limited_login_does_not_take_the_account_off_real(db, monkeypatch):
    user = _person(db)

    async def busy(**k):
        raise ao.AngelOneApiError("Angel One returned a non-JSON response (HTTP 403).", "auth", retryable=True)

    monkeypatch.setattr(ao, "login", busy)
    with pytest.raises(store.BrokerError) as e:
        asyncio.run(account._login_or_record(db, user.id))
    assert e.value.status == 502 and store.row(db, user.id)["status"] == "connected"             # 'error' would have put the account on paper

    async def wrong(**k):
        raise ao.AngelOneApiError("Invalid clientcode or password", "auth")

    monkeypatch.setattr(ao, "login", wrong)
    with pytest.raises(store.BrokerError):
        asyncio.run(account._login_or_record(db, user.id))
    assert store.row(db, user.id)["status"] == "error"                                           # wrong details still do
