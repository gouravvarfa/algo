"""Developer panel: function stats, circuit breakers, flags / kill switch, known-answer self-tests, incidents, health board, permissions,
and the engine's per-instrument isolation. No network; every test has its own empty platform database (see conftest.py)."""
import asyncio
import time

import pytest

from app.ops import circuit, flags, health, incidents, selftests, stats
from tests.security.helpers import api, login, make_owner, make_user


@pytest.fixture(autouse=True)
def clean_ops():
    stats.reset()
    circuit.reset()
    flags.reset_cache()
    yield
    stats.reset()
    circuit.reset()
    flags.reset_cache()


# ---- function stats ---------------------------------------------------------------------------------------------------------------------
def test_tracked_counts_calls_errors_and_keeps_the_return_value():
    @stats.tracked("t.ok")
    def ok(x):
        return x * 2

    @stats.tracked("t.bad")
    def bad():
        raise ValueError("boom")

    assert ok(21) == 42
    for _ in range(3):
        with pytest.raises(ValueError):
            bad()
    rows = {r["name"]: r for r in stats.snapshot()}
    assert rows["t.ok"]["calls_total"] == 1 and rows["t.ok"]["status"] == "OK" and rows["t.ok"]["errors_total"] == 0
    assert rows["t.bad"]["errors_total"] == 3 and rows["t.bad"]["status"] == "FAULTY" and "ValueError: boom" in rows["t.bad"]["last_error_msg"]


def test_tracked_works_on_async_functions():
    @stats.tracked("t.async")
    async def f():
        return 7

    assert asyncio.run(f()) == 7
    assert {r["name"]: r for r in stats.snapshot()}["t.async"]["calls_total"] == 1


def test_a_few_errors_among_many_calls_is_degraded_not_faulty():
    @stats.tracked("t.flaky")
    def f(fail):
        if fail:
            raise RuntimeError("x")

    for i in range(50):
        try:
            f(i in (3, 10, 20))
        except RuntimeError:
            pass
    assert {r["name"]: r for r in stats.snapshot()}["t.flaky"]["status"] == "DEGRADED"


# ---- circuit breaker --------------------------------------------------------------------------------------------------------------------
def test_circuit_opens_after_the_threshold_then_half_opens_then_closes():
    b = circuit.Breaker("t", threshold=3, window_s=60, cooldown_s=0.2)

    def fail():
        raise OSError("down")

    for _ in range(3):
        with pytest.raises(OSError):
            b.call(fail)
    assert b.state() == "OPEN"
    with pytest.raises(circuit.CircuitOpen):
        b.call(lambda: 1)                     # blocked at once: nothing is sent to the service
    time.sleep(0.25)
    assert b.state() == "HALF_OPEN"
    assert b.call(lambda: "fine") == "fine" and b.state() == "CLOSED"


def test_a_failed_trial_call_opens_the_circuit_again():
    b = circuit.Breaker("t", threshold=1, cooldown_s=0.1)
    with pytest.raises(OSError):
        b.call(lambda: (_ for _ in ()).throw(OSError("x")))
    time.sleep(0.15)
    with pytest.raises(OSError):
        b.call(lambda: (_ for _ in ()).throw(OSError("still down")))
    assert b.state() == "OPEN" and b.opened_count == 2


def test_errors_that_are_not_outages_do_not_count():
    b = circuit.Breaker("t", threshold=2)
    for _ in range(5):
        with pytest.raises(KeyError):
            b.call(lambda: {}["x"], count_if=lambda e: not isinstance(e, KeyError))
    assert b.state() == "CLOSED"


def test_forced_open_blocks_and_release_restores():
    b = circuit.Breaker("t")
    b.force("open")
    with pytest.raises(circuit.CircuitOpen):
        b.call(lambda: 1)
    b.force(None)
    assert b.call(lambda: 1) == 1


def test_async_breaker_and_the_on_open_hook():
    seen = []
    b = circuit.Breaker("t", threshold=2, on_open=lambda br: seen.append(br.name))

    async def fail():
        raise OSError("x")

    async def go():
        for _ in range(2):
            with pytest.raises(OSError):
                await b.acall(fail)

    asyncio.run(go())
    assert b.state() == "OPEN" and seen == ["t"]


# ---- flags / kill switch ----------------------------------------------------------------------------------------------------------------
def test_flags_defaults_scope_and_the_kill_switch(db):
    assert flags.get("feature.margin") is True and flags.engine_paused() is False
    flags.set_flag("engine.paused", True, "user:u1")
    assert flags.engine_paused("u1") is True and flags.engine_paused("u2") is False
    flags.set_flag("engines.paused", True)
    assert flags.engine_paused("u2") is True            # global kill switch stops everyone
    flags.clear_flag("engines.paused")
    assert flags.engine_paused("u2") is False


# ---- self-tests -------------------------------------------------------------------------------------------------------------------------
def test_every_known_answer_check_passes_on_the_real_code():
    res = selftests.run_all(report=False)
    assert res["failed"] == 0, [r for r in res["results"] if not r["ok"]]
    assert res["passed"] == len(selftests.CHECKS)


