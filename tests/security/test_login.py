"""Daily login: password + TOTP, generic errors, replay protection, backup codes, and the 5-failures / 15-minutes lock (PDF 3.2)."""
import time

import pytest

from app.db import audit
from app.security import ratelimit
from tests.security.helpers import api, login, make_user, totp_now


def test_login_succeeds_and_sets_a_server_side_session_cookie(db, client):
    acct = make_user(db)
    r = login(client, acct)
    assert r.status_code == 200 and r.json()["user"]["username"] == "rahul"
    cookies = r.headers.get_list("set-cookie")
    sid = next(c for c in cookies if c.startswith("algo_sid="))
    assert "HttpOnly" in sid and "SameSite=strict" in sid and "Max-Age=43200" in sid
    csrf = next(c for c in cookies if c.startswith("algo_csrf="))
    assert "HttpOnly" not in csrf and "SameSite=strict" in csrf          # the page script must be able to read this one
    token = client.cookies.get("algo_sid")
    assert len(token) >= 40
    assert token not in str(db.execute("SELECT * FROM sessions").fetchall()[0][:])      # the database holds only a hash of the cookie value
    assert client.get("/api/auth/me").json()["user"]["username"] == "rahul"


def test_wrong_password_wrong_code_and_unknown_user_look_exactly_the_same(db, client):
    acct = make_user(db)
    bodies = [
        login(client, acct, password="not the password at all"),
        login(client, acct, code="000000"),
        client.post("/api/auth/login", json={"username": "nobody", "password": acct.password, "totp": totp_now(acct.secret)}),
    ]
    assert [r.status_code for r in bodies] == [401, 401, 401]
    assert len({r.text for r in bodies}) == 1 and bodies[0].json() == {"error": "INVALID_CREDENTIALS", "message": "Invalid credentials"}


def test_an_invited_user_who_has_not_finished_setup_cannot_log_in(db, client):
    from app.security import users
    users.create_user(db, name="Pending", username="pending", email=None, role="user", created_by="x")
    r = client.post("/api/auth/login", json={"username": "pending", "password": "correct horse battery staple", "totp": "123456"})
    assert r.status_code == 401


def test_the_same_code_cannot_be_used_twice(db, new_client):
    acct = make_user(db)
    code = totp_now(acct.secret)
    assert login(new_client(), acct, code=code).status_code == 200
    r = login(new_client(), acct, code=code)                       # replay of a code that was valid a moment ago
    assert r.status_code == 401


def test_an_older_code_than_the_last_used_is_refused_but_the_next_one_is_fine(db, new_client):
    acct = make_user(db)
    first = new_client()
    assert login(first, acct, code=totp_now(acct.secret, +1)).status_code == 200        # uses the NEXT step (inside the tolerance)
    first.post("/api/auth/logout", headers={"X-CSRF-Token": first.cookies.get("algo_csrf")})
    assert login(new_client(), acct, code=totp_now(acct.secret, 0)).status_code == 401  # the current step is now older than the last used
    assert login(new_client(), acct, code=totp_now(acct.secret, +1)).status_code == 401  # and the same step again is a replay


def test_a_backup_code_works_once_in_any_typing_style(db, new_client):
    acct = make_user(db)
    code = acct.next_backup()
    a = new_client()
    assert login(a, acct, code=code.upper()).status_code == 200
    api(a, "POST", "/api/auth/logout")
    assert login(new_client(), acct, code=code).status_code == 401
    assert login(new_client(), acct, code=acct.next_backup()).status_code == 200         # a different backup code is still good


def test_five_failures_lock_the_account_for_fifteen_minutes_even_for_the_right_credentials(db, client):
    acct = make_user(db)
    for _ in range(5):
        assert login(client, acct, password="wrong password here").status_code == 401
    r = login(client, acct)                                         # now correct, but locked
    assert r.status_code == 423 and r.json()["error"] == "LOCKED"
    assert "algo_sid" not in client.cookies


def test_the_lock_survives_a_restart_and_ends_after_fifteen_minutes(db):
    ks = ratelimit.keys("rahul", "1.2.3.4")
    now = time.time()
    for _ in range(5):
        ratelimit.record_failure(db, ks, now)
    from app.db import platform_db
    again = platform_db.connect()                                  # a "restart": a brand-new connection sees the same failures
    assert ratelimit.is_locked(again, ks, now + 60)
    assert ratelimit.is_locked(again, ks, now + 899)
    assert not ratelimit.is_locked(again, ks, now + 901)
    again.close()


def test_four_failures_do_not_lock_and_a_success_clears_the_username_counter(db, client):
    acct = make_user(db)
    for _ in range(4):
        login(client, acct, password="wrong password here")
    assert login(client, acct).status_code == 200
    assert db.execute("SELECT COUNT(*) FROM login_failures WHERE key=?", (ratelimit.keys("rahul", "x")[0],)).fetchone()[0] == 0


def test_an_ip_with_five_failures_is_locked_whatever_username_it_tries(db, client):
    acct = make_user(db)
    for i in range(5):
        client.post("/api/auth/login", json={"username": f"guess{i}", "password": "whatever password", "totp": "000000"})
    assert login(client, acct).status_code == 423


def test_login_events_are_audited_without_secrets(db, client):
    acct = make_user(db)
    login(client, acct, password="wrong password here")
    login(client, acct)
    rows = db.execute("SELECT action, actor_id, detail_json FROM audit_log WHERE action LIKE 'login.%' ORDER BY id").fetchall()
    assert [r["action"] for r in rows] == ["login.failed", "login.success"]
    assert rows[1]["actor_id"] == acct.id
    blob = " ".join(str(tuple(r)) for r in db.execute("SELECT * FROM audit_log"))
    for secret in (acct.password, "wrong password here", acct.secret, client.cookies.get("algo_sid")):
        assert secret not in blob
    assert audit.verify_chain(db) == (True, None)


def test_a_password_typed_into_the_username_box_is_not_written_to_the_audit_log(db, client):
    make_user(db)
    client.post("/api/auth/login", json={"username": "My Secret Pass phrase 123!", "password": "x" * 12, "totp": "123456"})
    blob = str(db.execute("SELECT detail_json FROM audit_log").fetchall()[0][0])
    assert "Secret" not in blob and "not a valid username" in blob
    assert "secret" not in str(db.execute("SELECT * FROM login_failures").fetchall()).lower()      # nor in the lock-out table


def test_oversized_input_is_rejected_before_any_hashing(client):
    assert client.post("/api/auth/login", json={"username": "a", "password": "p" * 5000, "totp": "123456"}).status_code == 422


def test_login_page_is_reachable_and_asks_for_the_code_only_in_platform_mode(db, client):
    assert client.get("/api/auth/mode").json() == {"mode": "open"}
    make_user(db)
    assert client.get("/api/auth/mode").json() == {"mode": "platform"}
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/app/login"
