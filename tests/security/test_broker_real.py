"""Real mode, phase 2: nothing is real until the owner switched the platform on AND an admin allowed the account AND the login was verified AND the person confirmed
with their password. The master switch is a kill switch. All data is made up; no broker is called."""
import pytest

from app.broker import api as broker_api, store
from app.ops import flags
from tests.security.helpers import PASSWORD, api, login, make_owner, make_user

BODY = {"client_code": "A123456", "api_key": "KEY12345", "pin": "1234", "totp_secret": "JBSWY3DPEHPK3PXP"}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    flags._cache = None
    broker_api._bad_pw.clear()
    yield
    flags._cache = None


def _setup(db, new_client):
    owner = make_owner(db)
    admin = make_user(db, "adminr", "admin", created_by=owner.id)
    user = make_user(db, "traderr", "user")
    c_owner, c_admin, c_user = new_client(), new_client(), new_client()
    for c, a in ((c_owner, owner), (c_admin, admin), (c_user, user)):
        assert login(c, a).status_code == 200
    api(c_user, "PUT", "/api/me/broker", json=BODY)
    db.execute("UPDATE user_broker SET status='connected' WHERE user_id=?", (user.id,))      # as if Test connection had passed
    db.commit()
    return owner, admin, user, c_owner, c_admin, c_user


def _grant(c_admin, user, **kw):
    return api(c_admin, "PUT", f"/api/admin/users/{user.id}/broker/real", json={"allowed": True, "max_lots": 1, **kw})


def test_real_is_blocked_until_every_condition_holds_and_each_reason_is_shown(db, new_client):
    owner, admin, user, c_owner, c_admin, c_user = _setup(db, new_client)
    me = c_user.get("/api/me/broker").json()
    assert me["effective_mode"] == "paper" and len(me["real_blockers"]) == 2                                  # master off + not allowed
    r = api(c_user, "PUT", "/api/me/broker/mode", json={"mode": "real", "password": PASSWORD})
    assert r.status_code == 403 and r.json()["error"] == "REAL_BLOCKED"
    assert _grant(c_admin, user).status_code == 200
    assert api(c_user, "PUT", "/api/me/broker/mode", json={"mode": "real", "password": PASSWORD}).status_code == 403   # master still off
    assert api(c_owner, "POST", "/api/admin/broker/master", json={"enabled": True}).status_code == 200
    me = c_user.get("/api/me/broker").json()
    assert me["real_blockers"] == [] and me["mode"] == "paper"
    r = api(c_user, "PUT", "/api/me/broker/mode", json={"mode": "real", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["mode"] == "real" and r.json()["effective_mode"] == "real"
    acts = [e["action"] for e in c_admin.get("/api/admin/audit?limit=30").json()["entries"]]
    assert {"broker.grant", "broker.master", "broker.mode"} <= set(acts)


def test_a_wrong_password_is_refused_and_repeated_ones_lock_the_switch(db, new_client):
    owner, admin, user, c_owner, c_admin, c_user = _setup(db, new_client)
    _grant(c_admin, user)
    api(c_owner, "POST", "/api/admin/broker/master", json={"enabled": True})
    for _ in range(5):
        r = api(c_user, "PUT", "/api/me/broker/mode", json={"mode": "real", "password": "nope"})
        assert r.status_code == 403 and r.json()["error"] == "BAD_PASSWORD"
    assert api(c_user, "PUT", "/api/me/broker/mode", json={"mode": "real", "password": PASSWORD}).status_code == 429
    assert c_user.get("/api/me/broker").json()["mode"] == "paper"


def test_the_kill_switch_puts_everyone_back_to_paper_and_only_the_owner_can_turn_it_on(db, new_client):
    owner, admin, user, c_owner, c_admin, c_user = _setup(db, new_client)
    _grant(c_admin, user)
    assert api(c_admin, "POST", "/api/admin/broker/master", json={"enabled": True}).status_code == 403     # an admin cannot switch real ON
    api(c_owner, "POST", "/api/admin/broker/master", json={"enabled": True})
    assert api(c_user, "PUT", "/api/me/broker/mode", json={"mode": "real", "password": PASSWORD}).status_code == 200
    r = api(c_admin, "POST", "/api/admin/broker/master", json={"enabled": False})                         # any admin can kill it
    assert r.status_code == 200 and r.json()["accounts_back_to_paper"] == 1
    me = c_user.get("/api/me/broker").json()
    assert me["mode"] == "paper" and me["effective_mode"] == "paper" and me["real_master_on"] is False
    ov = c_admin.get("/api/admin/broker/overview").json()
    assert ov["master_on"] is False and ov["counts"]["real_now"] == 0 and ov["can_turn_on"] is False


def test_withdrawing_the_grant_returns_the_account_to_paper_and_limits_are_checked(db, new_client):
    owner, admin, user, c_owner, c_admin, c_user = _setup(db, new_client)
    api(c_owner, "POST", "/api/admin/broker/master", json={"enabled": True})
    _grant(c_admin, user, max_lots=2, max_daily_loss=5000, allowed_symbols=["NIFTY"])
    me = c_user.get("/api/me/broker").json()
    assert me["max_lots"] == 2 and me["max_daily_loss"] == 5000 and me["allowed_symbols"] == ["NIFTY"]
    api(c_user, "PUT", "/api/me/broker/mode", json={"mode": "real", "password": PASSWORD})
    r = api(c_admin, "PUT", f"/api/admin/users/{user.id}/broker/real", json={"allowed": False, "max_lots": 2})
    assert r.status_code == 200 and r.json()["mode"] == "paper" and r.json()["effective_mode"] == "paper"
    assert _grant(c_admin, user, allowed_symbols=["SENSEX"]).status_code == 400
    assert api(c_admin, "PUT", f"/api/admin/users/{user.id}/broker/real", json={"allowed": True, "max_lots": 99}).status_code == 422
    assert api(c_user, "PUT", f"/api/admin/users/{user.id}/broker/real", json={"allowed": True, "max_lots": 1}).status_code == 403      # a user cannot grant themselves


def test_a_failed_login_check_makes_the_account_paper_even_if_real_was_chosen(db, new_client):
    owner, admin, user, c_owner, c_admin, c_user = _setup(db, new_client)
    api(c_owner, "POST", "/api/admin/broker/master", json={"enabled": True})
    _grant(c_admin, user)
    api(c_user, "PUT", "/api/me/broker/mode", json={"mode": "real", "password": PASSWORD})
    store.set_status(db, user.id, "error", "Invalid totp")
    me = c_user.get("/api/me/broker").json()
    assert me["mode"] == "real" and me["effective_mode"] == "paper" and any("login" in b for b in me["real_blockers"])


def test_paper_can_always_be_chosen_without_a_password(db, new_client):
    owner, admin, user, c_owner, c_admin, c_user = _setup(db, new_client)
    r = api(c_user, "PUT", "/api/me/broker/mode", json={"mode": "paper"})
    assert r.status_code == 200 and r.json()["mode"] == "paper"
    assert api(c_user, "PUT", "/api/me/broker/mode", json={"mode": "banana"}).status_code in (400, 403)
