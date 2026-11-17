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
INST = {"trading_symbol": CONTRACT, "token": "555", "exch_seg": "NFO", "lot_size": 30, "lots": 1, "quantity": 30, "session": "NSE", "tick_size": 0.05}


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
    e.at = lambda: (dt.datetime.now(tb_real.IST).replace(tzinfo=None) + dt.timedelta(seconds=1)).isoformat()   # the engine writes IST wall time, whatever the machine's zone

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
    assert s["p"]["variety"] == "STOPLOSS" and s["p"]["ordertype"] == "STOPLOSS_LIMIT" and s["p"]["transactiontype"] == "SELL" and s["p"]["triggerprice"] == "49499.95" and s["p"]["price"] == "49489.95"
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
    assert len(mods) == 1 and mods[0][2]["triggerprice"] == "50002.45" and mods[0][2]["price"] == "49992.45"
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
    assert env.fb.orders[sid]["status"] == "cancelled"
    xid, x = env.fb.last("entry")
    assert x["p"]["transactiontype"] == "SELL" and x["p"]["ordertype"] == "LIMIT" and x["p"]["quantity"] == "30"
    await env.rt.tick(secs(64))                                                                        # the LIMIT exit did not fill in 3 s
    assert env.fb.orders[xid]["status"] == "cancelled"
    await env.rt.tick(secs(65))
    _, m = env.fb.last("entry")
    assert m["p"]["transactiontype"] == "SELL" and m["p"]["ordertype"] == "MARKET"                   # then at the market


@async_test
async def test_a_stop_that_does_not_fill_in_time_is_replaced_by_an_exit_order(env):
    sid = await open_long(env)
    env.sync(None, secs(60))
    await go(env, env.ev("EXIT", "BUY", now=secs(60), reason="STOP_LOSS_REVERSAL", exit_price=49500.0), now=secs(60))
    assert env.fb.orders[sid]["status"] == "trigger pending"                                              # waiting for our own stop
    await env.rt.tick(secs(62))
    assert env.fb.orders[sid]["status"] == "trigger pending"                                              # still inside the 3 s grace
    await env.rt.tick(secs(64))
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
async def test_a_price_that_ran_away_is_still_entered_at_the_live_price(env):
    env.sync(None)
    env.fb.ltp_value = 50400.0                                                                           # 400 points past 50000 (it used to be skipped)
    await go(env, env.ev("ENTRY", "BUY"))
    _, o = env.fb.last("entry")
    assert env.fb.n == 1 and o["p"]["ordertype"] == "LIMIT" and float(o["p"]["price"]) == 50402.0       # decided 10 Nov 2026: never skipped


@async_test
@pytest.mark.parametrize("what,reason", [("margin", "margin needed"), ("cash", "margin needed"), ("loss", "daily cap")])
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
async def test_the_accounts_own_switch_to_paper_pauses_and_back_on_real_the_open_position_is_managed_again(env):
    """The owner, 15 Nov 2026: going to paper and back must not lose the real arm. While on paper nothing is sent; back on REAL the next round manages it."""
    sid = await open_long(env)
    store.set_mode(env.db, env.uid, "paper", "tbtrader")
    await env.rt.tick(secs(10))
    assert env.state()["released"] is True and env.fb.orders[sid]["status"] == "trigger pending"      # the stop stays at Angel One
    assert store.instrument_switches(env.db, env.uid)[IID] is True and store.instrument_real(env.db, env.uid, IID) is False
    env.sync({"direction": "BUY", "entry_price": 50000.0, "active_stop": 50002.5}, secs(20))
    await env.rt.tick(secs(20))
    assert not [c for c in env.fb.calls if c[0] == "modify"]                                          # on paper: nothing is sent
    store.set_mode(env.db, env.uid, "real", "tbtrader")
    await env.rt.tick(secs(25))                                                                       # 15 s later, not after the 60 s idle poll
    a = env.state()
    assert a["released"] is False and any("on again" in e["text"] for e in a["events"])
    mods = [c for c in env.fb.calls if c[0] == "modify"]
    assert len(mods) == 1 and mods[0][2]["triggerprice"] == "50002.45"


@async_test
async def test_a_new_system_point_while_the_account_is_on_paper_sends_nothing_and_the_next_one_on_real_does(env):
    store.set_mode(env.db, env.uid, "paper", "tbtrader")
    env.sync(None)
    await go(env, env.ev("ENTRY", "BUY"))
    assert env.fb.n == 0
    store.set_mode(env.db, env.uid, "real", "tbtrader")
    await go(env, env.ev("ENTRY", "BUY", now=secs(60)), now=secs(60))
    assert env.fb.n == 1 and env.fb.last("entry")[0] is not None


