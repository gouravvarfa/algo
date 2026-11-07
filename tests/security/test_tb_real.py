"""Top-Bottom real orders: the whole lifecycle against a fake Angel One (entry, stop, reversal exit-then-entry, retries, guards, halts, switching off, restart).
Everything is made up and no broker is reached. The fake keeps its own order book and positions, and fills only when a test says so."""
import asyncio
import datetime as dt
import functools
import json

import pytest

from app.broker import angel_orders, store, tb_real
from app.ops import flags
from app.providers import angelone_client as ao
from tests.security.helpers import make_user

def async_test(fn):
    """The suite has no pytest-asyncio: run each async test in its own event loop."""
    @functools.wraps(fn)
    def run(*a, **k):
        return asyncio.run(fn(*a, **k))
    return run


BODY = {"client_code": "A123456", "api_key": "KEY12345", "pin": "1234", "totp_secret": "JBSWY3DPEHPK3PXP"}
IID, SYM, CONTRACT = "i-bank", "BANKNIFTY", "BANKNIFTY25NOV26FUT"
MON = dt.datetime(2026, 11, 9, 10, 0, 0)                      # a Monday, market open
INST = {"trading_symbol": CONTRACT, "token": "555", "exch_seg": "NFO", "lot_size": 30, "lots": 1, "quantity": 30, "session": "NSE"}


class FakeBroker:
    def __init__(self):
        self.orders, self.n = {}, 0
        self.ltp_value, self.cash, self.pnl, self.margin_need = 50001.0, 500000.0, 0.0, 100000.0
        self.place_error = None
        self.calls = []

    async def place(self, payload):
        self.calls.append(("place", payload))
        if self.place_error:
            raise self.place_error
        self.n += 1
        oid = f"B{self.n}"
        stop = payload["ordertype"] == "STOPLOSS_LIMIT"
        self.orders[oid] = {"orderid": oid, "status": "trigger pending" if stop else "open", "averageprice": "0", "text": "", "p": dict(payload)}
        return oid

    async def modify(self, oid, payload):
        self.calls.append(("modify", oid, payload))
        self.orders[oid]["p"].update(payload)

    async def cancel(self, oid):
        self.calls.append(("cancel", oid))
        self.orders[oid]["status"] = "cancelled"

    async def book(self):
        return [{k: v for k, v in o.items() if k != "p"} for o in self.orders.values()]

    async def positions(self):
        net = {}
        for o in self.orders.values():
            if o["status"] == "complete":
                q = int(o["p"]["quantity"]) * (1 if o["p"]["transactiontype"] == "BUY" else -1)
                net[o["p"]["tradingsymbol"]] = net.get(o["p"]["tradingsymbol"], 0) + q
        return [{"tradingsymbol": k, "netqty": str(v)} for k, v in net.items()]

    async def funds(self):
        return {"availablecash": str(self.cash), "m2mrealized": str(self.pnl), "m2munrealized": "0"}

    async def ltp(self, exch, sym, token):
        return self.ltp_value

    async def margin(self, basket):
        return {"totalMarginRequired": self.margin_need}

    def fill(self, oid, avg):
        self.orders[oid]["status"], self.orders[oid]["averageprice"] = "complete", str(avg)

    def last(self, role):
        rows = [(i, o) for i, o in self.orders.items() if (role == "stop") == (o["p"]["ordertype"] == "STOPLOSS_LIMIT") and o["status"] in ("open", "trigger pending")]
        return rows[-1] if rows else (None, None)

    def n_open(self):
        return sum(o["status"] in ("open", "trigger pending") for o in self.orders.values())


