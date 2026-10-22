"""Which login is in force?

  platform : at least one platform user exists -> password + TOTP login with server-side sessions (this package)
  legacy   : no platform user, but APP_USERNAME / APP_PASSWORD_HASH / APP_SECRET are set -> the old single-user login (app/auth.py)
  open     : neither -> no login at all (local use, exactly as before)
So an existing deployment keeps working until its first platform user is created (scripts/create_owner.py).
Once platform mode is on it stays on: users are only ever soft-deleted, so the table never becomes empty again.
"""
from __future__ import annotations

import time

from app import auth
from app.db import platform_db

_RECHECK_SECONDS = 5
_has_users = False
_checked_at = 0.0


def has_users() -> bool:
    global _has_users, _checked_at
    if _has_users:
        return True
    now = time.monotonic()
    if now - _checked_at < _RECHECK_SECONDS:
        return False
    _checked_at = now
    if not platform_db.DB.exists():                    # never create the database just to ask this question
        return False
    c = platform_db.connect()                          # an unreadable database raises: the app fails closed, it does not open up
    try:
        _has_users = c.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    finally:
        c.close()
    return _has_users


def current() -> str:
    if has_users():
        return "platform"
    return "legacy" if auth.enabled() else "open"


def reset() -> None:
    """Forget the cached answer (tests, and a server pointed at another database)."""
    global _has_users, _checked_at
    _has_users, _checked_at = False, 0.0