@async_test
async def test_real_orders_go_out_while_the_person_is_signed_out_of_the_app(env):
    """The owner, 16 Nov 2026: live orders must never depend on the person being signed in (the 4 pm sign-out, a closed laptop or phone)."""
    from app.security import sessions
    sessions.create(env.db, env.uid, "Safari", "1.1.1.1")
    sessions.end_all(env.db, env.uid, "expired")                                  # signed out everywhere, as after the daily 4 pm sign-out
    assert env.db.execute("SELECT COUNT(*) FROM sessions WHERE user_id=? AND ended_at IS NULL", (env.uid,)).fetchone()[0] == 0
    env.sync(None)
    await go(env, env.ev("ENTRY", "BUY"))
    oid, _ = env.fb.last("entry")
    assert env.fb.n == 1 and oid is not None                                      # the entry went to Angel One
    env.fb.fill(oid, 50002.5)
    env.sync(PAPER_BUY, MON)
    await env.rt.tick(secs(5))
    assert env.fb.last("stop")[0] is not None and env.state()["position"]["side"] == "BUY"   # and its stop-loss too


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
    blob = json.dumps(env.state()) + json.dumps([{k: v for k, v in dict(r).items() if k not in ("created_at", "updated_at")} for r in rows])   # wall-clock times can contain "1234"
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
async def test_an_mcx_future_is_ordered_in_lots_x_lotsize_on_the_rupee_grid(env):
    mcx = {"trading_symbol": "GOLDM04DEC26FUT", "token": "777", "exch_seg": "MCX", "lot_size": 100, "lots": 1, "quantity": 100, "session": "MCX", "tick_size": 1.0}
    env.fb.ltp_value = 125010.0
    env.rt.sync(IID, env.uid, SYM, {"instrument": mcx, "ltp": 125010.0, "position": None}, MON)
    await go(env, env.ev("ENTRY", "BUY", contract="GOLDM04DEC26FUT", quantity=100, entry_price=125000.0, active_stop=124000.0))
    [(_, p)] = [c for c in env.fb.calls if c[0] == "place"]
    assert p["exchange"] == "MCX" and p["quantity"] == "100" and p["ordertype"] == "LIMIT"     # 1 lot of GOLDM = 100 (grams) in Angel One's own unit
    price = float(p["price"])
    assert price == int(price) and price == 125073.0                        # whole rupees, a buffer of about Rs 62, inside the 0.5% band
    oid, _ = env.fb.last("entry")
    env.fb.fill(oid, price)
    await go(env, now=secs(5), position={"direction": "BUY", "entry_price": 125000.0, "active_stop": 124000.0})
    _, stop = env.fb.last("stop")
    sp = stop["p"]
    assert float(sp["triggerprice"]) == 123999.0 and float(sp["price"]) == 123751.0 and sp["quantity"] == "100"   # the stop's limit ~0.2% past the trigger


@async_test
async def test_an_unknown_tick_uses_whole_rupees_which_every_futures_grid_accepts(env):
    inst = {k: v for k, v in INST.items() if k != "tick_size"}  # no tick known: BANKNIFTY futures really trade on 0.20 now
    env.rt.sync(IID, env.uid, SYM, {"instrument": inst, "ltp": 50001.0, "position": None}, MON)
    await go(env, env.ev("ENTRY", "BUY"))
    [(_, p)] = [c for c in env.fb.calls if c[0] == "place"]
    price = float(p["price"])
    assert price == int(price) and round(price / 0.2, 6) == int(round(price / 0.2))


@async_test
async def test_a_real_stop_never_moves_back_after_the_handover_day(env):
    # paper on 10 Nov 2026: a BANKNIFTY BUY's stop went from 54,823 back DOWN to 53,834 (a bug, fixed). A real stop must never follow such a move.
    await open_long(env)                                                                      # Monday: stop 49,500 (the entry-day stop)
    def mod_triggers():
        return [c[2]["triggerprice"] for c in env.fb.calls if c[0] == "modify"]
    env.sync({"direction": "BUY", "entry_price": 50000.0, "active_stop": 49000.0}, secs(30))
    await env.rt.tick(secs(30))
    assert env.state()["stop_target"] == 49500.0 and mod_triggers() == []                      # the entry day: never wider
    tue = MON + dt.timedelta(days=1)
    env.sync({"direction": "BUY", "entry_price": 50000.0, "active_stop": 49000.0}, tue)
    await env.rt.tick(tue)
    assert env.state()["stop_target"] == 49000.0                                             # the day after entry: the 1% stop is handed over to the structural one (a rule)
    env.sync({"direction": "BUY", "entry_price": 50000.0, "active_stop": 49800.0}, tue + dt.timedelta(minutes=1))
    await env.rt.tick(tue + dt.timedelta(minutes=1))
    assert env.state()["stop_target"] == 49800.0                                             # trails up
    wed = MON + dt.timedelta(days=2)
    env.sync({"direction": "BUY", "entry_price": 50000.0, "active_stop": 49000.0}, wed)
    await env.rt.tick(wed)
    a = env.state()
    assert a["stop_target"] == 49800.0 and mod_triggers()[-1] == "49799.95"                    # never back
    assert any("never moves back" in ev["text"] if isinstance(ev, dict) else "never moves back" in str(ev) for ev in a["events"])
    env.sync({"direction": "BUY", "entry_price": 50000.0, "active_stop": 50100.0}, wed + dt.timedelta(minutes=1))
    await env.rt.tick(wed + dt.timedelta(minutes=1))
    assert env.state()["stop_target"] == 50100.0                                             # and still trails forward


