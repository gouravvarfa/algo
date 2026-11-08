"""Exit now on the Top-Bottom page, end to end over HTTP against the fake Angel One: real goes off, the stop is cancelled, a LIMIT exit closes the position."""
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
