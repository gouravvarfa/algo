"""Login brute-force lock: 5 failures in 15 minutes lock a username, and 20 lock an IP address.
The IP limit is higher because a home or an office reaches the server from ONE public address: one person's 5 mistakes (an account that was just
reset, say) locked every family member out (the owner, 15 Nov 2026). A guesser is still held by the username limit, and a single address
guessing many usernames by the IP one. Failures live in the database, so restarting the server does not reset the lock."""
from __future__ import annotations

import hashlib
import math
import sqlite3
import time

MAX_FAILURES = 5                 # per username
MAX_FAILURES_IP = 20             # per IP address
WINDOW_SECONDS = 15 * 60


def keys(username: str, ip: str) -> list[str]:
    # the username is stored only as a hash: if someone types their password into the username box it must not sit in this table in clear
    return [f"user:{hashlib.sha256((username or '').strip().lower().encode()).hexdigest()[:32]}", f"ip:{(ip or '?')[:100]}"]


def _limit(key: str) -> int:
    return MAX_FAILURES_IP if key.startswith("ip:") else MAX_FAILURES


def seconds_locked(c: sqlite3.Connection, ks: list[str], now: float | None = None) -> int:
    """0 when not locked; otherwise how many seconds until the lock ends (when the failure that made the count reach the limit leaves the window)."""
    now = time.time() if now is None else now
    left = 0
    for k in ks:
        ts = [r[0] for r in c.execute("SELECT ts FROM login_failures WHERE key=? AND ts>? ORDER BY ts DESC", (k, now - WINDOW_SECONDS))]
        lim = _limit(k)
        if len(ts) >= lim:
            left = max(left, math.ceil(ts[lim - 1] + WINDOW_SECONDS - now))
    return left


def is_locked(c: sqlite3.Connection, ks: list[str], now: float | None = None) -> bool:
    return seconds_locked(c, ks, now) > 0


def locked_message(seconds: int) -> str:
    m = max(1, -(-seconds // 60))
    return f"Too many attempts. Try again in {m} minute{'s' if m > 1 else ''}."


def record_failure(c: sqlite3.Connection, ks: list[str], now: float | None = None) -> None:
    now = time.time() if now is None else now
    with c:
        c.execute("DELETE FROM login_failures WHERE ts<?", (now - WINDOW_SECONDS,))
        c.executemany("INSERT INTO login_failures (key, ts) VALUES (?,?)", [(k, now) for k in ks])


def clear(c: sqlite3.Connection, key: str) -> None:
    with c:
        c.execute("DELETE FROM login_failures WHERE key=?", (key,))