# ---------------------------------------------------------------- deep check before real accounts (10 Nov 2026)
TUE = dt.datetime(2026, 11, 10, 9, 15, 5)


def _texts(env):
    return [e["text"] for e in env.state()["events"]]


async def _new_day_without_yesterdays_stop(env, sid):
    env.fb.orders[sid]["status"] = "cancelled"                                                 # the exchange ends a DAY order at the close


@async_test
async def test_a_gap_open_beyond_the_stop_waits_for_the_strategys_15_minute_decision_then_uses_its_protective_stop(env):
    sid = await open_long(env)                                                                # Monday: BUY, stop 49,500
    await _new_day_without_yesterdays_stop(env, sid)
    env.fb.ltp_value = 49300.0                                                                # Tuesday opens at 49,300: below the stop
    env.sync(PAPER_BUY, TUE)                                                                  # the strategy has not seen today's first minute yet
    await env.rt.tick(TUE)
    assert env.fb.last("stop") == (None, None) and not [c for c in env.fb.calls if c[0] == "place" and c[1]["ordertype"] == "LIMIT" and c[1]["transactiontype"] == "SELL"]
    assert any("opened beyond the stop" in t for t in _texts(env))                            # before: a stop into the gap, refused -> sold at the open
    env.sync({**PAPER_BUY, "gap_state": "GAP_PENDING"}, TUE.replace(minute=25))
    await env.rt.tick(TUE.replace(minute=25))
    assert env.fb.last("stop") == (None, None) and env.state()["position"] is not None
    t931 = TUE.replace(minute=31)
    env.sync({**PAPER_BUY, "gap_state": "GAP_HELD", "protective_sl": 49200.0}, t931)         # held: the 15-minute candle's low is today's stop
    await env.rt.tick(t931)
    _, s = env.fb.last("stop")
    assert s["p"]["triggerprice"] == "49199.95" and env.state()["stop_target"] == 49200.0     # wider than 49,500, allowed: it is the strategy's gap rule


@async_test
async def test_a_gap_exit_by_the_15_minute_rule_sends_the_exit_order_at_once(env):
    sid = await open_long(env)
    await _new_day_without_yesterdays_stop(env, sid)
    env.fb.ltp_value = 49300.0
    env.sync({**PAPER_BUY, "gap_state": "GAP_PENDING"}, TUE)
    await env.rt.tick(TUE)
    t930 = TUE.replace(minute=30, second=20)
    env.fb.ltp_value = 49140.0
    await go(env, env.ev("EXIT", "BUY", now=t930, reason="GAP_EXIT_15M", exit_price=49150.0), now=t930, position=None)
    ex = [c[1] for c in env.fb.calls if c[0] == "place" and c[1]["ordertype"] == "LIMIT" and c[1]["transactiontype"] == "SELL"]
    assert len(ex) == 1 and env.fb.last("stop") == (None, None)


@async_test
async def test_the_gap_wait_ends_after_25_minutes_and_the_ordinary_stop_goes_out(env):
    sid = await open_long(env)
    await _new_day_without_yesterdays_stop(env, sid)
    env.fb.ltp_value = 49300.0
    env.sync(PAPER_BUY, TUE)
    await env.rt.tick(TUE)
    assert env.fb.last("stop") == (None, None)
    late = TUE.replace(minute=40, second=10)                                                   # no decision from the strategy in 25 minutes
    await env.rt.tick(late)
    assert env.fb.last("stop")[1]["p"]["triggerprice"] == "49499.95"                         # (Angel One refuses it -> the position is exited at once)


@async_test
async def test_a_stop_order_of_an_earlier_day_missing_from_the_book_is_not_taken_as_resting(env):
    sid = await open_long(env)
    del env.fb.orders[sid]                                                                    # a new day: Angel One's book no longer has it
    t = dt.datetime(2026, 11, 10, 9, 20)
    env.sync(PAPER_BUY, t)
    await env.rt.tick(t)
    old = [o for o in env.state()["orders"] if o["broker_id"] == sid][0]
    assert old["state"] == "cancelled"
    nid, s = env.fb.last("stop")
    assert nid != sid and s["p"]["triggerprice"] == "49499.95"                                # protected again, not left to an order that is gone


@async_test
async def test_a_triggered_stop_that_does_not_fill_is_replaced_by_an_exit_order_without_waiting_for_the_strategy(env):
    sid = await open_long(env)
    env.fb.orders[sid]["status"] = "open"                                                     # the trigger was hit; the limit waits
    env.fb.ltp_value = 49400.0                                                                # price ran past the stop's limit (49,489.95)
    await env.rt.tick(secs(10))
    await env.rt.tick(secs(45))
    assert env.fb.orders[sid]["status"] == "cancelled"
    ex = [c[1] for c in env.fb.calls if c[0] == "place" and c[1]["ordertype"] == "LIMIT" and c[1]["transactiontype"] == "SELL"]
    assert len(ex) == 1


