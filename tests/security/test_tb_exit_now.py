"""Exit now on the Top-Bottom page, end to end over HTTP against the fake Angel One: real goes off, the stop is cancelled, a LIMIT exit closes the position."""
import pytest
from app.broker import store
from tests.security.test_real_e2e import CONTRACT, IID, MON, PAPER_BUY, async_test, e2e, places, secs  # noqa: F401


@async_test
async def test_exit_now_cancels_the_stop_and_closes_with_a_limit_order(e2e, monkeypatch):
    fa, rt = e2e.fa, e2e.rt
    from app.broker import tb_real
    monkeypatch.setattr(tb_real, "now_ist", lambda: secs(20))
    e2e.sync(None)
    rt.handle([e2e.ev("ENTRY", "BUY")], {}, MON)
    await rt.tick(MON)
    fa.fill(fa.open_orders()[0]["orderid"], 50002.5)
    e2e.sync(PAPER_BUY)
    await rt.tick(secs(5))
    assert len(fa.open_orders("STOPLOSS_LIMIT")) == 1
    store.instrument_set(e2e.db, e2e.uid, IID, "BANKNIFTY", False, "me")               # what the endpoint does first
    assert rt.exit_now(e2e.db, e2e.uid, IID, "me") == "exiting"
    await rt.tick(secs(21))
    assert fa.open_orders("STOPLOSS_LIMIT") == []
    [ex] = [b for b in places(fa, "LIMIT") if b["transactiontype"] == "SELL"]
    fa.fill(fa.open_orders("LIMIT")[0]["orderid"], 50000.0)
    await rt.tick(secs(26))
    assert fa.net(CONTRACT) == 0 and e2e.state()["position"] is None and not e2e.state().get("manual_exit")
    await rt.tick(secs(90))
    assert len(places(fa, "LIMIT")) == 2                                                # nothing re-enters


def test_exit_now_with_nothing_open_says_so(e2e):
    assert e2e.rt.exit_now(e2e.db, e2e.uid, IID, "me") == "nothing"


@async_test
async def test_a_symbol_the_admin_did_not_allow_is_never_traded(e2e):
    from app.broker import store as st
    st.grant(e2e.db, e2e.uid, True, 1, None, ["NIFTY"], "admin")
    st.set_mode(e2e.db, e2e.uid, "real", "e2etrader")
    st.instrument_set(e2e.db, e2e.uid, IID, "BANKNIFTY", True, "me")
    e2e.db.execute("UPDATE user_instrument_real SET changed_at=changed_at-120")
    e2e.db.commit()
    e2e.sync(None)
    e2e.rt.handle([e2e.ev("ENTRY", "BUY")], {}, MON)
    await e2e.rt.tick(MON)
    assert places(e2e.fa) == [] and "not in the symbols" in e2e.state()["missed"][-1]["reason"]


@async_test
async def test_real_trades_are_built_from_angel_one_fills_only(e2e):
    from app.broker import tb_real
    fa, rt = e2e.fa, e2e.rt
    e2e.sync(None)
    rt.handle([e2e.ev("ENTRY", "BUY")], {}, MON)
    await rt.tick(MON)
    assert tb_real.RealTB.real_trades(e2e.db, e2e.uid, IID) == []                     # placed, not filled: no trade yet
    fa.fill(fa.open_orders()[0]["orderid"], 50002.0)
    e2e.sync(PAPER_BUY)
    await rt.tick(secs(5))
    [t] = tb_real.RealTB.real_trades(e2e.db, e2e.uid, IID)
    assert t["status"] == "OPEN" and t["entry_price"] == 50002.0 and t["direction"] == "BUY"
    fa.fill(fa.open_orders("STOPLOSS_LIMIT")[0]["orderid"], 49500.0)
    await rt.tick(secs(10))
    [t] = tb_real.RealTB.real_trades(e2e.db, e2e.uid, IID)
    assert t["status"] == "CLOSED" and t["exit_by"] == "stop" and t["pnl_points"] == -502.0 and t["pnl_amount"] == -502.0 * 30


