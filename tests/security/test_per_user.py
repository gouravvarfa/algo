"""Per-user data and the admin's read-only "view as": every user sees only their own instruments, journal, positions and settings; an admin
sees any lower-ranked user exactly as that user sees themselves, can change nothing, and every look is audited and visible to the user."""
import json

import pytest

from app import main
from app.ops import context
from app.paper_trading import engine as engine_module
from app.paper_trading.engine import Instrument, PaperEngine
from tests.security.helpers import api, login, make_owner, make_user


class _P:
    async def get_session(self):
        return object()

    def reset_session(self):
        pass


def _inst(id_, owner, symbol="BANKNIFTY"):
    return Instrument(id=id_, symbol=symbol, instrument_type="FUTURE", timeframe="1D", quantity=30, token="61466", trading_symbol=f"{symbol}23NOV26FUT",
                      exch_seg="NFO", from_date="2026-01-01T00:00:00", added_at="2026-01-01T00:00:00", owner=owner)


def _snap(direction=None, unrealized=0.0, realized=0.0):
    return {"ltp": 55000.0, "position": {"direction": direction, "entry_price": 55795.8} if direction else None, "waiting": False,
            "stats": {"live_trades": 1, "live_wins": 1, "live_realized": realized, "unrealized": unrealized, "replay_trades": 3, "replay_realized": 10.0},
            "trades": [], "candles": [], "markers": []}


@pytest.fixture
def eng(monkeypatch, tmp_path):
    monkeypatch.setattr(engine_module, "STATE_PATH", tmp_path / "watchlist.json")
    monkeypatch.setattr(engine_module, "JOURNAL_PATH", tmp_path / "journal.jsonl")
    monkeypatch.setattr(engine_module, "LEGACY_OWNER_PATH", tmp_path / "legacy_owner.txt")
    e = PaperEngine(_P())
    monkeypatch.setattr(main, "engine", e)
    monkeypatch.setattr(context, "engine", e)
    return e


def _login(new_client, acct):
    c = new_client()
    assert login(c, acct).status_code == 200
    return c


def test_each_user_sees_only_their_own_instruments(db, new_client, eng):
    a, b = make_user(db, "anil"), make_user(db, "bina")
    eng.instruments.update({"ia": _inst("ia", a.id), "ib": _inst("ib", b.id)})
    eng.snapshots.update({"ia": _snap("SELL", 100.0), "ib": _snap(None)})
    ca, cb = _login(new_client, a), _login(new_client, b)
    assert [i["instrument"]["id"] for i in ca.get("/api/overview").json()["items"]] == ["ia"]
    assert [i["instrument"]["id"] for i in cb.get("/api/overview").json()["items"]] == ["ib"]
    assert ca.get("/api/instruments/ia").status_code == 200
    assert ca.get("/api/instruments/ib").status_code == 404                     # somebody else's: indistinguishable from "does not exist"
    assert api(ca, "DELETE", "/api/instruments/ib").status_code == 404 and "ib" in eng.instruments
    assert api(cb, "DELETE", "/api/instruments/ib").status_code == 200 and "ib" not in eng.instruments


