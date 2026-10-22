"""'Sign out the other device and sign in here': allowed only with the right password AND code, audited, and the signed-out device is told why."""
import threading

import pytest

from app.db import platform_db
from app.security import sessions
from tests.security.conftest import CHROME_WINDOWS, FIREFOX_LINUX
from tests.security.helpers import api, login, make_user, totp_now


def take_over(client, acct, code=None, password=None, replace=True):
    return client.post("/api/auth/login", json={"username": acct.username, "password": password or acct.password,
                                                "totp": code if code is not None else totp_now(acct.secret), "replace_existing": replace})


def signed_in_laptop(db, new_client):
    acct = make_user(db)
    laptop = new_client(CHROME_WINDOWS)
    assert login(laptop, acct, code=acct.next_backup()).status_code == 200
    return acct, laptop


def test_the_refusal_offers_the_takeover(db, new_client):
    acct, _ = signed_in_laptop(db, new_client)
    r = login(new_client(FIREFOX_LINUX), acct)
    assert r.status_code == 409 and r.json()["can_take_over"] is True


def test_taking_over_signs_the_other_device_out_and_this_one_in(db, new_client):
    acct, laptop = signed_in_laptop(db, new_client)
    phone = new_client(FIREFOX_LINUX)
    r = take_over(phone, acct)
    assert r.status_code == 200 and phone.get("/api/auth/me").status_code == 200
    assert laptop.get("/api/auth/me").status_code == 401                                   # the laptop is out
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id=? AND ended_at IS NULL", (acct.id,)).fetchone()[0] == 1
    assert [x[0] for x in db.execute("SELECT end_reason FROM sessions ORDER BY started_at")] == ["taken_over", None]


def test_the_signed_out_device_is_told_why_on_its_next_request(db, new_client):
    acct, laptop = signed_in_laptop(db, new_client)
    take_over(new_client(FIREFOX_LINUX), acct)
    beat = api(laptop, "POST", "/api/auth/heartbeat", json={"active": True})
    assert beat.status_code == 401 and beat.json()["reason"] == "taken_over"                # shell.js turns this into the 'moved' message


def test_a_takeover_is_audited_with_both_devices_and_no_secrets(db, new_client):
    acct, _ = signed_in_laptop(db, new_client)
    take_over(new_client(FIREFOX_LINUX), acct)
    row = db.execute("SELECT actor_id, detail_json FROM audit_log WHERE action='login.takeover'").fetchone()
    assert row["actor_id"] == acct.id
    assert "Chrome · Windows" in row["detail_json"] and "Firefox · Linux" in row["detail_json"]
    assert acct.password not in str(db.execute("SELECT * FROM audit_log").fetchall())


# ---- the safety properties ----
def test_without_the_flag_nothing_is_taken_over(db, new_client):
    acct, laptop = signed_in_laptop(db, new_client)
    assert take_over(new_client(FIREFOX_LINUX), acct, replace=False).status_code == 409
    assert laptop.get("/api/auth/me").status_code == 200


@pytest.mark.parametrize("what", ["password", "code"])
def test_the_flag_does_not_skip_the_credentials(db, new_client, what):
    acct, laptop = signed_in_laptop(db, new_client)
    phone = new_client(FIREFOX_LINUX)
    bad = take_over(phone, acct, password="not the password at all") if what == "password" else take_over(phone, acct, code="000000")
    assert bad.status_code == 401
    assert laptop.get("/api/auth/me").status_code == 200                                    # a wrong guess can never sign the real user out
    assert "algo_sid" not in phone.cookies


def test_a_locked_account_cannot_be_used_to_take_over(db, new_client):
    acct, laptop = signed_in_laptop(db, new_client)
    phone = new_client(FIREFOX_LINUX)
    for _ in range(5):
        take_over(phone, acct, password="wrong password here")
    assert take_over(phone, acct).status_code == 423
    assert laptop.get("/api/auth/me").status_code == 200


def test_it_can_be_switched_off_for_a_deployment(db, new_client, monkeypatch):
    monkeypatch.setenv("APP_ALLOW_TAKEOVER", "0")
    acct, laptop = signed_in_laptop(db, new_client)
    phone = new_client(FIREFOX_LINUX)
    refused = take_over(phone, acct)
    assert refused.status_code == 409 and refused.json()["can_take_over"] is False          # the page then shows no button
    assert laptop.get("/api/auth/me").status_code == 200                                    # and the flag is ignored


def test_the_code_from_the_refusal_still_works_for_the_takeover(db, new_client):
    """The page's two steps: first a normal attempt (refused, code given back), then the button re-sends the same fields with the flag."""
    acct = make_user(db)
    laptop, phone = new_client(), new_client(FIREFOX_LINUX)
    assert login(laptop, acct, code=acct.next_backup()).status_code == 200
    code = totp_now(acct.secret)
    assert take_over(phone, acct, code=code, replace=False).status_code == 409
    assert take_over(phone, acct, code=code, replace=True).status_code == 200
    assert take_over(new_client(), acct, code=code).status_code == 401                      # and now that code is spent


def test_a_stale_session_is_replaced_quietly_not_reported_as_a_takeover(db, new_client):
    acct, laptop = signed_in_laptop(db, new_client)
    db.execute("UPDATE sessions SET last_seen_at = last_seen_at - 200")
    db.commit()
    assert login(new_client(FIREFOX_LINUX), acct).status_code == 200
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action='login.takeover'").fetchone()[0] == 0
    assert db.execute("SELECT end_reason FROM sessions ORDER BY started_at").fetchone()[0] == "replaced"


def test_eight_simultaneous_takeovers_leave_exactly_one_session(db):
    acct = make_user(db)
    sessions.create(db, acct.id, CHROME_WINDOWS, "1.1.1.1")
    barrier, results = threading.Barrier(8), []

    def attempt():
        c = platform_db.connect()
        try:
            barrier.wait()
            results.append(bool(sessions.create(c, acct.id, FIREFOX_LINUX, "2.2.2.2", replace=True)))
        finally:
            c.close()

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == [True] * 8
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id=? AND ended_at IS NULL", (acct.id,)).fetchone()[0] == 1


# ---- why a session ended ----
def test_the_401_says_why_the_session_ended(db, new_client):
    acct = make_user(db)
    c = new_client()
    login(c, acct)
    token = c.cookies.get("algo_sid")
    api(c, "POST", "/api/auth/logout")
    gone = new_client().get("/api/auth/me", headers={"Cookie": f"algo_sid={token}"})
    assert gone.status_code == 401 and gone.json()["reason"] == "logout"
    unknown = new_client().get("/api/auth/me", headers={"Cookie": "algo_sid=never-existed-" + "x" * 30})
    assert unknown.status_code == 401 and unknown.json()["reason"] is None


# ---- the page ----
def test_login_page_has_the_button_and_the_moved_message(client):
    page = client.get("/login").text
    assert "Sign out the other device and sign in here" in page and "replace_existing" in page
    assert "signed in on another device" in page and "e') === 'moved'" in page
    shell = client.get("/static/shell.js").text
    assert "taken_over" in shell and "/login?e=" in shell