@async_test
async def test_an_open_status_without_the_price_past_the_trigger_does_not_exit(env):
    sid = await open_long(env)
    env.fb.orders[sid]["status"] = "open"
    env.fb.ltp_value = 49900.0                                                                # nowhere near the trigger: a status we do not trust alone
    await env.rt.tick(secs(10))
    await env.rt.tick(secs(45))
    assert env.fb.orders[sid]["status"] == "open" and env.state()["exit_since"] is None


@async_test
async def test_a_result_the_real_account_may_not_act_on_is_ignored(env):
    await open_long(env)
    env.rt.sync(IID, env.uid, SYM, {"instrument": dict(INST), "ltp": 50001.0, "real_ready": False,           # e.g. the daily-close fallback showing a SELL
                                    "position": {"direction": "SELL", "entry_price": 50000.0, "active_stop": 50500.0}}, secs(20))
    await env.rt.tick(secs(20))
    await env.rt.tick(secs(60))
    a = env.state()
    assert a["exit_since"] is None and a["position"]["side"] == "BUY" and a["stop_target"] == 49500.0


@async_test
async def test_the_stop_may_widen_only_on_the_first_trading_day_after_the_entry(env):
    await open_long(env)                                                                      # Monday
    early = dt.datetime(2026, 11, 10, 8, 0)                                                    # Tuesday before the open: not a trading moment yet
    env.sync({"direction": "BUY", "entry_price": 50000.0, "active_stop": 49000.0}, early)
    await env.rt.tick(early)
    assert env.state()["stop_target"] == 49500.0
    t = dt.datetime(2026, 11, 10, 9, 20)                                                       # Tuesday, open: the handover day
    await env.rt.tick(t)
    assert env.state()["stop_target"] == 49000.0


@async_test
async def test_a_late_opening_day_also_waits_for_the_strategys_gap_decision(env):
    # MCX on an NSE holiday opens only at 17:00 (GOLDTEN, 10 Nov 2026): the first stop of that day found price already below it
    sid = await open_long(env)
    await _new_day_without_yesterdays_stop(env, sid)
    env.fb.ltp_value = 49300.0
    t = dt.datetime(2026, 11, 10, 17, 0, 5)
    mcx = lambda pos, at: env.rt.sync(IID, env.uid, SYM, {"instrument": {**INST, "session": "MCX"}, "ltp": 49300.0, "position": pos}, at)
    mcx(PAPER_BUY, t)
    await env.rt.tick(t)
    assert env.fb.last("stop") == (None, None) and any("opened beyond the stop" in x for x in _texts(env))
    mcx({**PAPER_BUY, "gap_state": "GAP_PENDING"}, t.replace(minute=10))
    await env.rt.tick(t.replace(minute=10))
    assert env.fb.last("stop") == (None, None)
    mcx({**PAPER_BUY, "gap_state": "GAP_HELD", "protective_sl": 49200.0}, t.replace(minute=16))
    await env.rt.tick(t.replace(minute=16))
    assert env.fb.last("stop")[1]["p"]["triggerprice"] == "49199.95"


@async_test
async def test_a_later_first_stop_of_the_day_that_is_not_beyond_goes_out_at_once(env):
    sid = await open_long(env)
    await _new_day_without_yesterdays_stop(env, sid)
    t = dt.datetime(2026, 11, 10, 13, 0)
    env.sync(PAPER_BUY, t)
    await env.rt.tick(t)
    assert env.fb.last("stop")[1]["p"]["triggerprice"] == "49499.95"


# ---------------------------------------------------------------- a flat real account joins the strategy's side at a FRESH trigger (16 Nov 2026)
def _day_epoch(y, m, d):
    return int(dt.datetime(y, m, d, tzinfo=dt.timezone.utc).timestamp())


def _gap_down_minutes():
    """9 Nov (NSE session): the first 15-minute candle 09:15-09:29 has its LOW at 49,800; 09:30 stays above; 09:31 breaks it."""
    day = dt.date(2026, 11, 9)
    out = []
    for k in range(15):
        t = dt.datetime.combine(day, dt.time(9, 15 + k))
        out.append((t, 49880.0, 49900.0, 49800.0 if k == 7 else 49840.0, 49860.0))
    out.append((dt.datetime.combine(day, dt.time(9, 30)), 49860.0, 49870.0, 49850.0, 49855.0))
    out.append((dt.datetime.combine(day, dt.time(9, 31)), 49820.0, 49830.0, 49750.0, 49760.0))
    return out


