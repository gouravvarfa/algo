"""Options HTTP API: simulator time-travel endpoints, backtest persistence/history/compare.
Rows are SYNTHETIC fixtures in a temp DB (the app itself only ever reads real NSE Bhavcopy data)."""
import functools

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.options import api, bhavcopy, engine
from app.options.strategy import DEFAULT_CHARGES

DAYS = ["2026-03-05", "2026-03-06", "2026-03-09", "2026-03-10", "2026-03-11"]
EXP1, EXP2 = "2026-03-10", "2026-03-17"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    with bhavcopy._conn(db) as c:
        for i, d in enumerate(DAYS):
            c.execute("INSERT INTO load_log VALUES (?,?,?,?,?,?)", (d, "loaded", 1, 0, "", "x"))
            und = 22500 + 40 * i
            for exp in (EXP1, EXP2):
                if exp < d:
                    continue
                for k in (22400.0, 22500.0, 22600.0):
                    for o in ("CE", "PE"):
                        px = 100.0 + (10 if exp == EXP2 else 0)
                        vol = 0 if (k, o, d) == (22600.0, "PE", "2026-03-06") else 500
                        c.execute("INSERT INTO options_eod VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (d, "NIFTY", exp, k, o, px, px + 5, px - 5, px, px, und, 1000 + int(k) % 7, 0, vol, 65, "x"))
    monkeypatch.setattr(api, "_db", lambda: bhavcopy._conn(db))
    monkeypatch.setattr(api.engine, "run", functools.partial(engine.run.__wrapped__ if hasattr(engine.run, "__wrapped__") else engine.run, db=db))
    monkeypatch.setattr(api, "SAVE_DIR", tmp_path / "runs")
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_sim_dates_are_the_loaded_trading_days(client):
    assert client.get("/api/options/sim/dates?symbol=NIFTY").json()["dates"] == DAYS


def test_sim_chain_defaults_to_nearest_expiry_and_honours_a_valid_one(client):
    c = client.get("/api/options/sim/chain?date=2026-03-05").json()
    assert c["expiry"] == EXP1 and c["expiries"] == [EXP1, EXP2] and c["lot_size"] == 65 and c["atm"] == 22500
    assert client.get(f"/api/options/sim/chain?date=2026-03-05&expiry={EXP2}").json()["expiry"] == EXP2


def test_sim_chain_after_an_expiry_falls_back_to_a_listed_expiry(client):
    c = client.get(f"/api/options/sim/chain?date=2026-03-11&expiry={EXP1}").json()   # EXP1 is gone on 11 Mar
    assert c["expiry"] == EXP2 and EXP1 not in c["expiries"]


def test_sim_chain_unknown_date_is_a_404_not_a_fabricated_chain(client):
    r = client.get("/api/options/sim/chain?date=2026-03-07")                         # Saturday, no data
    assert r.status_code == 404


def test_untraded_contract_has_no_calculated_iv(client):
    c = client.get("/api/options/sim/chain?date=2026-03-06").json()
    pe = next(r for r in c["rows"] if r["strike"] == 22600)["PE"]
    assert pe["volume"] == 0 and pe["iv"] is None and pe["delta"] is None


def _pos(**kw):
    p = {"side": "SELL", "opt": "CE", "strike": 22500, "expiry": EXP1, "lots": 1, "lot_size": 65, "entry": 120.0, "entry_date": "2026-03-05"}
    p.update(kw)
    return p


def test_sim_mark_values_positions_on_the_selected_date(client):
    r = client.post("/api/options/sim/mark", json={"date": "2026-03-09", "positions": [_pos()]}).json()
    assert r["positions"][0]["ltp"] == 100.0 and r["positions"][0]["pnl"] == pytest.approx((120 - 100) * 65)
    assert r["payoff"]["net_credit"] == pytest.approx(120 * 65)


def test_sim_mark_after_expiry_uses_settlement_at_that_days_index_close(client):
    r = client.post("/api/options/sim/mark", json={"date": "2026-03-11", "positions": [_pos()]}).json()
    und_exp = 22500 + 40 * DAYS.index(EXP1)
    assert r["positions"][0]["note"] == "settled at expiry" and r["positions"][0]["ltp"] == pytest.approx(und_exp - 22500)


def test_sim_mark_missing_contract_is_reported_not_filled(client):
    r = client.post("/api/options/sim/mark", json={"date": "2026-03-09", "positions": [_pos(strike=22700)]}).json()
    assert r["positions"][0]["ltp"] is None and r["positions"][0]["pnl"] is None and "missing" in r["positions"][0]["note"]
    assert client.post("/api/options/sim/mark", json={"date": "2026-03-07", "positions": [_pos()]}).status_code == 404


STRAT = {"name": "A", "symbol": "NIFTY", "start": "2026-03-01", "end": "2026-03-31", "entry_days_before_expiry": 2, "slippage_pct": 0,
         "legs": [{"side": "SELL", "opt": "CE", "lots": 1, "strike": {"mode": "ATM", "offset": 0}}], "charges": {k: 0 for k in DEFAULT_CHARGES}}


def test_backtest_validation_errors_are_422_and_nothing_is_saved(client, tmp_path):
    r = client.post("/api/options/backtest", json={**STRAT, "legs": []})
    assert r.status_code == 422 and "legs" in str(r.json())
    assert not (tmp_path / "runs").exists() or not list((tmp_path / "runs").glob("*.json"))


def test_backtests_are_persisted_listed_reopened_and_compared_without_overwrite(client):
    a = client.post("/api/options/backtest", json=STRAT).json()
    b = client.post("/api/options/backtest", json={**STRAT, "name": "B", "slippage_pct": 1.0}).json()
    assert a["id"] != b["id"] and a["metrics"]["trades"] == 1 and a["metrics"]["wins"] + a["metrics"]["losses"] == 1
    hist = client.get("/api/options/backtests").json()
    assert {h["id"] for h in hist} == {a["id"], b["id"]} and all("max_drawdown" in h and "created" in h for h in hist)
    again = client.get(f"/api/options/backtests/{a['id']}").json()
    assert again["metrics"]["total"] == a["metrics"]["total"]
    cmp = client.get(f"/api/options/backtests/compare?ids={a['id']},{b['id']}").json()
    assert set(cmp["differences"]) == {"name", "slippage_pct"} and cmp["same_range"]
    assert cmp["runs"][0]["metrics"]["total"] == a["metrics"]["total"]
    assert cmp["runs"][1]["metrics"]["total"] < cmp["runs"][0]["metrics"]["total"]          # slippage costs money
    assert client.get(f"/api/options/backtests/compare?ids={a['id']}").status_code == 400
    assert client.get(f"/api/options/backtests/compare?ids={a['id']},nope-1").status_code == 404
    assert client.get("/api/options/backtests/..%2Fsecret").status_code == 404


def test_empty_result_has_zero_trades_metrics(client):
    r = client.post("/api/options/backtest", json={**STRAT, "start": "2026-05-01", "end": "2026-05-31"}).json()
    assert r["metrics"] == {"trades": 0} and r["trades"] == []


# ---------------------------------------------------------------- P5: saved strategies
@pytest.fixture
def sclient(client, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "STORE_DB", tmp_path / "strategies.db")
    return client


def test_strategy_save_list_get_overwrite_delete(sclient):
    body = {"name": "My straddle", "state": {"f-name": "My straddle"}, "strategy": STRAT}
    r = sclient.post("/api/options/strategies", json=body)
    assert r.status_code == 200 and r.json()["created"]
    sid = r.json()["id"]
    assert sclient.post("/api/options/strategies", json=body).status_code == 409                 # no silent overwrite
    assert sclient.post("/api/options/strategies", json={**body, "name": "my STRADDLE"}).status_code == 409   # case-insensitive
    r2 = sclient.post("/api/options/strategies", json={**body, "overwrite": True, "strategy": {**STRAT, "slippage_pct": 1.0}}).json()
    assert r2["id"] == sid and not r2["created"] and r2["strategy"]["slippage_pct"] == 1.0
    assert [s["name"] for s in sclient.get("/api/options/strategies").json()] == ["My straddle"]
    assert sclient.get(f"/api/options/strategies/{sid}").json()["state"] == {"f-name": "My straddle"}
    assert sclient.delete(f"/api/options/strategies/{sid}").json() == {"deleted": sid}
    assert sclient.get(f"/api/options/strategies/{sid}").status_code == 404 and sclient.get("/api/options/strategies").json() == []


def test_strategy_save_validates_like_the_backtester(sclient):
    r = sclient.post("/api/options/strategies", json={"name": "bad", "state": {}, "strategy": {**STRAT, "legs": []}})
    assert r.status_code == 422 and "legs" in str(r.json())
    assert sclient.post("/api/options/strategies", json={"name": " ", "state": {}, "strategy": STRAT}).status_code == 422


def test_browser_import_copies_valid_items_and_never_overwrites(sclient):
    sclient.post("/api/options/strategies", json={"name": "Kept", "state": {"v": 1}, "strategy": STRAT})
    r = sclient.post("/api/options/strategies/import", json={"items": [
        {"name": "Kept", "state": {"v": 2}, "strategy": STRAT}, {"name": "New", "state": {}, "strategy": STRAT},
        {"name": "Broken", "state": {}, "strategy": {"legs": []}}]}).json()
    assert r["imported"] == ["New"] and {s["name"] for s in r["skipped"]} == {"Kept", "Broken"}
    kept = next(s for s in sclient.get("/api/options/strategies").json() if s["name"] == "Kept")
    assert sclient.get(f"/api/options/strategies/{kept['id']}").json()["state"] == {"v": 1}
    assert next(s for s in sclient.get("/api/options/strategies").json() if s["name"] == "New")["source"] == "browser-import"


def test_store_migration_is_idempotent_and_keeps_rows(tmp_path):
    from app.options import store
    db = tmp_path / "s.db"
    with store.connect(db) as c:
        store.save(c, "A", {}, STRAT)
    with store.connect(db) as c:                     # reopening re-runs the migration: no duplicate version, row kept
        assert c.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1 and [s["name"] for s in store.list_all(c)] == ["A"]
