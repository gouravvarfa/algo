"""Single-user login for a hosted deployment (stdlib only).

Enabled only when the environment has APP_USERNAME, APP_PASSWORD_HASH and APP_SECRET (e.g. the systemd
EnvironmentFile on the server). Without them the app behaves exactly as before (local use).
  * password: PBKDF2-SHA256, stored as  pbkdf2$<iterations>$<salt b64>$<hash b64>  (scripts/make_login.py makes it)
  * session : HMAC-SHA256 signed cookie "user|expiry", HttpOnly, SameSite=Strict, Secure when served over HTTPS
  * repeated failures are slowed down per client IP
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

COOKIE = "algo_session"
SESSION_SECONDS = 12 * 3600
_fails: dict[str, list[float]] = {}


def enabled() -> bool:
    return all(os.environ.get(k) for k in ("APP_USERNAME", "APP_PASSWORD_HASH", "APP_SECRET"))


def hash_password(password: str, iterations: int = 390_000) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, it, salt, want = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(dk, base64.b64decode(want))
    except (ValueError, TypeError):
        return False


def _sign(payload: str) -> str:
    return hmac.new(os.environ["APP_SECRET"].encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session(user: str, now: float | None = None) -> str:
    exp = int((now or time.time()) + SESSION_SECONDS)
    payload = f"{user}|{exp}"
    return f"{payload}|{_sign(payload)}"


def valid_session(token: str | None, now: float | None = None) -> bool:
    if not token or token.count("|") != 2:
        return False
    user, exp, sig = token.split("|")
    if not hmac.compare_digest(sig, _sign(f"{user}|{exp}")):
        return False
    return user == os.environ.get("APP_USERNAME") and exp.isdigit() and int(exp) > (now or time.time())


def throttled(ip: str, now: float | None = None) -> bool:
    now = now or time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 900]
    _fails[ip] = recent
    return len(recent) >= 5                    # 5 failures in 15 minutes -> wait


def record_failure(ip: str) -> None:
    _fails.setdefault(ip, []).append(time.time())


def login(user: str, password: str) -> bool:
    return hmac.compare_digest(user.encode(), os.environ.get("APP_USERNAME", "").encode()) and \
        check_password(password, os.environ.get("APP_PASSWORD_HASH", ""))
