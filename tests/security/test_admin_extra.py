import pytest

from app import admin_extra
from tests.security.helpers import api, login, make_owner, make_user


def _two(db, new_client):
    owner = make_owner(db)
    admin = make_user(db, "adminq", "admin", created_by=owner.id if hasattr(owner, "id") else "seed")
    user = make_user(db, "traderq", "user")
    c_admin, c_user = new_client(), new_client()
    assert login(c_admin, admin).status_code == 200 and login(c_user, user).status_code == 200
    return owner, admin, user, c_admin, c_user


def test_limits_are_set_by_an_admin_and_enforced_on_the_server(db, new_client):
    _, _, user, c_admin, c_user = _two(db, new_client)
    assert api(c_user, "PUT", f"/api/admin/users/{user.id}/limits", json={"max_lots": 1}).status_code == 403     # a user cannot set limits
    r = api(c_admin, "PUT", f"/api/admin/users/{user.id}/limits", json={"max_lots": 2, "allowed_symbols": ["NIFTY"]})
    assert r.status_code == 200 and r.json()["max_lots"] == 2
    assert c_user.get("/api/me/limits").json()["allowed_symbols"] == ["NIFTY"]
    ok = api(c_user, "PUT", "/api/me/sim-positions?symbol=NIFTY", json={"positions": [{"lots": 2}]})
    assert ok.status_code == 200
    assert api(c_user, "PUT", "/api/me/sim-positions?symbol=NIFTY", json={"positions": [{"lots": 3}]}).status_code == 403
    assert api(c_user, "PUT", "/api/me/sim-positions?symbol=BANKNIFTY", json={"positions": [{"lots": 1}]}).status_code == 403
    assert api(c_user, "PUT", "/api/me/sim-positions?symbol=BANKNIFTY", json={"positions": []}).status_code == 200      # clearing is always allowed
    assert api(c_admin, "PUT", f"/api/admin/users/{user.id}/limits", json={"allowed_symbols": ["SENSEX"]}).status_code == 400


def test_notes_are_admin_only_and_audited(db, new_client):
    _, _, user, c_admin, c_user = _two(db, new_client)
    assert c_user.get(f"/api/admin/users/{user.id}/notes").status_code == 403
    r = api(c_admin, "POST", f"/api/admin/users/{user.id}/notes", json={"note": "asked for a limit raise"})
    assert r.status_code == 200 and r.json()["notes"][0]["note"] == "asked for a limit raise"
    acts = [e["action"] for e in c_admin.get("/api/admin/audit?limit=20").json()["entries"]]
    assert "user.note" in acts


def test_csv_exports_neutralise_formulas_and_are_audited(db, new_client):
    _, _, user, c_admin, c_user = _two(db, new_client)
    assert c_user.get("/api/admin/audit.csv").status_code == 403
    csv_text = c_admin.get("/api/admin/users.csv").text
    assert csv_text.splitlines()[0].startswith("username,name,role") and "traderq" in csv_text
    assert admin_extra._cell("=SUM(A1)") == "'=SUM(A1)" and admin_extra._cell("ok") == "ok"
    assert "login.success" in c_admin.get("/api/admin/audit.csv?q=login").text
    acts = [e["action"] for e in c_admin.get("/api/admin/audit?limit=20").json()["entries"]]
    assert "export.users" in acts and "export.audit" in acts


def test_trends_bulk_and_announcement(db, new_client):
    owner, _, user, c_admin, c_user = _two(db, new_client)
    t = c_admin.get("/api/admin/trends?days=14").json()
    assert len(t["days"]) == 14 and sum(t["logins"]) >= 2 and len(t["active_users"]) == 14
    r = api(c_admin, "POST", "/api/admin/users/bulk", json={"ids": [user.id, "nope"], "action": "disable"}).json()
    assert user.id in r["done"] and r["failed"][0]["id"] == "nope"
    assert api(c_admin, "POST", "/api/admin/users/bulk", json={"ids": [user.id], "action": "explode"}).status_code == 400
    c_new = new_client()
    assert login(c_new, owner).status_code == 200
    assert api(c_new, "PUT", "/api/admin/announcement", json={"message": "Maintenance at 10 pm", "level": "warn"}).status_code == 200
    assert c_new.get("/api/announcement").json()["announcement"]["message"] == "Maintenance at 10 pm"
    assert api(c_new, "DELETE", "/api/admin/announcement").json()["announcement"] is None
    assert c_admin.get("/api/admin/security-policy").json()["one_device"] is True


def test_ip_allow_list_only_blocks_admin_and_developer_apis(db, new_client, monkeypatch):
    owner = make_owner(db)
    c = new_client()
    assert login(c, owner).status_code == 200
    monkeypatch.setenv("ADMIN_ALLOWED_IPS", "10.0.0.0/8")
    assert not admin_extra.ip_allowed("203.0.113.5") and admin_extra.ip_allowed("10.1.2.3")
    assert c.get("/api/admin/users").status_code == 403 and c.get("/api/dev/health").status_code == 403
    assert c.get("/api/auth/me").status_code == 200                                    # the trading app is unaffected
    monkeypatch.delenv("ADMIN_ALLOWED_IPS")
    assert c.get("/api/admin/users").status_code == 200


def test_admin_can_change_an_accounts_data_and_the_user_sees_it(db, new_client):
    _, _, user, c_admin, c_user = _two(db, new_client)
    r = api(c_admin, "PATCH", f"/api/admin/users/{user.id}/profile", json={"name": "Trader Q", "email": "q@example.com"})
    assert r.status_code == 200 and r.json()["name"] == "Trader Q"
    assert api(c_admin, "PATCH", f"/api/admin/users/{user.id}/profile", json={"username": "x"}).status_code == 400
    assert api(c_admin, "PATCH", f"/api/admin/users/{user.id}/profile", json={"username": "adminq"}).status_code == 409       # taken
    assert api(c_user, "PATCH", f"/api/admin/users/{user.id}/profile", json={"name": "me"}).status_code == 403                  # a user cannot
    assert api(c_admin, "PUT", f"/api/admin/users/{user.id}/sim-positions?symbol=NIFTY", json={"positions": [{"lots": 1}]}).status_code == 200
    assert c_user.get("/api/me/sim-positions?symbol=NIFTY").json()["positions"] == [{"lots": 1}]
    assert api(c_admin, "PUT", f"/api/admin/users/{user.id}/settings/live", json={"value": True}).status_code == 200
    assert c_user.get("/api/me/settings").json()["settings"]["live"] is True
    seen = {v["what"] for v in c_user.get("/api/me/activity").json()["views"]}
    assert {"admin.edit_profile", "admin.edit_positions", "admin.edit_setting"} <= seen
    # an admin cannot change another admin or a developer
    owner_row = db.execute("SELECT id FROM users WHERE role='developer'").fetchone()
    assert api(c_admin, "PATCH", f"/api/admin/users/{owner_row['id']}/profile", json={"name": "x"}).status_code == 403
