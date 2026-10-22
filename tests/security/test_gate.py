"""Which login is in force (open / legacy / platform), what stays public, CSRF, logout, and that the old login keeps working until the first
platform user exists."""
import pytest

from app import auth
from tests.security.helpers import api, login, make_user

LEGACY_PASSWORD = "legacy password 12345"


@pytest.fixture
def legacy_env(monkeypatch):
    monkeypatch.setenv("APP_USERNAME", "old")
    monkeypatch.setenv("APP_PASSWORD_HASH", auth.hash_password(LEGACY_PASSWORD, iterations=1000))
    monkeypatch.setenv("APP_SECRET", "x" * 32)


# ---- open (local use) ----
def test_with_no_login_configured_everything_is_open_as_before(client):
    assert client.get("/api/auth/mode").json() == {"mode": "open"}
    assert client.get("/top-bottom").status_code == 200
    assert client.get("/api/journal").status_code == 200


# ---- legacy (the old single-user login) ----
def test_the_old_login_still_works_until_the_first_platform_user_exists(db, new_client, legacy_env):
    c = new_client()
    assert c.get("/api/auth/mode").json() == {"mode": "legacy"}
    assert c.get("/top-bottom", follow_redirects=False).status_code == 303
    assert c.get("/api/journal").status_code == 401 and c.get("/api/journal").json() == {"detail": "login required"}
    assert c.post("/login", data={"username": "old", "password": "wrong"}, follow_redirects=False).headers["location"] == "/login?e=1"
    r = c.post("/login", data={"username": "old", "password": LEGACY_PASSWORD}, follow_redirects=False)
    assert r.status_code == 303 and c.get("/top-bottom").status_code == 200

    make_user(db)                                                         # the first platform user switches the whole app over
    assert c.get("/api/auth/mode").json() == {"mode": "platform"}
    assert c.get("/top-bottom", follow_redirects=False).status_code == 303   # the old cookie is no longer enough
    assert c.get("/api/journal").status_code == 401


# ---- platform ----
def test_in_platform_mode_pages_redirect_and_apis_answer_401(db, client):
    make_user(db)
    r = client.get("/top-bottom", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = client.get("/api/overview")
    assert r.status_code == 401 and r.json()["error"] == "LOGIN_REQUIRED" and r.json()["detail"] == "login required"


@pytest.mark.parametrize("path", ["/login", "/healthz", "/api/auth/mode", "/setup/anything", "/static/theme.css", "/static/shell.js"])
def test_the_pages_needed_to_sign_in_stay_public(db, client, path):
    make_user(db)
    assert client.get(path, follow_redirects=False).status_code == 200


def test_after_login_the_pages_open_and_me_returns_the_csrf_token(db, client):
    acct = make_user(db)
    login(client, acct)
    assert client.get("/top-bottom").status_code == 200
    me = client.get("/api/auth/me").json()
    assert me["csrf_token"] == client.cookies.get("algo_csrf") and me["permissions"] == ["session.self"] and me["user"]["owner"] is False


def test_state_changing_requests_need_the_csrf_header(db, client):
    acct = make_user(db)
    login(client, acct)
    beat = {"json": {"active": True}}
    assert client.post("/api/auth/heartbeat", **beat).status_code == 403                                   # no header
    assert client.post("/api/auth/heartbeat", headers={"X-CSRF-Token": "wrong"}, **beat).status_code == 403
    assert client.post("/api/auth/heartbeat", **beat).json()["error"] == "CSRF"
    assert api(client, "POST", "/api/auth/heartbeat", **beat).status_code == 200
    assert client.get("/api/auth/me").status_code == 200                                                    # reads never need it


def test_the_csrf_token_of_another_session_does_not_work(db, new_client):
    a, b = make_user(db, "rahul"), make_user(db, "asha")
    one, two = new_client(), new_client()
    login(one, a)
    login(two, b)
    stolen = two.cookies.get("algo_csrf")
    assert one.post("/api/auth/heartbeat", json={"active": True}, headers={"X-CSRF-Token": stolen}).status_code == 403


def test_logout_page_closes_the_session_and_clears_the_cookies(db, client):
    acct = make_user(db)
    login(client, acct)
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/api/auth/me").status_code == 401
    assert db.execute("SELECT end_reason FROM sessions").fetchone()[0] == "logout"
    assert client.get("/logout", follow_redirects=False).status_code == 303                                # logging out twice is harmless


def test_logout_api_is_audited(db, client):
    acct = make_user(db)
    login(client, acct)
    assert api(client, "POST", "/api/auth/logout").json() == {"ok": True}
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action='logout' AND actor_id=?", (acct.id,)).fetchone()[0] == 1


def test_a_removed_session_cookie_or_a_forged_one_gets_nothing(db, client):
    acct = make_user(db)
    login(client, acct)
    client.cookies.clear()
    forged = {"Cookie": "algo_sid=forged-token-" + "a" * 30}
    assert client.get("/api/auth/me", headers=forged).status_code == 401
    assert client.get("/top-bottom", headers=forged, follow_redirects=False).status_code == 303
    assert client.get("/top-bottom", follow_redirects=False).status_code == 303                            # and with no cookie at all


def test_the_page_script_sends_the_csrf_header_and_a_heartbeat():
    """The browser half: shell.js (loaded by every page) must add the header and ping the server, or every page would break after login."""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[2] / "web" / "shell.js").read_text(encoding="utf-8")
    assert "X-CSRF-Token" in js and "/api/auth/heartbeat" in js and "algo_csrf" in js and "30000" in js