@pytest.fixture
def env(db, monkeypatch):
    flags._cache = None
    monkeypatch.setattr(store, "MIN_GAP_S", 0.0)
    fb = FakeBroker()

    async def broker_for(c, uid):
        return fb

    rt = tb_real.RealTB(broker_for=broker_for)
    user = make_user(db, "tbtrader", "user")
    flags.set_flag("real.enabled", True)
    store.save(db, user.id, **{k: BODY[k] for k in ("client_code", "api_key", "pin")}, totp_secret=BODY["totp_secret"], actor_id=user.id)
    db.execute("UPDATE user_broker SET status='connected' WHERE user_id=?", (user.id,))
    db.commit()
    store.grant(db, user.id, True, 1, 5000.0, None, "admin")
    store.set_mode(db, user.id, "real", "tbtrader")
    store.instrument_set(db, user.id, IID, SYM, True, "tbtrader")
    db.execute("UPDATE user_instrument_real SET changed_at=changed_at-60")            # the switch was turned on a minute ago
    db.commit()

    class E:
        pass

    e = E()
    e.fb, e.rt, e.user, e.db, e.uid = fb, rt, user, db, user.id
    e.at = lambda: (dt.datetime.now() + dt.timedelta(seconds=1)).isoformat()

    def snap(position=None, ltp=50001.0):
        return {"instrument": dict(INST), "ltp": ltp, "position": position}

    def sync(position=None, now=MON):
        rt.sync(IID, user.id, SYM, snap(position), now)

    def ev(kind, direction, now=MON, **kv):
        d = {"at": e.at(), "owner": user.id, "instrument_id": IID, "symbol": SYM, "event": kind, "direction": direction, "entry_date": now.strftime("%Y-%m-%d %H:%M"),
             "exit_date": now.strftime("%Y-%m-%d %H:%M"), "entry_price": 50000.0, "quantity": 30, "active_stop": 49500.0, "contract": CONTRACT, "backfill": False}
        d.update(kv)
        return d

    e.sync, e.ev = sync, ev
    e.state = lambda: rt.state[(user.id, IID)]
    yield e
    flags._cache = None


async def go(env, *events, now=MON, position="keep"):
    if position != "keep":
        env.sync(position, now)
    env.rt.handle(list(events), {}, now)
    await env.rt.tick(now)


def secs(n):
    return MON + dt.timedelta(seconds=n)


PAPER_BUY = {"direction": "BUY", "entry_price": 50000.0, "active_stop": 49500.0}


@async_test
async def test_system_point_places_a_limit_entry_then_a_stop_after_the_fill(env):
    env.sync(None)
    await go(env, env.ev("ENTRY", "BUY"))
    oid, o = env.fb.last("entry")
    p = o["p"]
    assert p["ordertype"] == "LIMIT" and p["transactiontype"] == "BUY" and p["quantity"] == "30" and p["price"] == "50003.00" and p["ordertag"] == "ALGOTB" and p["producttype"] == "CARRYFORWARD"
    assert env.fb.n_open() == 1                                              # only the entry, no stop before it fills
    env.fb.fill(oid, 50002.5)
    env.sync(PAPER_BUY, MON)
    await env.rt.tick(secs(5))
    a = env.state()
    assert a["position"]["side"] == "BUY" and a["position"]["entry_price"] == 50002.5 and a["pending_entry"] is None
    sid, s = env.fb.last("stop")
    assert s["p"]["variety"] == "STOPLOSS" and s["p"]["ordertype"] == "STOPLOSS_LIMIT" and s["p"]["transactiontype"] == "SELL" and s["p"]["triggerprice"] == "49500.00" and s["p"]["price"] == "49490.00"
    rows = env.db.execute("SELECT role, status FROM real_tb_orders ORDER BY created_at").fetchall()
    assert [(r["role"], r["status"]) for r in rows] == [("entry", "filled"), ("stop", "placed")]


@async_test
async def test_the_stop_follows_the_strategys_stop_by_modifying_the_resting_order(env):
    env.sync(None)
    await go(env, env.ev("ENTRY", "BUY"))
    env.fb.fill(env.fb.last("entry")[0], 50002.5)
    env.sync(PAPER_BUY, MON)
    await env.rt.tick(secs(5))
    env.sync({"direction": "BUY", "entry_price": 50000.0, "active_stop": 50002.5}, secs(10))
    await env.rt.tick(secs(10))
    mods = [c for c in env.fb.calls if c[0] == "modify"]
    assert len(mods) == 1 and mods[0][2]["triggerprice"] == "50002.50" and mods[0][2]["price"] == "49992.50"
    assert env.state()["stop_target"] == 50002.5


async def open_long(env):
    env.sync(None)
    await go(env, env.ev("ENTRY", "BUY"))
    env.fb.fill(env.fb.last("entry")[0], 50002.5)
    env.sync(PAPER_BUY, MON)
    await env.rt.tick(secs(5))
    return env.fb.last("stop")[0]