def _join_env(env, monkeypatch, direction="SELL"):
    from app.live import candle_store
    monkeypatch.setattr(candle_store, "get", lambda *a, **k: __import__("pandas").DataFrame(columns=["open", "high", "low", "close", "volume"]))
    env.db.execute("UPDATE user_instrument_real SET changed_at=?", (dt.datetime(2026, 11, 8, tzinfo=tb_real.IST).timestamp(),))   # armed the day before
    env.db.commit()
    env.rt.tick_minutes = lambda tok: _gap_down_minutes()
    env.fb.ltp_value = 49760.0
    snap = {"instrument": dict(INST), "ltp": 49760.0, "latest_top": 52000.0, "latest_bottom": 50000.0,
            "position": {"direction": direction, "entry_price": 51000.0, "active_stop": 51000.0},
            "candles": [{"time": _day_epoch(2026, 11, 6), "open": 50200, "high": 50300, "low": 50050, "close": 50100},
                        {"time": _day_epoch(2026, 11, 9), "open": 49900, "high": 49900, "low": 49750, "close": 49760}]}
    return snap


def test_the_15_minute_gap_rule_trigger_for_a_flat_account():
    from app.strategy.top_bottom.gap_protocol import NSE
    mins = _gap_down_minutes()
    hit = tb_real.join_trigger("SELL", 50100, 49900, 50000, mins, NSE, dt.datetime(2026, 11, 9, 9, 31, 20))
    assert hit is not None and hit[0] == 49800.0 and hit[1].time() == dt.time(9, 31) and "15-minute" in hit[2]
    assert tb_real.join_trigger("SELL", 50100, 49900, 50000, mins, NSE, dt.datetime(2026, 11, 9, 9, 25)) is None          # candle not complete
    assert tb_real.join_trigger("SELL", 50100, 49900, 50000, mins[:16], NSE, dt.datetime(2026, 11, 9, 9, 30, 30)) is None  # low not broken yet
    assert tb_real.join_trigger("SELL", 49950, 49900, 50000, mins, NSE, dt.datetime(2026, 11, 9, 9, 31, 20)) is None       # already below yesterday: not fresh
    no_gap = tb_real.join_trigger("SELL", 50100, 50050, 50000, mins, NSE, dt.datetime(2026, 11, 9, 9, 31, 20))              # opened above: first minute below
    assert no_gap is not None and no_gap[0] == 49880.0 and no_gap[1].time() == dt.time(9, 15) and no_gap[2] == "Bottom crossed"
    assert tb_real.join_trigger("BUY", 50100, 49900, 50000, mins, NSE, dt.datetime(2026, 11, 9, 9, 31, 20)) is None        # a BUY point the other way: nothing
    partial = mins[5:]                                                                                                     # 09:15-09:19 lost (a restart)
    assert tb_real.join_trigger("SELL", 50100, 49900, 50000, partial, NSE, dt.datetime(2026, 11, 9, 9, 31, 20)) is None    # never guesses the LOW


@async_test
async def test_a_flat_real_account_takes_the_strategys_sell_when_the_15_minute_low_breaks_after_a_gap_down(env, monkeypatch):
    snap = _join_env(env, monkeypatch)
    now = dt.datetime(2026, 11, 9, 9, 31, 20)
    env.rt.sync(IID, env.uid, SYM, snap, now)
    await env.rt.tick(now)
    places = [c for c in env.fb.calls if c[0] == "place"]
    assert len(places) == 1 and places[0][1]["transactiontype"] == "SELL" and places[0][1]["ordertype"] == "LIMIT"
    pe = env.state()["pending_entry"]
    assert pe["side"] == "SELL" and pe["strategy_stop"] == 51000.0 and pe["strategy_price"] == 49800.0                       # the strategy's own stop
    assert any("15-minute gap rule" in e["text"] for e in env.state()["events"])
    await env.rt.tick(now + dt.timedelta(seconds=5))
    joins = [e for e in env.state()["events"] if "fresh SELL trigger" in e["text"]]
    assert len(joins) == 1                                     # the trigger is taken once (a second order here is the same entry's MARKET retry after 3 s)


@async_test
async def test_a_trigger_noticed_too_late_is_not_chased(env, monkeypatch):
    snap = _join_env(env, monkeypatch)
    now = dt.datetime(2026, 11, 9, 9, 50)                                                                                  # 19 minutes after 09:31
    env.rt.sync(IID, env.uid, SYM, snap, now)
    await env.rt.tick(now)
    assert not [c for c in env.fb.calls if c[0] == "place"]
    assert any("not chased" in e["text"] for e in env.state()["events"])


@async_test
async def test_no_join_when_the_strategy_is_flat_or_the_real_switch_is_off(env, monkeypatch):
    snap = _join_env(env, monkeypatch)
    now = dt.datetime(2026, 11, 9, 9, 31, 20)
    flat = dict(snap, position=None)
    env.rt.sync(IID, env.uid, SYM, flat, now)
    await env.rt.tick(now)
    assert not [c for c in env.fb.calls if c[0] == "place"]
    store.instrument_set(env.db, env.uid, IID, SYM, False, "tbtrader")
    env.rt.sync(IID, env.uid, SYM, snap, now)
    await env.rt.tick(now)
    assert not [c for c in env.fb.calls if c[0] == "place"]


