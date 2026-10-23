"""Saved strategies and saved backtests are private to the user who made them. What existed before accounts belongs to the owner account."""
import json
import sqlite3

import pytest

from app.options import api as options_api, store
from tests.security.helpers import api, login, make_owner, make_user

STRAT = {"name": "S", "symbol": "NIFTY", "start": "2026-01-01", "end": "2026-02-01", "expiry": "weekly", "entry_days_before_expiry": 2, "exit_days_before_expiry": 0,
         "legs": [{"side": "SELL", "opt": "CE", "lots": 1, "strike": {"mode": "ATM", "offset": 0}}, {"side": "SELL", "opt": "PE", "lots": 1, "strike": {"mode": "ATM", "offset": 0}}]}


@pytest.fixture(autouse=True)
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(options_api, "STORE_DB", tmp_path / "strategies.db")
    monkeypatch.setattr(options_api, "SAVE_DIR", tmp_path / "runs")
    (tmp_path / "runs").mkdir()


def _login(new_client, acct):
    c = new_client()
    assert login(c, acct).status_code == 200
    return c


def _body(name="Straddle"):
    return {"name": name, "state": {"f-name": name}, "strategy": {**STRAT, "name": name}}


def test_strategies_are_private_and_names_are_per_user(db, new_client):
    a, b = make_user(db, "anil"), make_user(db, "bina")
    ca, cb = _login(new_client, a), _login(new_client, b)
    sid = api(ca, "POST", "/api/options/strategies", json=_body()).json()["id"]
    assert [s["name"] for s in ca.get("/api/options/strategies").json()] == ["Straddle"]
    assert cb.get("/api/options/strategies").json() == []
    assert cb.get(f"/api/options/strategies/{sid}").status_code == 404                   # not yours = does not exist
    assert api(cb, "DELETE", f"/api/options/strategies/{sid}").status_code == 404
    assert api(cb, "POST", "/api/options/strategies", json=_body()).status_code == 200    # the same name is free for another user
    assert api(ca, "POST", "/api/options/strategies", json=_body()).status_code == 409    # but not twice for the same user
    assert ca.get(f"/api/options/strategies/{sid}").status_code == 200


def _write_run(tmp_path, rid, **extra):
    run = {"id": rid, "strategy": {"name": "Run " + rid, "symbol": "NIFTY", "start": "2026-01-01", "end": "2026-02-01"}, "metrics": {"trades": 2, "total": 100.0, "max_drawdown": -5, "win_rate": 50},
           "trades": [], "skipped": [], **extra}
    (tmp_path / "runs" / f"{rid}.json").write_text(json.dumps(run), encoding="utf-8")


def test_backtests_are_private_and_old_runs_go_to_the_owner(db, new_client, tmp_path):
    owner, a, b = make_owner(db), make_user(db, "anil"), make_user(db, "bina")
    _write_run(tmp_path, "20261001-100000-aaaa", owner=a.id)
    _write_run(tmp_path, "20261001-110000-bbbb", owner=b.id)
    _write_run(tmp_path, "20260901-090000-0ld0")                                         # saved before accounts existed: no owner field
    co, ca, cb = _login(new_client, owner), _login(new_client, a), _login(new_client, b)
    assert [r["id"] for r in ca.get("/api/options/backtests").json()] == ["20261001-100000-aaaa"]
    assert [r["id"] for r in cb.get("/api/options/backtests").json()] == ["20261001-110000-bbbb"]
    assert [r["id"] for r in co.get("/api/options/backtests").json()] == ["20260901-090000-0ld0"]
    assert ca.get("/api/options/backtests/20261001-110000-bbbb").status_code == 404
    assert ca.get("/api/options/backtests/20261001-100000-aaaa").status_code == 200
    assert cb.get("/api/options/backtests/20260901-090000-0ld0").status_code == 404      # an old run is not public
    assert ca.get("/api/options/backtests/compare?ids=20261001-100000-aaaa,20261001-110000-bbbb").status_code == 404


def test_version_1_database_is_copied_not_rewritten_and_adopted_by_the_owner(db, tmp_path):
    old = tmp_path / "old.db"
    c = sqlite3.connect(old)
    c.executescript(store.SCHEMA)
    c.execute("INSERT INTO schema_version VALUES (1)")
    c.execute("INSERT INTO strategies VALUES ('id1','Old one','{}','{}','builder','t','t')")
    c.commit(); c.close()
    s = store.connect(old)
    assert [x["name"] for x in store.list_all(s, "")] == ["Old one"] and store.list_all(s, "someone") == []
    assert s.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 1                # the version-1 table is untouched
    assert s.execute("SELECT version FROM schema_version").fetchall()[0][0] == 2 and s.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
    assert store.adopt(s, "owner-id") == 1 and store.list_all(s, "") == [] and [x["name"] for x in store.list_all(s, "owner-id")] == ["Old one"]
    s.close()
    store.connect(old).close()                                                            # reopening is a no-op