@async_test
async def test_a_reversal_exits_first_and_the_new_entry_goes_out_only_after_the_stop_has_filled(env):
    sid = await open_long(env)
    env.sync(None, secs(60))
    await go(env, env.ev("EXIT", "BUY", now=secs(60), reason="TRAILING_STOP_REVERSAL", exit_price=49500.0), env.ev("ENTRY", "SELL", now=secs(60), entry_price=49500.0, active_stop=50000.0), now=secs(60))
    assert env.state()["pending_entry"]["side"] == "SELL" and env.fb.last("entry")[0] is None            # waiting: the stop must close the position first
    assert env.fb.orders[sid]["status"] == "trigger pending"
    env.fb.fill(sid, 49498.0)
    await env.rt.tick(secs(65))
    a = env.state()
    assert a["position"] is None
    _, e = env.fb.last("entry")
    assert e["p"]["transactiontype"] == "SELL" and e["p"]["ordertype"] == "LIMIT"                          # the opposite entry, immediately after the exit
    assert float(e["p"]["price"]) <= 50001.0 - 0.0


@async_test
async def test_a_gap_exit_cancels_the_stop_and_sends_an_exit_limit_at_once(env):
    sid = await open_long(env)
    env.sync(None, secs(60))
    await go(env, env.ev("EXIT", "BUY", now=secs(60), reason="GAP_PROTOCOL_EXIT", exit_price=49000.0), now=secs(60))
    await env.rt.tick(secs(65))
    assert env.fb.orders[sid]["status"] == "cancelled"
    _, x = env.fb.last("entry")
    assert x["p"]["transactiontype"] == "SELL" and x["p"]["ordertype"] == "LIMIT" and x["p"]["quantity"] == "30"


@async_test
async def test_a_stop_that_does_not_fill_in_time_is_replaced_by_an_exit_order(env):
    sid = await open_long(env)
    env.sync(None, secs(60))
    await go(env, env.ev("EXIT", "BUY", now=secs(60), reason="STOP_LOSS_REVERSAL", exit_price=49500.0), now=secs(60))
    assert env.fb.orders[sid]["status"] == "trigger pending"                                              # waiting for our own stop
    await env.rt.tick(secs(80))
    assert env.fb.orders[sid]["status"] == "trigger pending"                                              # still inside the 30 s grace
    await env.rt.tick(secs(95))
    assert env.fb.orders[sid]["status"] == "cancelled" and env.fb.last("entry")[1]["p"]["transactiontype"] == "SELL"


@async_test
async def test_an_unfilled_entry_is_cancelled_and_placed_again_three_times_then_missed(env):
    env.sync(None)
    await go(env, env.ev("ENTRY", "BUY"))
    t = 0
    for attempt in range(1, 4):
        oid, _ = env.fb.last("entry")
        assert oid is not None and env.state()["pending_entry"]["attempts"] == attempt
        t += 31
        await env.rt.tick(secs(t))                                                                        # older than 30 s: cancelled
        assert env.fb.orders[oid]["status"] == "cancelled"
        t += 1
        await env.rt.tick(secs(t))                                                                        # next round: new attempt, or the miss
    a = env.state()
    assert a["pending_entry"] is None and "attempts were not filled" in a["missed"][-1]["reason"] and env.fb.n == 3


@async_test
async def test_a_price_that_ran_away_is_not_chased(env):
    env.sync(None)
    env.fb.ltp_value = 50400.0                                                                           # 400 points past 50000, band is 250
    await go(env, env.ev("ENTRY", "BUY"))
    a = env.state()
    assert env.fb.n == 0 and a["pending_entry"] is None and "not chasing" in a["missed"][-1]["reason"]