async def _joined_and_filled(env, monkeypatch, fill=49760.0):
    snap = _join_env(env, monkeypatch)
    now = dt.datetime(2026, 11, 9, 9, 31, 20)
    env.rt.sync(IID, env.uid, SYM, snap, now)
    await env.rt.tick(now)
    oid, _ = env.fb.last("entry")
    env.fb.fill(oid, fill)
    await env.rt.tick(now + dt.timedelta(seconds=2))
    return snap, now


@async_test
async def test_a_joined_position_gets_a_fresh_trades_1_percent_stop_not_the_old_far_one(env, monkeypatch):
    """16 Nov: the join took the strategy's old stop (4.6% away, beyond MCX's circuit limit), Angel One refused it and the position was closed."""
    await _joined_and_filled(env, monkeypatch, 49760.0)
    a = env.state()
    assert a["position"]["side"] == "SELL" and a["position"]["join"]["stop"] == 50257.6 and a["stop_target"] == 50257.6     # 49,760 + 1%
    sid, stop = env.fb.last("stop")
    assert sid is not None and float(stop["p"]["triggerprice"]) > 50257.6 and float(stop["p"]["triggerprice"]) < 50258       # one tick beyond


@async_test
async def test_a_joined_position_moves_to_breakeven_at_2_percent_and_takes_a_tighter_strategy_stop(env, monkeypatch):
    snap, now = await _joined_and_filled(env, monkeypatch, 49760.0)
    env.rt.sync(IID, env.uid, SYM, dict(snap, ltp=48700.0), now + dt.timedelta(seconds=10))        # 2.1% in profit
    await env.rt.tick(now + dt.timedelta(seconds=10))
    assert env.state()["stop_target"] == 49760.0                                                   # breakeven
    tight = dict(snap, ltp=48700.0, position={"direction": "SELL", "entry_price": 51000.0, "active_stop": 49500.0})
    env.rt.sync(IID, env.uid, SYM, tight, now + dt.timedelta(seconds=20))
    await env.rt.tick(now + dt.timedelta(seconds=20))
    assert env.state()["stop_target"] == 49500.0                                                   # the strategy's own stop, now tighter
    loose = dict(snap, ltp=49000.0, position={"direction": "SELL", "entry_price": 51000.0, "active_stop": 51000.0})
    env.rt.sync(IID, env.uid, SYM, loose, now + dt.timedelta(seconds=30))
    await env.rt.tick(now + dt.timedelta(seconds=30))
    assert env.state()["stop_target"] == 49500.0                                                   # never loosens


@async_test
async def test_after_a_refused_stop_closes_the_join_one_fresh_re_cross_may_join_again_that_day_and_no_more(env, monkeypatch):
    snap, now = await _joined_and_filled(env, monkeypatch, 49760.0)
    sid, _ = env.fb.last("stop")
    env.fb.orders[sid]["status"], env.fb.orders[sid]["text"] = "rejected", "RMS: order refused"
    await env.rt.tick(now + dt.timedelta(seconds=10))                                              # refused (not for the daily limit): the safety exit goes out
    xid, _ = env.fb.last("entry")                                                                  # the exit is a plain LIMIT order
    env.fb.fill(xid, 49790.0)
    await env.rt.tick(now + dt.timedelta(seconds=14))
    a = env.state()
    assert a["position"] is None and str(a.get("join_retry_after", "")).startswith("2026-11-09")
    day = dt.date(2026, 11, 9)
    later = _gap_down_minutes() + [(dt.datetime.combine(day, dt.time(9, 40)), 49790.0, 49830.0, 49795.0, 49820.0),     # back above the LOW
                                   (dt.datetime.combine(day, dt.time(9, 41)), 49810.0, 49815.0, 49760.0, 49770.0)]     # and below it again
    env.rt.tick_minutes = lambda tok: later
    t2 = dt.datetime(2026, 11, 9, 9, 41, 20)
    env.rt.sync(IID, env.uid, SYM, snap, t2)
    n_before = len([c for c in env.fb.calls if c[0] == "place"])
    await env.rt.tick(t2)
    assert len([c for c in env.fb.calls if c[0] == "place"]) == n_before + 1 and env.state()["pending_entry"]["side"] == "SELL"
    assert any("crossed again" in e["text"] for e in env.state()["events"])
    assert env.state().get("join_retry_day") == "2026-11-09"                                       # the one extra try is used
    assert env.rt._join_retry_after(env.state(), "2026-11-09") is None                             # no third