@async_test
async def test_an_expiry_roll_closes_the_old_contract_then_opens_the_same_side_in_the_new_one(e2e):
    from tests.security.test_real_e2e import INST
    fa, rt = e2e.fa, e2e.rt
    e2e.sync(None)
    rt.handle([e2e.ev("ENTRY", "BUY")], {}, MON)
    await rt.tick(MON)
    fa.fill(fa.open_orders()[0]["orderid"], 50002.5)
    e2e.sync(PAPER_BUY)
    await rt.tick(secs(5))
    NEW = "BANKNIFTY30DEC26FUT"
    fa.lot_sizes["556"] = 30
    rt.sync(IID, e2e.uid, "BANKNIFTY", {"instrument": {**INST, "trading_symbol": NEW, "token": "556"}, "ltp": 50001.0, "position": PAPER_BUY}, secs(60))
    rt.handle([e2e.ev("EXIT", "BUY", now=secs(60), reason="ROLLED_TO_NEXT_CONTRACT", exit_price=50001.0),
               e2e.ev("ENTRY", "BUY", now=secs(60), contract=NEW)], {}, secs(60))
    await rt.tick(secs(60))
    [ex] = [b for b in places(fa, "LIMIT") if b["transactiontype"] == "SELL"]
    assert ex["tradingsymbol"] == CONTRACT and fa.open_orders("STOPLOSS_LIMIT") == []          # old contract: stop gone, exit sent at once
    fa.fill(fa.open_orders("LIMIT")[0]["orderid"], 50000.0)
    await rt.tick(secs(65))
    await rt.tick(secs(66))
    new = [b for b in places(fa, "LIMIT") if b["tradingsymbol"] == NEW]
    assert len(new) == 1 and new[0]["transactiontype"] == "BUY" and new[0]["symboltoken"] == "556"


@async_test
async def test_the_real_order_uses_the_lots_the_person_chose_not_the_paper_quantity(e2e):
    from app.broker import store as st
    st.grant(e2e.db, e2e.uid, True, 3, None, None, "admin")
    st.set_mode(e2e.db, e2e.uid, "real", "e2etrader")
    st.instrument_set(e2e.db, e2e.uid, IID, "BANKNIFTY", True, "me", lots=2)
    e2e.db.execute("UPDATE user_instrument_real SET changed_at=changed_at-120")
    e2e.db.commit()
    e2e.sync(None)
    e2e.rt.handle([e2e.ev("ENTRY", "BUY")], {}, MON)                        # the paper event says quantity 30 (1 lot)
    await e2e.rt.tick(MON)
    [o] = places(e2e.fa)
    assert o["quantity"] == "60"


def test_the_person_picks_the_lots_and_only_a_typo_is_refused(e2e):
    from app.broker import store as st
    st.instrument_set(e2e.db, e2e.uid, IID, "BANKNIFTY", True, "me", lots=5)          # the person decides; no admin lot limit
    with pytest.raises(st.BrokerError):
        st.instrument_set(e2e.db, e2e.uid, IID, "BANKNIFTY", True, "me", lots=51)   # only a typo guard


def test_positions_list_marks_what_the_app_manages(db, new_client, monkeypatch):
    from app.broker import api as bapi, store as st
    from tests.security.helpers import make_user
    u = make_user(db, "posuser", "user")
    st.save(db, u.id, "A123456", "KEY12345", "1234", "JBSWY3DPEHPK3PXP", u.id)
    db.execute("UPDATE user_broker SET status='connected' WHERE user_id=?", (u.id,))
    db.commit()

    class B:
        async def positions(self):
            return [{"tradingsymbol": "BANKNIFTY25NOV26FUT", "exchange": "NFO", "producttype": "CARRYFORWARD", "netqty": "30", "netprice": "50000", "ltp": "50100", "pnl": "3000"},
                    {"tradingsymbol": "SBIN-EQ", "exchange": "NSE", "producttype": "DELIVERY", "netqty": "5", "netprice": "800", "ltp": "790", "pnl": "-50"}]

    async def bf(c, uid):
        return B()

    class RT:
        def view(self, c, uid):
            return [{"instrument_id": "i-bank", "position": {"contract": "BANKNIFTY25NOV26FUT"}, "stop_target": 49500.0}]

    from app.broker import tb_real
    monkeypatch.setattr(tb_real, "angel_broker_for", bf)
    monkeypatch.setattr(bapi, "_executor", lambda: RT())
    bapi._POS.clear()
    from tests.security.helpers import login
    cl = new_client()
    assert login(cl, u).status_code == 200
    d = cl.get("/api/me/broker/positions").json()
    assert d["total_pnl"] == 2950 and d["open"] == 2
    fut, eq = d["positions"]
    assert fut["app"] and fut["instrument_id"] == "i-bank" and fut["stop"] == 49500.0
    assert not eq["app"] and eq["instrument_id"] is None                                  # a manual stock position is shown, never managed


