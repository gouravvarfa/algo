"""One account on any number of devices; heartbeat, idle and absolute timeouts; atomic session creation (PDF 3.3 and 3.4)."""
import datetime as dt
import time
import threading

import pytest

from app.db import platform_db
from app.security import sessions
from tests.security.conftest import CHROME_WINDOWS, FIREFOX_LINUX
from tests.security.helpers import api, login, make_user


def age(db, user_id, **columns):
    """Make the user's active session older: age(db, uid, last_seen_at=130) moves that column 130 s into the past."""
    sets = ", ".join(f"{c} = {c} - {sec}" for c, sec in columns.items())
    db.execute(f"UPDATE sessions SET {sets} WHERE user_id=? AND ended_at IS NULL", (user_id,))
    db.commit()


def end_reason(db, user_id):
    return [r[0] for r in db.execute("SELECT end_reason FROM sessions WHERE user_id=? ORDER BY started_at", (user_id,))]


# ---- any number of devices (the owner, 15 Nov 2026) ----
def test_a_second_device_signs_in_and_both_stay_signed_in(db, new_client):
    acct = make_user(db)
    laptop, phone = new_client(CHROME_WINDOWS), new_client(FIREFOX_LINUX)
    assert login(laptop, acct).status_code == 200
    assert login(phone, acct, code=acct.next_backup()).status_code == 200
    assert laptop.get("/api/auth/me").status_code == 200 and phone.get("/api/auth/me").status_code == 200
    assert end_reason(db, acct.id) == [None, None]


def test_five_devices_at_once(db, new_client):
    acct = make_user(db)
    devices = [new_client() for _ in range(5)]
    for d in devices:
        assert login(d, acct, code=acct.next_backup()).status_code == 200
    assert all(d.get("/api/auth/me").status_code == 200 for d in devices)


def test_logging_out_on_one_device_leaves_the_others_signed_in(db, new_client):
    acct = make_user(db)
    laptop, phone = new_client(), new_client()
    login(laptop, acct)
    login(phone, acct, code=acct.next_backup())
    assert api(laptop, "POST", "/api/auth/logout").status_code == 200
    assert laptop.get("/api/auth/me").status_code == 401 and phone.get("/api/auth/me").status_code == 200
    assert end_reason(db, acct.id) == ["logout", None]


def test_the_old_replace_flag_signs_nobody_out(db, new_client):
    acct = make_user(db)
    laptop, phone = new_client(), new_client()
    login(laptop, acct)
    r = phone.post("/api/auth/login", json={"username": acct.username, "password": acct.password, "totp": acct.next_backup(), "replace_existing": True})
    assert r.status_code == 200 and laptop.get("/api/auth/me").status_code == 200


def test_an_admin_force_logout_ends_every_device(db, new_client):
    acct = make_user(db)
    laptop, phone = new_client(), new_client()
    login(laptop, acct)
    login(phone, acct, code=acct.next_backup())
    assert sessions.end_all(db, acct.id, "forced") == 2
    assert laptop.get("/api/auth/me").status_code == 401 and phone.get("/api/auth/me").status_code == 401


def test_a_device_left_signed_in_past_its_4am_is_closed_at_the_next_login(db, new_client, monkeypatch):
    monkeypatch.setattr(sessions, "SWITCHED_AT", 0)                                 # the rule itself, without the one-time 4 pm -> 4 am grace
    acct = make_user(db)
    login(new_client(), acct)
    age(db, acct.id, started_at=2 * 24 * 3600)                                     # signed in two days ago and never came back
    assert login(new_client(), acct, code=acct.next_backup()).status_code == 200
    assert end_reason(db, acct.id) == ["expired", None]


def test_the_admin_list_shows_a_person_once_however_many_devices(db, new_client):
    from app.security import users
    acct = make_user(db)
    login(new_client(CHROME_WINDOWS), acct)
    login(new_client(FIREFOX_LINUX), acct, code=acct.next_backup())
    rows = [r for r in users.list_users(db) if r["id"] == acct.id]
    assert len(rows) == 1 and rows[0]["online"] and rows[0]["devices_online"] == 2 and rows[0]["device"].endswith("+1 more")


# ---- one daily sign-out time (4 am IST), no idle timeout ----
def _ist(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=sessions.IST).timestamp()


def test_sessions_end_at_the_next_4am_ist():
    assert sessions.ends_at(_ist(2026, 11, 20, 9, 15)) == _ist(2026, 11, 21, 4, 0)        # a trading-day login lasts through NSE and MCX hours
    assert sessions.ends_at(_ist(2026, 11, 20, 23, 50)) == _ist(2026, 11, 21, 4, 0)       # MCX evening: still signed in after 23:55
    assert sessions.ends_at(_ist(2026, 11, 21, 3, 59)) == _ist(2026, 11, 21, 4, 0)
    assert sessions.ends_at(_ist(2026, 11, 21, 4, 0)) == _ist(2026, 11, 22, 4, 0)         # at or after 4 am: 4 am the next day


