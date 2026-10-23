"""Roles and permissions, and the admin's user management: create / list / disable / force-logout / reset TOTP / remove (PDF 2, 3.1, 4)."""
import pyotp
import pytest

from app.db import audit
from app.security import authz, users
from app.security.users import AuthError
from tests.security.helpers import PASSWORD, Account, api, login, make_owner, make_user, totp_now


@pytest.fixture
def world(db, new_client):
    """An owner, an admin made by the owner, and two plain users, each already signed in on their own browser."""
    owner = make_owner(db)
    admin = make_user(db, "boss", role="admin", created_by=owner.id)
    rahul, asha = make_user(db, "rahul", created_by=admin.id), make_user(db, "asha", created_by=admin.id)
    clients = {}
    for acct in (owner, admin, rahul, asha):
        clients[acct.username] = new_client()
        assert login(clients[acct.username], acct).status_code == 200
    return {"owner": owner, "admin": admin, "rahul": rahul, "asha": asha, "c": clients}


# ---- permission map ----
def test_roles_are_cumulative():
    assert authz.PERMISSIONS["user"] < authz.PERMISSIONS["admin"] < authz.PERMISSIONS["developer"]
    assert not authz.has("user", "users.create") and authz.has("admin", "users.create") and authz.has("developer", "users.manage")
    assert not authz.has("admin", "system.read") and authz.has("developer", "system.read")
    assert not authz.has("nonsense", "session.self")


def test_who_can_create_and_manage_whom():
    owner = {"id": "o", "role": "developer", "created_by": None}
    dev = {"id": "d", "role": "developer", "created_by": "o"}
    admin = {"id": "a", "role": "admin", "created_by": "o"}
    user = {"id": "u", "role": "user", "created_by": "a"}
    assert authz.is_owner(owner) and not authz.is_owner(dev)
    assert authz.can_create_role(owner, "developer") and authz.can_create_role(owner, "admin") and authz.can_create_role(owner, "user")
    assert authz.can_create_role(admin, "user") and not authz.can_create_role(admin, "admin") and authz.can_create_role(dev, "admin") and not authz.can_create_role(dev, "developer")
    assert not authz.can_create_role(user, "user")
    assert authz.can_manage(owner, dev) and authz.can_manage(dev, admin) and authz.can_manage(admin, user)
    assert not authz.can_manage(admin, dev) and not authz.can_manage(admin, {"id": "a2", "role": "admin"}) and not authz.can_manage(user, user | {"id": "u2"})
    assert not authz.can_manage(owner, owner) and not authz.can_manage(admin, admin)                  # never yourself


# ---- who may call what (acceptance test 8) ----
def test_a_normal_user_calling_admin_endpoints_gets_403(world):
    c = world["c"]["rahul"]
    assert c.get("/api/admin/users").status_code == 403
    assert c.get("/api/admin/audit").status_code == 403
    r = api(c, "POST", "/api/admin/users", json={"name": "X", "username": "xavier"})
    assert r.status_code == 403 and r.json()["error"] == "FORBIDDEN"


def test_nobody_logged_out_gets_anything_but_401(new_client, world):
    anon = new_client()
    for method, url in (("GET", "/api/admin/users"), ("POST", "/api/admin/users"), ("GET", "/api/admin/audit"), ("GET", "/api/auth/me")):
        assert anon.request(method, url).status_code == 401


def test_admin_can_list_users_and_sees_who_is_online_and_on_what_device(world):
    rows = {u["username"]: u for u in world["c"]["boss"].get("/api/admin/users").json()["users"]}
    assert set(rows) == {"owner", "boss", "rahul", "asha"}
    assert rows["rahul"]["online"] is True and rows["rahul"]["device"] == "Chrome · Windows" and rows["rahul"]["last_active"]
    assert rows["rahul"]["role"] == "user" and rows["rahul"]["status"] == "ACTIVE"
    assert not any("password" in k or "totp" in k for k in rows["rahul"])                      # no secrets in the list


