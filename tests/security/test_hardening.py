"""Proxy-header trust, the encryption-key fallback, giving a code back when the login is refused for another device, and changing a password."""
import logging
import time

import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import platform_db
from app.security import crypto, ratelimit, users
from tests.security.helpers import api, login, make_user, totp_now


def failing(client, name, ip=None):
    return client.post("/api/auth/login", json={"username": name, "password": "wrong password here", "totp": "000000"},
                       headers={"X-Real-IP": ip} if ip else {})


# ---- X-Real-IP / X-Forwarded-Proto are believed only from a trusted proxy ----
def test_a_stranger_cannot_dodge_the_ip_lock_by_sending_an_x_real_ip_header(db, client):
    acct = make_user(db)
    for i in range(5):
        failing(client, f"guess{i}", ip=f"10.0.0.{i}")                      # a different claimed address every time
    assert login(client, acct).status_code == 423                           # still one real address: locked


def test_the_proxy_on_this_machine_is_believed(db):
    c = TestClient(main.app, client=("127.0.0.1", 50000))
    try:
        failing(c, "someone", ip="10.9.8.7")
    finally:
        c.close()
    assert db.execute("SELECT 1 FROM login_failures WHERE key=?", (ratelimit.keys("x", "10.9.8.7")[1],)).fetchone()
    assert db.execute("SELECT 1 FROM login_failures WHERE key=?", (ratelimit.keys("x", "127.0.0.1")[1],)).fetchone() is None


def test_an_extra_trusted_proxy_can_be_configured(db, client, monkeypatch):
    monkeypatch.setenv("APP_TRUSTED_PROXIES", "testclient, 10.1.1.1")
    failing(client, "someone", ip="10.9.8.7")
    assert db.execute("SELECT 1 FROM login_failures WHERE key=?", (ratelimit.keys("x", "10.9.8.7")[1],)).fetchone()


def test_secure_cookie_flag_follows_the_proxy_header_only_from_a_trusted_peer(db):
    acct = make_user(db)
    https = {"X-Forwarded-Proto": "https"}
    behind_proxy = TestClient(main.app, client=("127.0.0.1", 50000), headers=https)
    stranger = TestClient(main.app, headers=https)
    try:
        trusted = login(behind_proxy, acct).headers.get_list("set-cookie")
        assert all("Secure" in c for c in trusted)
        db.execute("UPDATE sessions SET ended_at=1, end_reason='logout'")
        db.commit()
        untrusted = login(stranger, acct, code=acct.next_backup()).headers.get_list("set-cookie")
        assert untrusted and not any("Secure" in c for c in untrusted)
    finally:
        behind_proxy.close()
        stranger.close()


