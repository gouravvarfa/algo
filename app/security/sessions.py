"""Server-side sessions: any number of devices per user, heartbeat, and one daily sign-out time.

The cookie carries only a random 256-bit token; the database row is keyed by its SHA-256. Ending a session (logout, force-logout, disable)
is therefore instant, which a signed stateless cookie cannot do.

  * heartbeat  : the open page pings every 30 s -> last_seen_at. No ping for 2 minutes = the page is gone (tab closed, laptop asleep).
  * activity   : a ping that reports real user input -> last_activity_at (shown to developers; it no longer signs anyone out).
  * daily end  : every session ends at the next 04:00 IST after it started (the owner, 18 Nov 2026: sign out at 4 am, when no market is open,
                 so nobody is signed out during NSE or MCX hours; before that it was 4 pm). Until then only logout or an admin ends it.
A login on another device opens its own session and leaves the others signed in (the owner, 15 Nov 2026: no limit on devices).
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
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DAILY_END = dt.time(4, 0)                  # 4 am IST: every session ends at the next one of these (no market is open then)
SWITCHED_AT = dt.datetime(2026, 11, 18, 13, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))).timestamp()   # 4 pm -> 4 am
ABSOLUTE_LIFETIME = 24 * 3600              # a session can never be older than one day


def ends_at(started_at: float) -> float:
    """The next 04:00 IST after `started_at` (a login at 03:59 ends a minute later; at 04:00 or after, it ends at 04:00 the next day). A session
    started before the switch from 4 pm keeps going until the first 4 am after the switch, so the change itself signs nobody out."""
    t = dt.datetime.fromtimestamp(max(started_at, SWITCHED_AT) if started_at < SWITCHED_AT else started_at, IST)
    end = t.replace(hour=DAILY_END.hour, minute=DAILY_END.minute, second=0, microsecond=0)
    if end <= t:
        end += dt.timedelta(days=1)
    return end.timestamp()


def seconds_left(started_at: float, now: float | None = None) -> int:
    now = time.time() if now is None else now
    return max(0, int(ends_at(started_at) - now))


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
    if now >= ends_at(row["started_at"]):
        return "expired"
    return None


def create(c: sqlite3.Connection, user_id: str, user_agent: str | None, ip: str, now: float | None = None, replace: bool = False) -> dict:
    """Start a session for this device. Sessions on the user's other devices stay signed in. `replace` is accepted for older pages
    and ignored; 'taken_over' is always None (no device is signed out by a login any more). The caller has already checked the
    password and the second factor."""
    now = time.time() if now is None else now
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    with write_tx(c):
        for old in c.execute("SELECT id, started_at FROM sessions WHERE user_id=? AND ended_at IS NULL", (user_id,)).fetchall():
            if _end_reason_if_dead(old, now):            # past its 4 am on a device that never came back: closed now, so the lists stay true
                c.execute("UPDATE sessions SET ended_at=?, end_reason='expired' WHERE id=?", (now, old["id"]))
        c.execute(
            "INSERT INTO sessions (id, user_id, csrf_token, started_at, last_seen_at, last_activity_at, device_label, user_agent_hash, ip_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (_hash(token), user_id, csrf, now, now, now, device_label(user_agent),
             hashlib.sha256((user_agent or "").encode()).hexdigest(), hashlib.sha256(ip.encode()).hexdigest()))
    return {"token": token, "csrf": csrf, "taken_over": None}


def ended_reason(c: sqlite3.Connection, token: str | None) -> str | None:
    """Why a cookie's session is no longer valid (logout, idle, expired, forced, disabled, reset, replaced, taken_over), or None if unknown.
    Lets the page that was signed out say so, instead of only failing."""
    if not token or len(token) > 200:
        return None
    row = c.execute("SELECT end_reason FROM sessions WHERE id=? AND ended_at IS NOT NULL", (_hash(token),)).fetchone()
    return row[0] if row else None


def lookup(c: sqlite3.Connection, token: str | None, now: float | None = None) -> sqlite3.Row | None:
    """The session row (joined with its user) if the cookie token is a live session of an ACTIVE user, else None.
    A session found to be past its daily sign-out time is closed here."""
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