def test_a_wrong_answer_is_reported_as_faulty_with_the_difference(monkeypatch, db):
    monkeypatch.setattr(selftests, "CHECKS", [("demo.wrong", lambda: (False, 7.9656, 8.1))])
    res = selftests.run_all()
    assert res["failed"] == 1 and res["results"][0]["expected"] == "7.9656" and res["results"][0]["actual"] == "8.1"
    assert any(i["component"] == "selftest.demo.wrong" for i in incidents.recent())
    assert {r["name"]: r for r in stats.snapshot()}["selftest.demo.wrong"]["errors_total"] == 1


def test_a_check_that_crashes_is_a_failed_check_not_a_crashed_panel(monkeypatch, db):
    def boom():
        raise RuntimeError("inside the function under test")
    monkeypatch.setattr(selftests, "CHECKS", [("demo.crash", boom)])
    res = selftests.run_all(report=False)
    assert res["failed"] == 1 and "RuntimeError" in res["results"][0]["error"]


# ---- incidents --------------------------------------------------------------------------------------------------------------------------
def test_incidents_are_stored_grouped_and_resolvable(db):
    for _ in range(3):
        incidents.record("engine", "BANKNIFTY evaluation failed: ValueError", user_id="u1", auto_action="isolated")
    incidents.record("feed", "disconnected", severity="warning")
    g = {x["component"]: x for x in incidents.grouped()}
    assert g["engine"]["count"] == 3 and g["engine"]["users"] == ["u1"] and g["feed"]["count"] == 1
    first = incidents.recent(1)[0]["id"]
    incidents.resolve(first)
    assert next(i for i in incidents.recent() if i["id"] == first)["resolved_at"] is not None
    assert incidents.count_since(60) == 4


# ---- health board -----------------------------------------------------------------------------------------------------------------------
def test_health_board_has_every_tile_and_survives_a_broken_check(monkeypatch, db):
    def broken():
        raise RuntimeError("check bug")
    monkeypatch.setattr(health, "TILES", health.TILES + [broken])
    tiles = health.all_tiles()
    keys = {t["key"] for t in tiles}
    assert {"api", "database", "feed", "angel_rest", "engine", "collector", "users", "audit", "selftests", "functions", "incidents", "host"} <= keys
    bad = next(t for t in tiles if "check bug" in t["reason"])
    assert bad["status"] == "DOWN"                      # reported on the board; the other tiles are still there
    assert health.overall(tiles) == "DOWN"


def test_angel_tile_goes_down_when_its_circuit_opens(db):
    b = circuit.breaker("angelone.rest", threshold=1)
    with pytest.raises(OSError):
        b.call(lambda: (_ for _ in ()).throw(OSError("x")))
    assert health.angel_rest_tile()["status"] == "DOWN"


def test_audit_tile_detects_tampering(db):
    from app.db import audit
    audit.append(db, "a")
    audit.append(db, "b")
    assert health.audit_tile()["status"] == "OK"
    db.execute("DROP TRIGGER audit_log_no_update")
    db.execute("UPDATE audit_log SET action='tampered' WHERE id=1")
    db.commit()
    assert health.audit_tile()["status"] == "DOWN"


# ---- permissions + HTTP -----------------------------------------------------------------------------------------------------------------
def test_only_a_developer_reaches_the_developer_panel(db, new_client):
    owner, admin, user = make_owner(db), make_user(db, "adminx", "admin"), make_user(db, "userx", "user")
    anon = new_client()
    assert anon.get("/api/dev/health").status_code == 401
    c_user = new_client()
    assert login(c_user, user).status_code == 200
    assert c_user.get("/api/dev/health").status_code == 403
    c_admin = new_client()
    assert login(c_admin, admin).status_code == 200
    assert c_admin.get("/api/dev/health").status_code == 403 and c_admin.get("/api/dev/functions").status_code == 403
    assert c_admin.get("/api/admin/system-summary").status_code == 200       # the small widget IS for admins
    c_dev = new_client()
    assert login(c_dev, owner).status_code == 200
    body = c_dev.get("/api/dev/health").json()
    assert body["overall"] in ("OK", "DEGRADED", "DOWN") and len(body["tiles"]) >= 10
    assert c_dev.get("/api/admin/users").status_code == 200                   # developer also has every admin screen
    assert c_dev.get("/api/dev/functions").status_code == 200 and c_dev.get("/metrics").status_code == 200