# ---- encryption key ----
def test_using_the_key_file_logs_a_warning_once_and_the_environment_key_does_not(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(crypto, "KEY_FILE", tmp_path / "enc.key")
    monkeypatch.setattr(crypto, "_warned", False)
    with caplog.at_level(logging.WARNING, logger="app.security"):
        crypto.encrypt("a")                                                  # APP_ENC_KEY is set by the fixture: silent
        assert not caplog.records
        monkeypatch.delenv("APP_ENC_KEY")
        crypto.encrypt("a")
        crypto.encrypt("b")
    warnings = [r for r in caplog.records if "APP_ENC_KEY is not set" in r.getMessage()]
    assert len(warnings) == 1 and (tmp_path / "enc.key").exists()


# ---- a refused second device must not burn the code ----
def test_a_totp_code_is_given_back_when_the_login_was_refused_for_another_device(db, new_client):
    acct = make_user(db)
    laptop1, laptop2 = new_client(), new_client()
    assert login(laptop1, acct, code=acct.next_backup()).status_code == 200
    code = totp_now(acct.secret)
    assert login(laptop2, acct, code=code).status_code == 409
    api(laptop1, "POST", "/api/auth/logout")
    assert login(laptop2, acct, code=code).status_code == 200               # the same code still works: it was not spent by the refusal
    assert login(new_client(), acct, code=code).status_code == 401           # ...but once it has logged someone in it is spent


def test_a_backup_code_is_given_back_too(db, new_client):
    acct = make_user(db)
    laptop1, laptop2 = new_client(), new_client()
    assert login(laptop1, acct).status_code == 200
    spare = acct.next_backup()
    assert login(laptop2, acct, code=spare).status_code == 409
    api(laptop1, "POST", "/api/auth/logout")
    assert login(laptop2, acct, code=spare).status_code == 200
    api(laptop2, "POST", "/api/auth/logout")
    assert login(new_client(), acct, code=spare).status_code == 401


def test_giving_a_code_back_never_rolls_back_a_newer_step(db):
    acct = make_user(db)
    now = time.time()
    _, undo_first = users.authenticate_with_undo(db, acct.username, acct.password, totp_now(acct.secret, 0), now)
    users.authenticate_with_undo(db, acct.username, acct.password, totp_now(acct.secret, +1), now)      # a later step is claimed
    newest = db.execute("SELECT totp_last_used_step FROM users WHERE id=?", (acct.id,)).fetchone()[0]
    undo_first()                                                                                       # must not move the counter back
    assert db.execute("SELECT totp_last_used_step FROM users WHERE id=?", (acct.id,)).fetchone()[0] == newest


# ---- change password ----
NEW = "a different long passphrase"


def test_a_user_can_change_their_password_and_the_old_one_stops_working(db, new_client):
    acct = make_user(db)
    c = new_client()
    login(c, acct)
    r = api(c, "POST", "/api/auth/change-password", json={"current_password": acct.password, "new_password": NEW})
    assert r.status_code == 200 and c.get("/api/auth/me").status_code == 200           # still signed in
    api(c, "POST", "/api/auth/logout")
    assert login(new_client(), acct, code=acct.next_backup()).status_code == 401       # old password
    assert login(new_client(), acct, code=acct.next_backup(), password=NEW).status_code == 200
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action='password.change' AND actor_id=?", (acct.id,)).fetchone()[0] == 1
    assert NEW not in str(db.execute("SELECT * FROM audit_log").fetchall())


def test_changing_the_password_needs_the_current_one_and_a_good_new_one(db, client):
    acct = make_user(db)
    login(client, acct)
    wrong = api(client, "POST", "/api/auth/change-password", json={"current_password": "not my password!!", "new_password": NEW})
    assert wrong.status_code == 401 and wrong.json()["error"] == "WRONG_PASSWORD"
    weak = api(client, "POST", "/api/auth/change-password", json={"current_password": acct.password, "new_password": "short"})
    assert weak.status_code == 400 and weak.json()["error"] == "WEAK_PASSWORD"
    same = api(client, "POST", "/api/auth/change-password", json={"current_password": acct.password, "new_password": acct.password})
    assert same.status_code == 400 and same.json()["error"] == "SAME_PASSWORD"


def test_guessing_the_current_password_with_a_stolen_session_gets_locked(db, client):
    acct = make_user(db)
    login(client, acct)
    for _ in range(5):
        assert api(client, "POST", "/api/auth/change-password", json={"current_password": "wrong guess here", "new_password": NEW}).status_code == 401
    r = api(client, "POST", "/api/auth/change-password", json={"current_password": acct.password, "new_password": NEW})
    assert r.status_code == 423                                                         # even the right password is refused while locked


def test_change_password_needs_a_session_and_the_csrf_header(db, client, new_client):
    acct = make_user(db)
    assert new_client().post("/api/auth/change-password", json={"current_password": "x" * 12, "new_password": NEW}).status_code == 401
    login(client, acct)
    assert client.post("/api/auth/change-password", json={"current_password": acct.password, "new_password": NEW}).status_code == 403


def test_account_page_needs_login_and_has_the_form(db, client):
    acct = make_user(db)
    assert client.get("/account", follow_redirects=False).status_code == 303
    login(client, acct)
    page = client.get("/account")
    assert page.status_code == 200 and "Change password" in page.text and "/api/auth/change-password" in page.text
