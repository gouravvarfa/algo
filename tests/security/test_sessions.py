"""One account, one device; heartbeat, idle and absolute timeouts; atomic session creation (PDF 3.3 and 3.4)."""
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


# ---- acceptance test 1 ----
def test_second_device_is_refused_while_the_first_is_live(db, new_client):
    acct = make_user(db)
    laptop1, laptop2 = new_client(CHROME_WINDOWS), new_client(FIREFOX_LINUX)
    assert login(laptop1, acct).status_code == 200
    r = login(laptop2, acct, code=acct.next_backup())
    body = r.json()
    assert r.status_code == 409 and body["error"] == "ALREADY_LOGGED_IN"
    assert "Log out there first" in body["message"]
    assert body["device"] == "Chrome · Windows"                                   # the OTHER device, as the PDF's message shows
    assert body["since"].endswith("+05:30") and body["last_active"].endswith("+05:30")
    assert "testclient" not in r.text and "Linux" not in r.text                    # no IP, and nothing about the device that is asking
    assert "algo_sid" not in laptop2.cookies
    assert laptop1.get("/api/auth/me").status_code == 200                          # the first device is untouched


def test_after_logging_out_on_the_first_device_the_second_can_log_in(db, new_client):
    acct = make_user(db)
    laptop1, laptop2 = new_client(), new_client()
    login(laptop1, acct)
    assert api(laptop1, "POST", "/api/auth/logout").status_code == 200
    assert login(laptop2, acct, code=acct.next_backup()).status_code == 200
    assert laptop1.get("/api/auth/me").status_code == 401
    assert end_reason(db, acct.id) == ["logout", None]


# ---- acceptance test 2 ----
def test_a_session_whose_page_stopped_pinging_for_two_minutes_is_replaced(db, new_client):
    acct = make_user(db)
    laptop1, laptop2 = new_client(), new_client()
    login(laptop1, acct)
    age(db, acct.id, last_seen_at=sessions.HEARTBEAT_DEAD + 5)                    # laptop 1 closed without logging out
    assert login(laptop2, acct, code=acct.next_backup()).status_code == 200
    assert laptop1.get("/api/auth/me").status_code == 401
    assert end_reason(db, acct.id) == ["replaced", None]


def test_just_under_two_minutes_still_counts_as_live(db, new_client):
    acct = make_user(db)
    login(new_client(), acct)
    age(db, acct.id, last_seen_at=sessions.HEARTBEAT_DEAD - 15)
    assert login(new_client(), acct, code=acct.next_backup()).status_code == 409


# ---- idle / absolute timeouts ----
def test_thirty_minutes_without_user_activity_ends_the_session(db, client):
    acct = make_user(db)
    login(client, acct)
    age(db, acct.id, last_activity_at=sessions.IDLE_TIMEOUT + 5)
    assert client.get("/api/auth/me").status_code == 401
    assert end_reason(db, acct.id) == ["idle"]


def test_an_idle_session_does_not_block_a_new_login_even_if_its_page_is_still_pinging(db, new_client):
    acct = make_user(db)
    login(new_client(), acct)
    age(db, acct.id, last_activity_at=sessions.IDLE_TIMEOUT + 5)                  # tab open (pinging) but nobody is there
    assert login(new_client(), acct, code=acct.next_backup()).status_code == 200
    assert end_reason(db, acct.id) == ["idle", None]


def test_a_session_ends_twelve_hours_after_login_whatever_the_user_does(db, client):
    acct = make_user(db)
    login(client, acct)
    age(db, acct.id, started_at=sessions.ABSOLUTE_LIFETIME + 5)
    assert client.get("/api/auth/me").status_code == 401
    assert end_reason(db, acct.id) == ["expired"]


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
def test_ten_simultaneous_logins_create_exactly_one_session(db):
    acct = make_user(db)
    barrier, results = threading.Barrier(10), []

    def attempt():
        c = platform_db.connect()
        try:
            barrier.wait()
            results.append(sessions.create(c, acct.id, CHROME_WINDOWS, "1.1.1.1") and "ok")
        except sessions.AlreadyLoggedIn:
            results.append("refused")
        finally:
            c.close()

    threads = [threading.Thread(target=attempt) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["ok"] + ["refused"] * 9
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id=? AND ended_at IS NULL", (acct.id,)).fetchone()[0] == 1


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