# ---- a session the server no longer accepts must not leave cookies behind (they made signed-out pages redirect in a loop) ----
def test_a_dead_session_has_its_cookies_removed_on_the_401(db, client):
    acct = make_user(db)
    login(client, acct)
    db.execute("UPDATE sessions SET ended_at=1, end_reason='idle'")
    db.commit()
    r = client.get("/api/auth/me")
    assert r.status_code == 401 and r.json()["reason"] == "idle"
    cleared = {c.split("=")[0]: c for c in r.headers.get_list("set-cookie")}
    assert set(cleared) >= {"algo_sid", "algo_csrf"} and all("Max-Age=0" in v for v in cleared.values())
    assert "algo_csrf" not in client.cookies                                    # so the next page load starts clean


def test_a_dead_session_has_its_cookies_removed_on_a_page_redirect_too(db, client):
    acct = make_user(db)
    login(client, acct)
    db.execute("UPDATE sessions SET ended_at=1, end_reason='forced'")
    db.commit()
    r = client.get("/top-bottom", follow_redirects=False)
    assert r.status_code == 303 and any(c.startswith("algo_csrf=") and "Max-Age=0" in c for c in r.headers.get_list("set-cookie"))


def test_no_cookies_are_sent_back_when_the_request_carried_none(db, client):
    make_user(db)
    assert client.get("/api/auth/me").headers.get_list("set-cookie") == []


def test_signed_out_pages_never_start_the_heartbeat():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[2] / "web" / "shell.js").read_text(encoding="utf-8")
    guard = js.index("(login|setup")
    assert guard < js.index("beat()") and guard < js.index("window.fetch = function")


def test_any_api_401_sends_a_signed_out_page_to_login_with_the_reason():
    """A tab that was signed out (takeover, idle, force-logout) must go to the sign-in page whichever of its requests notices first, and carry the
    server's reason; otherwise the reason is lost once the first 401 has cleared the cookies."""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[2] / "web" / "shell.js").read_text(encoding="utf-8")
    import re
    assert re.search(r"if \(api\) \{\s*p\.then\(function \(r\) \{\s*if \(r\.status !== 401\) return;.*?leave\(b && b\.reason\)", js, re.S)   # the 401 handler is live, for every /api/ call
    assert 'u.pathname !== "/api/auth/login"' in js                                                     # ...except the sign-in call itself
    assert '"taken_over" ? "moved" : "ended"' in js