def test_developer_actions_are_audited_and_take_effect(db, new_client):
    owner, user = make_owner(db), make_user(db, "rahul", "user")
    c = new_client()
    assert login(c, owner).status_code == 200
    r = api(c, "POST", "/api/dev/selftest/run")
    assert r.status_code == 200 and r.json()["failed"] == 0
    assert api(c, "POST", "/api/dev/circuits/angelone.rest", json={"action": "open"}).json()["state"] == "OPEN"
    assert api(c, "POST", "/api/dev/circuits/angelone.rest", json={"action": "release"}).json()["state"] == "CLOSED"
    assert api(c, "PUT", "/api/dev/flags", json={"key": "feature.margin", "value": False}).status_code == 200
    assert flags.get("feature.margin") is False
    assert api(c, "PUT", "/api/dev/flags", json={"key": "x", "value": True, "scope": "nonsense"}).status_code == 400
    assert api(c, "POST", f"/api/dev/users/{user.id}/pause-engine", json={"paused": True}).json()["paused"] is True
    assert flags.engine_paused(user.id) is True
    assert api(c, "POST", "/api/dev/engines/pause-all", json={"paused": True}).json()["engines_paused"] is True
    actions = [e["action"] for e in c.get("/api/admin/audit?limit=50").json()["entries"]]
    for a in ("dev.selftest", "dev.circuit.open", "dev.flag.set", "dev.engine.pause", "dev.kill_switch.on"):
        assert a in actions, a


def test_a_flag_change_needs_the_csrf_token(db, new_client):
    owner = make_owner(db)
    c = new_client()
    assert login(c, owner).status_code == 200
    assert c.put("/api/dev/flags", json={"key": "feature.margin", "value": False}).status_code == 403


def test_margin_switch_turns_the_calculator_off(db, new_client):
    import asyncio as aio
    from app.options import api as options_api
    flags.set_flag("feature.margin", False)
    out = aio.run(options_api.sim_margin(options_api.MarginReq(symbol="NIFTY", positions=[])))
    assert out["margin"] is None and "switched off" in out["note"]


# ---- engine isolation -------------------------------------------------------------------------------------------------------------------
def _engine_with(two_ids, tmp_path, monkeypatch):
    from app.paper_trading import engine as engine_module
    from app.paper_trading.engine import Instrument, PaperEngine
    monkeypatch.setattr(engine_module, "STATE_PATH", tmp_path / "watchlist.json")       # never read or write the real watchlist
    monkeypatch.setattr(engine_module, "JOURNAL_PATH", tmp_path / "journal.jsonl")

    class P:
        async def get_session(self):
            return object()

        def reset_session(self):
            pass

    e = PaperEngine(P())
    for i in two_ids:
        e.instruments[i] = Instrument(id=i, symbol=i, instrument_type="FUTURE", timeframe="1D", quantity=1, token="1", trading_symbol=i, exch_seg="NFO",
                                      from_date="2026-01-01T00:00:00", added_at="2026-01-01T00:00:00")
    return e


def test_one_failing_instrument_is_isolated_and_the_other_keeps_running(db, monkeypatch, tmp_path):
    e = _engine_with(["bad", "good"], tmp_path, monkeypatch)

    async def build(inst):
        if inst.id == "bad":
            raise ValueError("data glitch")
        return {"ok": inst.id}
    monkeypatch.setattr(e, "_build_snapshot", build)
    monkeypatch.setattr(e, "_journal", lambda *a, **k: None)
    asyncio.run(e.refresh_all())
    assert e.snapshots == {"good": {"ok": "good"}}
    assert "bad" in e.faults and "ValueError" in e.faults["bad"]["message"] and "good" not in e.faults
    assert any(i["component"] == "engine" and "data glitch" in i["message"] for i in incidents.recent())
    from app.ops import context
    monkeypatch.setattr(context, "engine", e)
    tile = health.engine_tile()
    assert tile["status"] == "DEGRADED" and "bad" in tile["reason"]          # visible on the board, not hidden


def test_kill_switch_stops_evaluation_and_keeps_the_last_snapshot(db, monkeypatch, tmp_path):
    e = _engine_with(["a"], tmp_path, monkeypatch)
    calls = []

    async def build(inst):
        calls.append(inst.id)
        return {"n": len(calls)}
    monkeypatch.setattr(e, "_build_snapshot", build)
    monkeypatch.setattr(e, "_journal", lambda *a, **k: None)
    asyncio.run(e.refresh_all())
    flags.set_flag("engines.paused", True)
    asyncio.run(e.refresh_all())
    assert calls == ["a"] and e.snapshots["a"] == {"n": 1}            # not evaluated again, last state kept
    flags.set_flag("engines.paused", False)
    asyncio.run(e.refresh_all())
    assert calls == ["a", "a"]


def test_a_fault_clears_when_the_instrument_recovers(db, monkeypatch, tmp_path):
    e = _engine_with(["x"], tmp_path, monkeypatch)
    state = {"fail": True}

    async def build(inst):
        if state["fail"]:
            raise RuntimeError("temporary")
        return {"ok": 1}
    monkeypatch.setattr(e, "_build_snapshot", build)
    monkeypatch.setattr(e, "_journal", lambda *a, **k: None)
    asyncio.run(e.refresh_all())
    assert "x" in e.faults
    state["fail"] = False
    asyncio.run(e.refresh_all())
    assert e.faults == {} and e.snapshots["x"] == {"ok": 1}