def test_margin_and_ticket_use_the_rolled_contract_not_the_one_the_instrument_was_created_with(monkeypatch):
    import types
    from app import main as appmain
    from app.broker import api as bapi
    inst = types.SimpleNamespace(id="i-bank", trading_symbol="BANKNIFTY27OCT26FUT", token="111", exch_seg="NFO", lot_size=30, lots=1, quantity=30)
    eng = types.SimpleNamespace(snapshots={"i-bank": {"instrument": {"trading_symbol": "BANKNIFTY23NOV26FUT", "token": "222", "exch_seg": "NFO", "lot_size": 30}}})
    monkeypatch.setattr(appmain, "engine", eng, raising=False)
    a = bapi._active(inst)
    assert a["trading_symbol"] == "BANKNIFTY23NOV26FUT" and a["token"] == "222" and a["lot_size"] == 30
    eng.snapshots = {}
    assert bapi._active(inst)["trading_symbol"] == "BANKNIFTY27OCT26FUT"              # no snapshot yet: the instrument's own contract


@async_test
async def test_a_position_the_person_holds_by_hand_in_the_same_contract_does_not_halt_the_app(e2e):
    fa, rt = e2e.fa, e2e.rt
    fa.orders["m1"] = {"tradingsymbol": CONTRACT, "transactiontype": "BUY", "quantity": "30", "status": "complete", "ordertype": "LIMIT", "variety": "NORMAL",
                       "orderid": "m1", "averageprice": "54794.4", "filledshares": "30"}          # the manual +30 bought days ago
    e2e.sync(None)
    rt.handle([e2e.ev("ENTRY", "BUY")], {}, MON)
    await rt.tick(MON)
    fa.fill([o for o in fa.open_orders() if o["orderid"] != "m1"][0]["orderid"], 50002.5)
    e2e.sync(PAPER_BUY)
    for t in (5, 15, 30, 45):
        await rt.tick(secs(t))
    assert fa.net(CONTRACT) == 60 and e2e.state()["halted"] is None                       # 30 by hand + 30 by the app: fine
    assert len(fa.open_orders("STOPLOSS_LIMIT")) == 1 and fa.open_orders("STOPLOSS_LIMIT")[0]["quantity"] == "30"   # the stop covers only the app's 30


@async_test
async def test_a_real_mismatch_still_halts_with_a_manual_position_present(e2e):
    fa, rt = e2e.fa, e2e.rt
    fa.orders["m1"] = {"tradingsymbol": CONTRACT, "transactiontype": "BUY", "quantity": "30", "status": "complete", "ordertype": "LIMIT", "variety": "NORMAL",
                       "orderid": "m1", "averageprice": "54794.4", "filledshares": "30"}
    e2e.sync(None)
    rt.handle([e2e.ev("ENTRY", "BUY")], {}, MON)
    await rt.tick(MON)
    fa.fill([o for o in fa.open_orders() if o["orderid"] != "m1"][0]["orderid"], 50002.5)
    e2e.sync(PAPER_BUY)
    await rt.tick(secs(5))
    fa.orders["m2"] = {**fa.orders["m1"], "orderid": "m2", "transactiontype": "SELL", "quantity": "60"}   # somebody sold 60 by hand
    for t in (10, 40):
        await rt.tick(secs(t))
    assert e2e.state()["halted"] and "held by hand" in e2e.state()["halted"]


def test_symbol_groups_for_real_orders():
    from app.broker import store as st
    assert st.symbol_allowed("BANKNIFTY", ["BANKNIFTY"]) and not st.symbol_allowed("NIFTY", ["BANKNIFTY"])
    assert st.symbol_allowed("GOLDM", ["MCX"]) and not st.symbol_allowed("GOLDM", ["STOCKS", "NIFTY"])
    assert st.symbol_allowed("RELIANCE", ["STOCKS"]) and not st.symbol_allowed("RELIANCE", ["MCX"])
    assert not st.symbol_allowed("NIFTY", ["STOCKS"])                                    # the index futures are only ever allowed by name