@async_test
@pytest.mark.parametrize("what,reason", [("margin", "margin needed"), ("cash", "margin needed"), ("loss", "daily cap"), ("lots", "above your limit")])
async def test_each_entry_guard_blocks_the_entry(env, what, reason):
    env.sync(None)
    if what == "margin":
        env.fb.margin_need = 900000.0
    elif what == "cash":
        env.fb.cash = 10.0
    elif what == "loss":
        env.fb.pnl = -6000.0
    else:
        env.rt.sync(IID, env.uid, SYM, {"instrument": dict(INST, lots=2, quantity=60), "ltp": 50001.0, "position": None}, MON)       # two lots, the limit is one
    env.rt.handle([env.ev("ENTRY", "BUY", quantity=60 if what == "lots" else 30)], {}, MON)
    await env.rt.tick(MON)
    a = env.state()
    assert env.fb.last("entry")[0] is None and a["pending_entry"] is None
    assert reason in a["missed"][-1]["reason"]


@async_test
async def test_a_closed_market_waits_instead_of_missing(env):
    env.sync(None)
    sat = dt.datetime(2026, 11, 7, 11, 0, 0)
    await go(env, env.ev("ENTRY", "BUY", now=sat), now=sat)
    a = env.state()
    assert env.fb.n == 0 and a["pending_entry"] is not None and not a["missed"]
    await env.rt.tick(dt.datetime(2026, 11, 9, 9, 10, 0))                                                  # still closed, Monday before the open
    assert env.fb.n == 0 and not env.state()["missed"]
    await env.rt.tick(MON)                                                                                # the session is open now
    assert env.fb.last("entry")[0] is not None


@async_test
async def test_it_starts_at_the_next_system_point_and_ignores_what_came_before(env):
    env.sync(PAPER_BUY)                                                                                   # the paper account already holds a long when the switch went on
    await go(env, env.ev("EXIT", "BUY", reason="TRAILING_STOP_REVERSAL", exit_price=49500.0))
    assert env.fb.n == 0 and env.state()["position"] is None
    await go(env, env.ev("ENTRY", "BUY", at="2020-01-01T00:00:00"))                                      # an event from before the switch
    assert env.fb.n == 0
    await go(env, env.ev("ENTRY", "BUY", backfill=True))
    assert env.fb.n == 0
    await go(env, env.ev("ENTRY", "BUY", entry_date="2026-11-09 09:00"), now=MON)                          # a signal 60 minutes old when noticed
    assert env.fb.n == 0
    await go(env, env.ev("ENTRY", "BUY"))
    assert env.fb.n == 1


@async_test
async def test_switching_the_instrument_off_cancels_a_waiting_entry_but_keeps_an_open_stop(env):
    env.sync(None)
    await go(env, env.ev("ENTRY", "BUY"))
    oid, _ = env.fb.last("entry")
    store.instrument_set(env.db, env.uid, IID, SYM, False, "tbtrader")
    await env.rt.tick(secs(5))
    assert env.fb.orders[oid]["status"] == "cancelled" and env.state()["pending_entry"] is None
    # with an open position
    env.rt.state.clear()
    store.instrument_set(env.db, env.uid, IID, SYM, True, "tbtrader")
    env.db.execute("UPDATE user_instrument_real SET changed_at=changed_at-60")
    env.db.commit()
    sid = await open_long(env)
    api_kill = store.force_all_paper(env.db, "admin")
    assert api_kill == 1
    await env.rt.tick(secs(70))
    a = env.state()
    assert env.fb.orders[sid]["status"] == "trigger pending" and a["position"] and a["released"] is True


@async_test
async def test_a_request_with_no_clear_answer_halts_the_instrument_until_a_person_clears_it(env):
    env.sync(None)
    env.fb.place_error = angel_orders.Unknown("No answer from Angel One.")
    await go(env, env.ev("ENTRY", "BUY"))
    a = env.state()
    assert a["halted"] and "no clear answer" in a["halted"] and env.fb.n == 0
    env.fb.place_error = None
    await go(env, env.ev("ENTRY", "BUY"), now=secs(30))
    assert env.fb.n == 0                                                                                   # halted: nothing more is sent
    assert env.rt.clear_halt(env.db, env.uid, IID, "tbtrader") is True and env.state()["halted"] is None


@async_test
async def test_a_rejected_stop_exits_the_position_instead_of_leaving_it_unprotected(env):
    env.sync(None)
    await go(env, env.ev("ENTRY", "BUY"))
    env.fb.fill(env.fb.last("entry")[0], 50002.5)
    env.sync(PAPER_BUY, MON)
    env.fb.place_error = ao.AngelOneApiError("AB1007 trigger too far", "data")
    await env.rt.tick(secs(5))
    a = env.state()
    assert a["position"] and a["exit_since"] is not None and any("rejected" in x["text"] for x in a["events"])
    env.fb.place_error = None
    await env.rt.tick(secs(40))                                                                            # past the grace: an exit LIMIT goes out
    assert env.fb.last("entry")[1]["p"]["transactiontype"] == "SELL"