def test_a_join_closed_by_a_refused_stop_before_this_rule_is_read_from_the_event_log():
    rt = tb_real.RealTB(broker_for=None)
    a = {"events": [{"at": "2026-11-16 09:51:57", "text": "the strategy already holds SELL and this real account is flat: fresh SELL trigger (...) puts it in"},
                    {"at": "2026-11-16 09:52:11", "text": "the stop order was rejected: the position is exited now with a LIMIT order, not left unprotected"},
                    {"at": "2026-11-16 09:52:16", "text": "POSITION CLOSED by the exit order at 136115.0; real P&L before charges -520.00"}]}
    assert rt._join_retry_after(a, "2026-11-16") == dt.datetime(2026, 11, 16, 9, 52, 16)
    assert rt._join_retry_after(dict(a, join_retry_day="2026-11-16"), "2026-11-16") is None
    assert rt._join_retry_after({"events": a["events"][:1]}, "2026-11-16") is None                 # closed some other way: no extra try


def _snap_at(position, ltp):
    return {"instrument": dict(INST), "ltp": ltp, "position": position}


async def _open_with_far_stop(env, stop=47000.0):
    env.sync(None)
    await go(env, env.ev("ENTRY", "BUY", active_stop=stop))
    env.fb.fill(env.fb.last("entry")[0], 50002.5)
    far = {"direction": "BUY", "entry_price": 50000.0, "active_stop": stop}
    env.rt.sync(IID, env.uid, SYM, _snap_at(far, 50001.0), MON)
    await env.rt.tick(secs(5))
    return far


@async_test
async def test_a_far_stop_is_watched_and_sent_at_its_own_trigger_when_the_price_comes_within_2_percent(env):
    """17 Nov 2026: a stop beyond the exchange's daily limit was refused. It now waits, and goes out at the strategy's trigger when the price is near."""
    far = await _open_with_far_stop(env)                                                               # 47,000 is 6% below 50,001
    a = env.state()
    assert a["position"] and env.fb.last("stop")[0] is None and a["stop_target"] == 47000.0            # no order yet, the stop is the strategy's
    assert any("watched" in e["text"] for e in a["events"]) and env.rt.view(env.db, env.uid)[0]["stop_watch_pct"] == 2.0
    env.rt.sync(IID, env.uid, SYM, _snap_at(far, 47900.0), secs(10))                                   # 1.9% away now
    await env.rt.tick(secs(10))
    sid, stop = env.fb.last("stop")
    assert sid is not None and 46999 < float(stop["p"]["triggerprice"]) < 47000                         # at the strategy's own trigger, one tick beyond
    assert env.rt.view(env.db, env.uid)[0]["stop_resting"] is True


@async_test
async def test_the_price_reaching_a_watched_stop_exits_at_once(env):
    far = await _open_with_far_stop(env)
    env.rt.sync(IID, env.uid, SYM, _snap_at(far, 46990.0), secs(10))                                   # straight through the stop between two rounds
    await env.rt.tick(secs(10))
    assert env.state()["exit_since"] and any("reached the watched stop" in e["text"] for e in env.state()["events"])


@async_test
async def test_a_stop_refused_for_the_daily_price_limit_is_watched_closer_each_time_then_exited(env):
    sid = await open_long(env)                                                                         # stop 49,500, 1% away: sent at once
    a = env.state()
    env.fb.orders[sid]["status"], env.fb.orders[sid]["text"] = "rejected", "Your order price exceeds the circuit limit. Please adjust your order"
    await env.rt.tick(secs(10))
    a = env.state()
    assert a["position"] and not a["exit_since"] and a["stop_watch"]["step"] == 1 and a["stop_target"] == 49500.0   # watched within 1% now, same stop
    near = {"direction": "BUY", "entry_price": 50000.0, "active_stop": 49500.0}
    env.rt.sync(IID, env.uid, SYM, _snap_at(near, 49900.0), secs(15))                                  # 0.8% away
    await env.rt.tick(secs(15))
    sid2, _ = env.fb.last("stop")
    assert sid2 not in (None, sid)
    env.fb.orders[sid2]["status"], env.fb.orders[sid2]["text"] = "rejected", "circuit limit"
    await env.rt.tick(secs(20))
    assert env.state()["stop_watch"]["step"] == 2 and not env.state()["exit_since"]                   # 0.5% next
    env.rt.sync(IID, env.uid, SYM, _snap_at(near, 49700.0), secs(25))                                  # 0.4% away
    await env.rt.tick(secs(25))
    sid3, _ = env.fb.last("stop")
    env.fb.orders[sid3]["status"], env.fb.orders[sid3]["text"] = "rejected", "circuit limit"
    await env.rt.tick(secs(30))
    assert env.state()["exit_since"]                                                                   # after 2%, 1% and 0.5%: the safety exit, as before


@async_test
async def test_a_stop_refused_for_another_reason_still_exits_at_once(env):
    sid = await open_long(env)
    env.fb.orders[sid]["status"], env.fb.orders[sid]["text"] = "rejected", "RMS: margin shortfall"
    await env.rt.tick(secs(10))
    assert env.state()["exit_since"] and not env.state().get("circuit_cap")


