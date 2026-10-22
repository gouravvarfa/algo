"""Server-side sessions: one active session per user, heartbeat, idle and absolute timeouts.

The cookie carries only a random 256-bit token; the database row is keyed by its SHA-256. Ending a session (logout, force-logout, disable)
is therefore instant, which a signed stateless cookie cannot do.

  * heartbeat  : the open page pings every 30 s -> last_seen_at. No ping for 2 minutes = the page is gone (tab closed, laptop asleep).
  * activity   : a ping that reports real user input -> last_activity_at. 30 minutes without it = idle timeout.
  * lifetime   : 12 hours after login the session ends whatever the user does (one trading day).
A second login is refused while the first session is live AND its page is still pinging; otherwise the old session is closed and replaced.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import sqlite3
import time

from app.db.platform_db import write_tx

COOKIE = "algo_sid"
CSRF_COOKIE = "algo_csrf"
HEARTBEAT_DEAD = 120
IDLE_TIMEOUT = 30 * 60
ABSOLUTE_LIFETIME = 12 * 3600
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


class AlreadyLoggedIn(Exception):
    def __init__(self, device: str, since: float, last_active: float):
        super().__init__("already logged in on another device")
        self.device, self.since, self.last_active = device, since, last_active


def iso_ist(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, IST).isoformat(timespec="seconds")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def device_label(user_agent: str | None) -> str:
    """'Chrome · Windows': enough for the owner of the account to recognise the other device. No IP, no version numbers."""
    ua = user_agent or ""
    browser = ("Edge" if "Edg/" in ua else "Opera" if "OPR/" in ua else "Firefox" if "Firefox/" in ua
               else "Chrome" if "Chrome/" in ua or "CriOS/" in ua else "Safari" if "Safari/" in ua else "Browser")
    os_ = ("Windows" if "Windows" in ua else "Android" if "Android" in ua else "iOS" if ("iPhone" in ua or "iPad" in ua)
           else "macOS" if "Mac OS X" in ua else "ChromeOS" if "CrOS" in ua else "Linux" if "Linux" in ua else "unknown OS")
    return f"{browser} · {os_}"


def _end_reason_if_dead(row: sqlite3.Row, now: float) -> str | None:
    if now - row["started_at"] >= ABSOLUTE_LIFETIME:
        return "expired"
    if now - row["last_activity_at"] >= IDLE_TIMEOUT:
        return "idle"
    return None


def create(c: sqlite3.Connection, user_id: str, user_agent: str | None, ip: str, now: float | None = None, replace: bool = False) -> dict:
    """Start the user's session. The 'does one exist?' check and the insert are one write transaction (and the unique index backs it up),
    so two simultaneous logins cannot both succeed. Raises AlreadyLoggedIn if another device is live, unless replace=True: then the live
    session is ended ('taken_over') and replaced. The caller has already checked the password and the second factor; the returned
    'taken_over' says which device was signed out (or None), so it can be audited."""
    now = time.time() if now is None else now
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    taken_over = None
    with write_tx(c):
        old = c.execute("SELECT * FROM sessions WHERE user_id=? AND ended_at IS NULL", (user_id,)).fetchone()
        if old:
            dead = _end_reason_if_dead(old, now)
            live = dead is None and now - old["last_seen_at"] < HEARTBEAT_DEAD
            if live and not replace:
                raise AlreadyLoggedIn(old["device_label"] or "another device", old["started_at"], old["last_seen_at"])
            reason = "taken_over" if live else (dead or "replaced")
            if live:
                taken_over = {"device": old["device_label"] or "another device", "since": old["started_at"], "last_active": old["last_seen_at"]}
            c.execute("UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?", (now, reason, old["id"]))
        c.execute(
            "INSERT INTO sessions (id, user_id, csrf_token, started_at, last_seen_at, last_activity_at, device_label, user_agent_hash, ip_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (_hash(token), user_id, csrf, now, now, now, device_label(user_agent),
             hashlib.sha256((user_agent or "").encode()).hexdigest(), hashlib.sha256(ip.encode()).hexdigest()))
    return {"token": token, "csrf": csrf, "taken_over": taken_over}


def ended_reason(c: sqlite3.Connection, token: str | None) -> str | None:
    """Why a cookie's session is no longer valid (logout, idle, expired, forced, disabled, reset, replaced, taken_over), or None if unknown.
    Lets the page that was signed out say so, instead of only failing."""
    if not token or len(token) > 200:
        return None
    row = c.execute("SELECT end_reason FROM sessions WHERE id=? AND ended_at IS NOT NULL", (_hash(token),)).fetchone()
    return row[0] if row else None


def lookup(c: sqlite3.Connection, token: str | None, now: float | None = None) -> sqlite3.Row | None:
    """The session row (joined with its user) if the cookie token is a live session of an ACTIVE user, else None.
    A session found to be past its idle or absolute limit is closed here."""
    if not token or len(token) > 200:
        return None
    now = time.time() if now is None else now
    row = c.execute(
        "SELECT s.*, u.username, u.name, u.role, u.status AS user_status, u.created_by "
        "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id=?", (_hash(token),)).fetchone()
    if row is None or row["ended_at"] is not None:
        return None
    if row["user_status"] != "ACTIVE":
        end(c, row["id"], "disabled", now)
        return None
    dead = _end_reason_if_dead(row, now)
    if dead:
        end(c, row["id"], dead, now)
        return None
    return row


def heartbeat(c: sqlite3.Connection, session_id: str, active: bool, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with c:
        c.execute("UPDATE sessions SET last_seen_at=?, last_activity_at=CASE WHEN ? THEN ? ELSE last_activity_at END "
                  "WHERE id=? AND ended_at IS NULL", (now, 1 if active else 0, now, session_id))


def end(c: sqlite3.Connection, session_id: str, reason: str, now: float | None = None) -> int:
    now = time.time() if now is None else now
    with c:
        return c.execute("UPDATE sessions SET ended_at=?, end_reason=? WHERE id=? AND ended_at IS NULL", (now, reason, session_id)).rowcount


def end_all(c: sqlite3.Connection, user_id: str, reason: str, now: float | None = None) -> int:
    now = time.time() if now is None else now
    with c:
        return c.execute("UPDATE sessions SET ended_at=?, end_reason=? WHERE user_id=? AND ended_at IS NULL", (now, reason, user_id)).rowcount