@async_test
async def test_a_position_at_angel_one_that_does_not_match_halts_only_after_it_persists(env):
    await open_long(env)
    env.fb.orders["B1"]["status"] = "cancelled"                                                            # Angel One no longer shows our long
    await env.rt.tick(secs(10))
    assert env.state()["halted"] is None                                                                   # a lagging positions report is tolerated
    await env.rt.tick(secs(40))
    assert env.state()["halted"] and "net quantity" in env.state()["halted"]


@async_test
async def test_the_state_survives_a_restart(env):
    sid = await open_long(env)
    rt2 = tb_real.RealTB(broker_for=env.rt.broker_for)
    rt2.sync(IID, env.uid, SYM, {"instrument": dict(INST), "ltp": 50001.0, "position": {"direction": "BUY", "entry_price": 50000.0, "active_stop": 50010.0}}, secs(20))
    await rt2.tick(secs(20))
    a = rt2.state[(env.uid, IID)]
    assert a["position"]["entry_price"] == 50002.5
    assert [c for c in env.fb.calls if c[0] == "modify" and c[1] == sid]


@async_test
async def test_every_order_is_stored_and_audited_and_no_secret_is_in_them(env):
    env.sync(None)
    await go(env, env.ev("ENTRY", "BUY"))
    rows = env.db.execute("SELECT * FROM real_tb_orders").fetchall()
    assert len(rows) == 1 and rows[0]["order_type"] == "LIMIT"
    acts = [r["action"] for r in env.db.execute("SELECT action FROM audit_log")]
    assert "broker.tb.order" in acts
    blob = json.dumps(env.state()) + json.dumps([dict(r) for r in rows])
    for s in ("KEY12345", "JBSWY3DPEHPK3PXP", "1234"):
        assert s not in blob.replace("1234567", "")


def test_the_paper_engine_hands_its_signals_to_the_executor_and_a_failure_there_never_reaches_it():
    from app.paper_trading.engine import PaperEngine
    seen = []

    class Good:
        def handle(self, events, snap, now):
            seen.append(("handle", len(events)))

        def sync(self, *a):
            seen.append(("sync", a[0]))

    class Bad:
        def handle(self, *a):
            raise RuntimeError("boom")

    eng = object.__new__(PaperEngine)
    eng.real = Good()
    eng._real_step("handle", [{"event": "ENTRY"}], {}, None)
    eng._real_step("sync", "i-1", "u", "BANKNIFTY", {}, None)
    assert seen == [("handle", 1), ("sync", "i-1")]
    eng.real = Bad()
    eng._real_step("handle", [], {}, None)                                                                 # swallowed
    eng.real = None
    eng._real_step("handle", [], {}, None)                                                                 # no executor: nothing happens


def test_people_and_admins_can_see_the_executor_state_and_a_person_can_clear_a_halt(db, new_client, monkeypatch):
    from types import SimpleNamespace
    from app import main as appmain
    from app.broker import api as broker_api
    from tests.security.helpers import api, login, make_owner
    owner = make_owner(db)
    admin = make_user(db, "admintb", "admin", created_by=owner.id)
    user = make_user(db, "usertb", "user")
    c_admin, c_user = new_client(), new_client()
    assert login(c_admin, admin).status_code == 200 and login(c_user, user).status_code == 200

    async def broker_for(c, uid):
        return FakeBroker()

    rt = tb_real.RealTB(broker_for=broker_for)
    monkeypatch.setattr(appmain, "engine", SimpleNamespace(real=rt, instruments={}))
    a = rt._acct(db, user.id, IID, SYM, 1.0)
    a["halted"] = "test halt"
    rt._save(db, a)
    mine = c_user.get("/api/me/broker/tb").json()["instruments"]
    assert len(mine) == 1 and mine[0]["halted"] == "test halt"
    assert [x["username"] for x in c_admin.get("/api/admin/broker/tb").json()["instruments"]] == ["usertb"]
    assert c_user.get("/api/admin/broker/tb").status_code == 403
    assert api(c_user, "POST", f"/api/me/broker/tb/{IID}/clear-halt").status_code == 200
    assert c_user.get("/api/me/broker/tb").json()["instruments"][0]["halted"] is None
    assert api(c_user, "POST", f"/api/me/broker/tb/{IID}/clear-halt").status_code == 404