# ---- creating users end to end (PDF 3.1) ----
def test_admin_creates_a_user_who_sets_up_and_logs_in(world, new_client, db):
    admin = world["c"]["boss"]
    r = api(admin, "POST", "/api/admin/users", json={"name": "Meera", "username": "meera", "email": "m@example.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["status"] == "INVITED" and body["expires_in_hours"] == 24
    assert body["setup_url"].endswith(body["setup_path"]) and body["setup_path"].startswith("/setup/")
    token = body["setup_path"].rsplit("/", 1)[1]
    assert token not in str(db.execute("SELECT * FROM user_invites").fetchall())

    meera = new_client()
    info = meera.get(f"/api/auth/setup/{token}").json()
    secret = info["secret"].replace(" ", "")
    done = meera.post(f"/api/auth/setup/{token}", json={"password": PASSWORD, "totp": pyotp.TOTP(secret).now()})
    assert done.status_code == 200 and len(done.json()["backup_codes"]) == 10
    assert meera.get(f"/api/auth/setup/{token}").status_code == 404                              # the link is spent
    acct = Account("", "meera", PASSWORD, secret, "user")
    assert login(meera, acct, code=totp_now(secret, +1)).status_code == 200                      # the next 30 s code: this one was used by setup
    assert meera.get("/api/auth/me").json()["user"]["role"] == "user"


def test_a_duplicate_username_is_refused(world):
    r = api(world["c"]["boss"], "POST", "/api/admin/users", json={"name": "Again", "username": "RAHUL"})
    assert r.status_code == 409 and r.json()["error"] == "USERNAME_TAKEN"


def test_only_the_owner_can_create_admins_and_developers(world):
    for who, expected in (("boss", 403), ("owner", 200)):
        for role in ("admin", "developer"):
            r = api(world["c"][who], "POST", "/api/admin/users", json={"name": "N", "username": f"{who}-{role}", "role": role})
            assert r.status_code == expected, (who, role, r.text)


# ---- managing users ----
def test_disabling_a_user_ends_their_session_at_once_and_blocks_login(world, new_client):
    boss, rahul, acct = world["c"]["boss"], world["c"]["rahul"], world["rahul"]
    assert rahul.get("/api/auth/me").status_code == 200
    r = api(boss, "PATCH", f"/api/admin/users/{acct.id}", json={"status": "DISABLED"})
    assert r.status_code == 200 and r.json()["user"]["status"] == "DISABLED"
    assert rahul.get("/api/auth/me").status_code == 401
    assert login(new_client(), acct, code=acct.next_backup()).status_code == 401
    api(boss, "PATCH", f"/api/admin/users/{acct.id}", json={"status": "ACTIVE"})
    assert login(new_client(), acct, code=acct.next_backup()).status_code == 200


def test_force_logout_ends_the_session(world, db):
    r = api(world["c"]["boss"], "POST", f"/api/admin/users/{world['rahul'].id}/force-logout")
    assert r.status_code == 200 and r.json()["sessions_ended"] == 1
    assert world["c"]["rahul"].get("/api/auth/me").status_code == 401
    assert db.execute("SELECT end_reason FROM sessions WHERE user_id=?", (world["rahul"].id,)).fetchone()[0] == "forced"


def test_reset_totp_closes_sessions_blocks_login_and_a_new_link_restores_access(world, new_client):
    boss, acct = world["c"]["boss"], world["rahul"]
    r = api(boss, "POST", f"/api/admin/users/{acct.id}/reset-totp")
    assert r.status_code == 200 and r.json()["user"]["status"] == "INVITED"
    assert world["c"]["rahul"].get("/api/auth/me").status_code == 401
    assert login(new_client(), acct, code=acct.next_backup()).status_code == 401                 # old backup codes are gone too
    token = r.json()["setup_path"].rsplit("/", 1)[1]
    phone = new_client()
    secret = phone.get(f"/api/auth/setup/{token}").json()["secret"].replace(" ", "")
    assert phone.post(f"/api/auth/setup/{token}", json={"password": "a brand new passphrase", "totp": pyotp.TOTP(secret).now()}).status_code == 200
    again = Account(acct.id, "rahul", "a brand new passphrase", secret, "user")
    assert login(new_client(), again, code=totp_now(secret, +1)).status_code == 200
    assert login(new_client(), acct).status_code == 401                                            # the old secret and old password are dead


def test_removed_user_disappears_cannot_log_in_and_the_name_stays_reserved(world, new_client):
    boss, acct = world["c"]["boss"], world["asha"]
    assert api(boss, "DELETE", f"/api/admin/users/{acct.id}").status_code == 200
    assert "asha" not in {u["username"] for u in boss.get("/api/admin/users").json()["users"]}
    assert world["c"]["asha"].get("/api/auth/me").status_code == 401
    assert login(new_client(), acct, code=acct.next_backup()).status_code == 401
    assert api(boss, "POST", "/api/admin/users", json={"name": "Asha 2", "username": "asha"}).status_code == 409    # soft delete: history keeps its owner


def test_nobody_can_remove_or_disable_themselves(world):
    for who in ("boss", "owner"):
        me = world[who].id if who != "boss" else world["admin"].id
        c = world["c"][who]
        assert api(c, "PATCH", f"/api/admin/users/{me}", json={"status": "DISABLED"}).status_code == 403
        assert api(c, "DELETE", f"/api/admin/users/{me}").status_code == 403
        assert api(c, "POST", f"/api/admin/users/{me}/force-logout").status_code == 403


def test_an_admin_cannot_touch_the_owner_or_another_admin_but_the_owner_can_manage_the_admin(world):
    boss = world["c"]["boss"]
    assert api(boss, "PATCH", f"/api/admin/users/{world['owner'].id}", json={"status": "DISABLED"}).status_code == 403
    assert api(boss, "DELETE", f"/api/admin/users/{world['owner'].id}").status_code == 403
    assert api(world["c"]["owner"], "POST", f"/api/admin/users/{world['admin'].id}/force-logout").status_code == 200


def test_unknown_user_id_is_404_and_bad_status_is_400(world):
    boss = world["c"]["boss"]
    assert api(boss, "PATCH", "/api/admin/users/nope", json={"status": "DISABLED"}).status_code == 404
    assert api(boss, "PATCH", f"/api/admin/users/{world['rahul'].id}", json={"status": "SUPERUSER"}).status_code == 400
    assert api(boss, "PATCH", f"/api/admin/users/{world['rahul'].id}", json={"status": "ACTIVE"}).status_code == 409        # already active


def test_the_last_active_admin_can_never_be_removed(db):
    """Defence in depth: a manager is normally an admin/developer themselves, so this guard only matters if every other one is gone."""
    only = make_user(db, "solo", role="admin", created_by="x")
    ghost_owner = {"id": "not-in-the-database", "role": "developer", "created_by": None}
    for call in (lambda: users.delete_user(db, ghost_owner, only.id), lambda: users.set_status(db, ghost_owner, only.id, "DISABLED"),
                 lambda: users.reset_totp(db, ghost_owner, only.id)):
        with pytest.raises(AuthError) as e:
            call()
        assert e.value.code == "LAST_ADMIN" and e.value.status == 409
    assert users.get_user(db, only.id)["status"] == "ACTIVE"


# ---- the audit log ----
def test_actions_are_audited_readable_by_admin_and_the_chain_verifies(world, db):
    boss = world["c"]["boss"]
    api(boss, "POST", "/api/admin/users", json={"name": "Meera", "username": "meera"})
    api(boss, "POST", f"/api/admin/users/{world['rahul'].id}/force-logout")
    api(boss, "PATCH", f"/api/admin/users/{world['asha'].id}", json={"status": "DISABLED"})
    entries = boss.get("/api/admin/audit?limit=50").json()["entries"]
    actions = [e["action"] for e in entries]
    assert {"login.success", "user.create", "user.force_logout", "user.disable"} <= set(actions)
    assert entries[0]["id"] > entries[-1]["id"]                                                   # newest first
    created = next(e for e in entries if e["action"] == "user.create")
    assert created["actor_id"] == world["admin"].id and created["actor_role"] == "admin" and "meera" in created["detail_json"]
    assert "setup" not in created["detail_json"]                                                  # the link is never logged
    assert audit.verify_chain(db) == (True, None)
    page2 = boss.get(f"/api/admin/audit?limit=2&before_id={entries[2]['id']}").json()["entries"]
    assert all(e["id"] < entries[2]["id"] for e in page2) and len(page2) == 2