def test_the_switch_from_4pm_to_4am_signs_nobody_out():
    assert sessions.ends_at(_ist(2026, 11, 17, 16, 0)) == _ist(2026, 11, 19, 4, 0)        # signed in before the switch: until the first 4 am after it
    assert sessions.ends_at(_ist(2026, 11, 18, 6, 54)) == _ist(2026, 11, 19, 4, 0)


def test_hours_without_any_action_do_not_sign_the_person_out(db, client):
    acct = make_user(db)
    login(client, acct)
    age(db, acct.id, last_activity_at=6 * 3600, last_seen_at=6 * 3600)              # six hours with no input and no ping
    assert client.get("/api/auth/me").status_code == 200                            # still signed in: only 4 am ends it


def test_the_session_ends_at_4am(db, client, monkeypatch):
    monkeypatch.setattr(sessions, "SWITCHED_AT", 0)
    acct = make_user(db)
    login(client, acct)
    started = db.execute("SELECT started_at FROM sessions WHERE ended_at IS NULL").fetchone()[0]
    before_last_4am = sessions.ends_at(started) - 86400 - 3600                     # a login an hour before the last 4 am: that 4 am has passed
    db.execute("UPDATE sessions SET started_at=? WHERE ended_at IS NULL", (before_last_4am,))
    db.commit()
    assert client.get("/api/auth/me").status_code == 401
    assert end_reason(db, acct.id) == ["expired"]


def test_the_cookie_lives_until_just_after_4am(db, client):
    acct = make_user(db)
    r = login(client, acct)
    cookie = [h for h in r.headers.get_list("set-cookie") if h.startswith(sessions.COOKIE + "=")][0]
    max_age = int(cookie.split("Max-Age=")[1].split(";")[0])
    assert abs(max_age - (sessions.seconds_left(time.time()) + 60)) <= 5


def test_heartbeat_with_input_keeps_the_person_active_but_a_bare_ping_does_not(db, client):
    acct = make_user(db)
    login(client, acct)
    age(db, acct.id, last_activity_at=1000, last_seen_at=1000)
    before = db.execute("SELECT last_seen_at, last_activity_at FROM sessions").fetchone()
    assert api(client, "POST", "/api/auth/heartbeat", json={"active": False}).status_code == 200
    ping = db.execute("SELECT last_seen_at, last_activity_at FROM sessions").fetchone()
    assert ping["last_seen_at"] > before["last_seen_at"] + 900 and ping["last_activity_at"] == before["last_activity_at"]
    assert api(client, "POST", "/api/auth/heartbeat", json={"active": True}).status_code == 200
    touched = db.execute("SELECT last_seen_at, last_activity_at FROM sessions").fetchone()
    assert touched["last_activity_at"] > before["last_activity_at"] + 900


def test_heartbeat_on_an_ended_session_answers_401_so_the_page_can_send_the_user_to_login(db, client):
    acct = make_user(db)
    login(client, acct)
    db.execute("UPDATE sessions SET ended_at=1, end_reason='forced'")
    db.commit()
    assert api(client, "POST", "/api/auth/heartbeat", json={"active": True}).status_code == 401


# ---- atomic check ----
def test_ten_simultaneous_logins_all_succeed(db):
    acct = make_user(db)
    barrier, results = threading.Barrier(10), []

    def attempt():
        c = platform_db.connect()
        try:
            barrier.wait()
            results.append(bool(sessions.create(c, acct.id, CHROME_WINDOWS, "1.1.1.1")))
        finally:
            c.close()

    threads = [threading.Thread(target=attempt) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == [True] * 10
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id=? AND ended_at IS NULL", (acct.id,)).fetchone()[0] == 10


# ---- device label ----
@pytest.mark.parametrize("ua,label", [
    (CHROME_WINDOWS, "Chrome · Windows"),
    (FIREFOX_LINUX, "Firefox · Linux"),
    ("Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/120.0 Safari/537.36 Edg/120.0", "Edge · Windows"),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile Safari/604.1", "Safari · iOS"),
    ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36", "Chrome · Android"),
    ("", "Browser · unknown OS"),
    (None, "Browser · unknown OS"),
])
def test_device_label_names_browser_and_os_only(ua, label):
    assert sessions.device_label(ua) == label


def test_the_cookie_value_is_never_the_database_key(db, client):
    acct = make_user(db)
    login(client, acct)
    token = client.cookies.get("algo_sid")
    assert db.execute("SELECT 1 FROM sessions WHERE id=?", (token,)).fetchone() is None
    assert sessions.lookup(db, token) is not None and sessions.lookup(db, token + "x") is None and sessions.lookup(db, None) is None