@async_test
async def test_engine_times_are_read_as_india_time_at_minute_precision(env):
    """The engine writes 'YYYY-MM-DD HH:MM' in India time without a zone, and the server runs on UTC."""
    ist_now = dt.datetime.now(tb_real.IST).replace(tzinfo=None)
    env.db.execute("UPDATE user_instrument_real SET changed_at=?", (dt.datetime.now().timestamp(),))
    env.db.commit()
    env.sync(None)
    await go(env, env.ev("ENTRY", "BUY", at=(ist_now - dt.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")))
    assert env.fb.n == 0                                                                                  # five minutes before the switch: ignored
    await go(env, env.ev("ENTRY", "BUY", at=ist_now.strftime("%Y-%m-%d %H:%M")))
    assert env.fb.n == 1                                                                                  # the same minute: acted on


@async_test
async def test_the_daily_order_cap_never_holds_back_a_stop(env, monkeypatch):
    monkeypatch.setattr(tb_real, "MAX_ORDERS_PER_DAY", 1)
    env.sync(None)
    await go(env, env.ev("ENTRY", "BUY"))
    env.fb.fill(env.fb.last("entry")[0], 50002.5)
    env.sync(PAPER_BUY, MON)
    await env.rt.tick(secs(5))
    assert env.fb.last("stop")[0] is not None


@async_test
async def test_a_partly_filled_order_that_ends_cancelled_halts(env):
    env.sync(None)
    env.rt.sync(IID, env.uid, SYM, {"instrument": dict(INST, lots=1, quantity=30), "ltp": 50001.0, "position": None}, MON)
    await go(env, env.ev("ENTRY", "BUY"))
    oid, _ = env.fb.last("entry")
    env.fb.orders[oid].update(status="cancelled", filledshares="15")
    await env.rt.tick(secs(5))
    a = env.state()
    assert a["halted"] and "15 of 30" in a["halted"]


@async_test
async def test_a_rejected_stop_exits_at_once(env):
    env.sync(None)
    await go(env, env.ev("ENTRY", "BUY"))
    env.fb.fill(env.fb.last("entry")[0], 50002.5)
    env.sync(PAPER_BUY, MON)
    env.fb.place_error = ao.AngelOneApiError("trigger invalid", "data")
    await env.rt.tick(secs(5))
    env.fb.place_error = None
    await env.rt.tick(secs(10))                                                                           # the very next round, not 30 s later
    _, x = env.fb.last("entry")
    assert x["p"]["transactiontype"] == "SELL"
    stops_tried = [c for c in env.fb.calls if c[0] == "place" and c[1]["ordertype"] == "STOPLOSS_LIMIT"]
    assert len(stops_tried) == 1                                                                          # no second stop while exiting


@async_test
async def test_nothing_is_asked_of_angel_one_when_there_is_nothing_to_do(env):
    calls = []
    inner = env.rt.broker_for

    async def counting(c, uid):
        calls.append(uid)
        return await inner(c, uid)

    env.rt.broker_for = counting
    env.sync(None)
    for t in (0, 5, 10):
        await env.rt.tick(secs(t))
    assert calls == []


@async_test
async def test_mcx_contracts_are_not_traded_for_real_yet(env):
    mcx = {"trading_symbol": "GOLDM05DEC26FUT", "token": "777", "exch_seg": "MCX", "lot_size": 1, "lots": 1, "quantity": 1, "session": "MCX"}
    env.rt.sync(IID, env.uid, SYM, {"instrument": mcx, "ltp": 50001.0, "position": None}, MON)
    await go(env, env.ev("ENTRY", "BUY", contract="GOLDM05DEC26FUT", quantity=1))
    a = env.state()
    assert env.fb.n == 0 and "MCX" in a["missed"][-1]["reason"]
