"""Login brute-force lock: 5 failures in 15 minutes lock a username AND, separately, an IP address.
Failures live in the database, so restarting the server does not reset the lock."""
from __future__ import annotations

import hashlib
import sqlite3
import time

MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60


def keys(username: str, ip: str) -> list[str]:
    # the username is stored only as a hash: if someone types their password into the username box it must not sit in this table in clear
    return [f"user:{hashlib.sha256((username or '').strip().lower().encode()).hexdigest()[:32]}", f"ip:{(ip or '?')[:100]}"]


def is_locked(c: sqlite3.Connection, ks: list[str], now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return any(c.execute("SELECT COUNT(*) FROM login_failures WHERE key=? AND ts>?", (k, now - WINDOW_SECONDS)).fetchone()[0] >= MAX_FAILURES
               for k in ks)


def record_failure(c: sqlite3.Connection, ks: list[str], now: float | None = None) -> None:
    now = time.time() if now is None else now
    with c:
        c.execute("DELETE FROM login_failures WHERE ts<?", (now - WINDOW_SECONDS,))
        c.executemany("INSERT INTO login_failures (key, ts) VALUES (?,?)", [(k, now) for k in ks])


def clear(c: sqlite3.Connection, key: str) -> None:
    with c:
        c.execute("DELETE FROM login_failures WHERE key=?", (key,))