def test_a_signal_wakes_the_executor_at_once_instead_of_after_5_seconds(monkeypatch):
    import time as _time
    rounds = []
    rt = tb_real.RealTB(broker_for=None)

    async def tick(now=None):
        rounds.append(_time.monotonic())
    monkeypatch.setattr(rt, "tick", tick)
    monkeypatch.setattr(tb_real, "MIN_ROUND_GAP_S", 0.0)

    async def run():
        task = asyncio.create_task(rt._loop())
        await asyncio.sleep(0.05)
        rt.handle([{"event": "ENTRY", "backfill": False}], {}, None)
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(run())
    assert len(rounds) == 2 and rounds[1] - rounds[0] < 1.0                                            # TICK_S is 5 s: the signal did not wait for it


# ---------------------------------------------------------------- Angel One rebuilds its positions book overnight (17 Nov 2026, 07:01)
@async_test
async def test_outside_the_session_the_positions_book_is_not_compared(env):
    await open_long(env)

    async def empty():
        return []
    env.fb.positions = empty
    morning = dt.datetime(2026, 11, 10, 7, 1, 40)                                                    # NSE closed: the book is being rebuilt
    await env.rt.tick(morning)
    await env.rt.tick(morning + dt.timedelta(seconds=65))
    assert not env.state()["halted"] and env.state()["position"]


@async_test
async def test_a_carried_position_missing_from_the_days_book_halts_only_after_a_few_minutes(env):
    await open_long(env)                                                                             # Monday 10:00

    async def empty():
        return []
    env.fb.positions = empty
    t = dt.datetime(2026, 11, 10, 9, 16)                                                             # Tuesday, in the session, the book still empty
    await env.rt.tick(t)
    await env.rt.tick(t + dt.timedelta(seconds=30))
    assert not env.state()["halted"]                                                                 # not after 20 s: the day's book may come late
    await env.rt.tick(t + dt.timedelta(seconds=305))
    assert env.state()["halted"]                                                                     # still missing after 5 minutes: a person looks


@async_test
async def test_a_halt_raised_outside_the_session_by_the_positions_check_is_lifted_and_no_other(env):
    await open_long(env)
    a = env.state()
    a["halted"] = "Angel One shows a net quantity of 0 in BANKNIFTY25NOV26FUT but this app holds 30. If you changed your own position in this contract, check it, then clear the halt."
    a["events"].append({"at": "2026-11-10 07:01:40", "text": "HALTED: " + a["halted"]})
    await env.rt.tick(dt.datetime(2026, 11, 10, 8, 30))
    assert env.state()["halted"] is None and any("halt lifted" in e["text"] for e in env.state()["events"])
    a = env.state()
    a["halted"] = "Angel One shows a net quantity of 0 in BANKNIFTY25NOV26FUT but this app holds 30. If you changed your own position in this contract, check it, then clear the halt."
    a["events"].append({"at": "2026-11-10 10:01:40", "text": "HALTED: " + a["halted"]})              # raised IN the session: a real difference
    await env.rt.tick(dt.datetime(2026, 11, 10, 10, 2))
    assert env.state()["halted"]
    a["halted"] = "two entry orders filled: this app now holds 60"
    a["events"].append({"at": "2026-11-10 07:05:00", "text": "HALTED: two entry orders filled: this app now holds 60"})
    await env.rt.tick(dt.datetime(2026, 11, 10, 10, 3))
    assert env.state()["halted"].startswith("two entry orders")                                      # any other halt stays for a person


@async_test
async def test_hold_rule_reversal_closes_the_old_contract_first_then_enters_the_next_contract(env, monkeypatch):
    """Owner, 17 Nov 2026: past the cutoff the position stays in its contract; when its stop / System Point closes it, the reversal goes into the
    NEXT contract. The real account closes the old contract first (its own stop), then enters the next one."""
    nxt = {"trading_symbol": "BANKNIFTY29DEC26FUT", "token": "777", "exch_seg": "NFO", "lot_size": 30, "tick_size": 0.05}
    monkeypatch.setattr(tb_real.dryrun, "resolve_contract", lambda sym, name: dict(nxt) if name == nxt["trading_symbol"] else None)
    sid = await open_long(env)
    env.sync(None, secs(60))
    await go(env, env.ev("EXIT", "BUY", now=secs(60), reason="STOP_LOSS_REVERSAL", exit_price=49500.0, contract=CONTRACT),
             env.ev("ENTRY", "SELL", now=secs(60), entry_price=49800.0, active_stop=50298.0, contract=nxt["trading_symbol"]), now=secs(60))
    a = env.state()
    assert a["pending_entry"]["contract"]["trading_symbol"] == nxt["trading_symbol"] and env.fb.last("entry")[0] is None
    env.fb.fill(sid, 49498.0)                                                                         # the old contract's stop fills
    await env.rt.tick(secs(65))
    _, e = env.fb.last("entry")
    assert e["p"]["tradingsymbol"] == nxt["trading_symbol"] and e["p"]["symboltoken"] == "777" and e["p"]["transactiontype"] == "SELL"
    assert env.fb.orders[sid]["p"]["tradingsymbol"] == CONTRACT