def test_the_journal_is_per_user_and_old_events_go_to_the_owner(db, new_client, eng, tmp_path):
    owner, user = make_owner(db), make_user(db, "anil")
    lines = [{"event": "ENTRY", "direction": "SELL", "owner": user.id, "at": "t1"},
             {"event": "ENTRY", "direction": "BUY", "owner": owner.id, "at": "t2"},
             {"event": "ENTRY", "direction": "BUY", "at": "t0-before-accounts"}]
    (tmp_path / "journal.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    eng.instruments["old"] = _inst("old", None)                                  # the watchlist from before user accounts
    co, cu = _login(new_client, owner), _login(new_client, user)
    mine_u = cu.get("/api/journal").json()
    assert [e["at"] for e in mine_u] == ["t1"]
    mine_o = co.get("/api/journal").json()                                       # the owner adopts the old watchlist and the old events
    assert sorted(e["at"] for e in mine_o) == ["t0-before-accounts", "t2"] and eng.instruments["old"].owner == owner.id


def test_sim_positions_are_stored_per_user_and_index(db, new_client):
    a, b = make_user(db, "anil"), make_user(db, "bina")
    ca, cb = _login(new_client, a), _login(new_client, b)
    legs = [{"side": "SELL", "opt": "CE", "strike": 23050, "lots": 1}]
    assert api(ca, "PUT", "/api/me/sim-positions?symbol=NIFTY", json={"positions": legs}).status_code == 200
    assert ca.get("/api/me/sim-positions?symbol=NIFTY").json()["positions"] == legs
    assert ca.get("/api/me/sim-positions?symbol=BANKNIFTY").json()["positions"] == []
    assert cb.get("/api/me/sim-positions?symbol=NIFTY").json()["positions"] == []
    assert api(ca, "PUT", "/api/me/sim-positions?symbol=DOGE", json={"positions": []}).status_code == 400
    assert ca.put("/api/me/sim-positions?symbol=NIFTY", json={"positions": []}).status_code == 403          # CSRF header required
    assert api(ca, "PUT", "/api/me/sim-positions?symbol=NIFTY", json={"positions": [{"x": "y" * 2000}] * 250}).status_code == 413


def test_settings_are_per_user(db, new_client):
    a, b = make_user(db, "anil"), make_user(db, "bina")
    ca, cb = _login(new_client, a), _login(new_client, b)
    assert api(ca, "PUT", "/api/me/settings/sim.live", json={"value": True}).status_code == 200
    assert ca.get("/api/me/settings").json()["settings"] == {"sim.live": True} and cb.get("/api/me/settings").json()["settings"] == {}
    assert api(ca, "PUT", "/api/me/settings/bad key!", json={"value": 1}).status_code == 400


def test_admin_views_a_user_read_only_and_it_is_audited_once(db, new_client, eng):
    owner, admin, user = make_owner(db), make_user(db, "adminx", "admin"), make_user(db, "anil")
    eng.instruments["ia"] = _inst("ia", user.id)
    eng.snapshots["ia"] = _snap("SELL", 4200.0)
    cu, ca = _login(new_client, user), None
    api(cu, "PUT", "/api/me/sim-positions?symbol=NIFTY", json={"positions": [{"strike": 23050}]})
    ca = _login(new_client, admin)
    ov = ca.get(f"/api/admin/users/{user.id}/overview").json()
    assert ov["viewing"]["username"] == "anil" and [i["instrument"]["id"] for i in ov["items"]] == ["ia"] and ov["summary"]["state"] == "SELL"
    assert ca.get(f"/api/admin/users/{user.id}/instruments/ia").status_code == 200
    assert ca.get(f"/api/admin/users/{user.id}/sim-positions?symbol=NIFTY").json()["positions"] == [{"strike": 23050}]
    ca.get(f"/api/admin/users/{user.id}/overview")                               # a second look inside 10 minutes: no second audit row
    views = [e for e in ca.get("/api/admin/audit?limit=100").json()["entries"] if e["action"] == "admin.view_as"]
    assert len(views) == 1 and views[0]["target_id"] == user.id
    seen = cu.get("/api/me/activity").json()["views"]                            # the user can see that their dashboard was opened
    assert [v["by"] for v in seen] == ["adminx"]


def test_view_as_is_read_only(db, new_client, eng):
    admin, user = make_user(db, "adminx", "admin"), make_user(db, "anil")
    eng.instruments["ia"] = _inst("ia", user.id)
    ca = _login(new_client, admin)
    for method, url in (("POST", f"/api/admin/users/{user.id}/overview"), ("PUT", f"/api/admin/users/{user.id}/sim-positions"),
                        ("DELETE", f"/api/admin/users/{user.id}/instruments/ia"), ("PUT", f"/api/admin/users/{user.id}/settings")):
        assert api(ca, method, url, json={}).status_code in (404, 405)
    assert "ia" in eng.instruments


def test_who_may_view_whom(db, new_client, eng):
    owner, dev2 = make_owner(db), make_user(db, "dev2", "developer", created_by="owner-id")
    admin, user = make_user(db, "adminx", "admin"), make_user(db, "anil")
    cu, ca, cd = _login(new_client, user), _login(new_client, admin), _login(new_client, dev2)
    assert cu.get(f"/api/admin/users/{user.id}/overview").status_code == 403           # a plain user has no view-as at all
    assert ca.get(f"/api/admin/users/{user.id}/overview").status_code == 200
    assert ca.get(f"/api/admin/users/{dev2.id}/overview").status_code == 403           # an admin cannot view a developer
    assert ca.get(f"/api/admin/users/{admin.id}/overview").status_code == 403           # nor a peer / themselves
    assert cd.get(f"/api/admin/users/{admin.id}/overview").status_code == 200           # a developer can view an admin
    assert ca.get("/api/admin/users/nope/overview").status_code == 404


def test_live_users_lists_state_pnl_and_faults(db, new_client, eng):
    admin, user = make_user(db, "adminx", "admin"), make_user(db, "anil")
    eng.instruments["ia"] = _inst("ia", user.id)
    eng.snapshots["ia"] = _snap("BUY", 1500.0, 200.0)
    ca = _login(new_client, admin)
    rows = {r["username"]: r for r in ca.get("/api/admin/live-users").json()["users"]}
    assert rows["anil"]["state"] == "BUY" and rows["anil"]["open_positions"] == 1 and rows["anil"]["unrealized"] == 1500.0 and rows["anil"]["can_view"] is True
    assert rows["adminx"]["can_view"] is False and rows["adminx"]["instruments"] == 0
    eng.faults["ia"] = {"message": "ValueError: boom", "since": "t"}
    assert {r["username"]: r for r in ca.get("/api/admin/live-users").json()["users"]}["anil"]["fault"] == "ValueError: boom"


def test_a_user_cannot_reach_the_admin_views(db, new_client, eng):
    a, b = make_user(db, "anil"), make_user(db, "bina")
    ca = _login(new_client, a)
    assert ca.get("/api/admin/live-users").status_code == 403
    assert ca.get(f"/api/admin/users/{b.id}/settings").status_code == 403
