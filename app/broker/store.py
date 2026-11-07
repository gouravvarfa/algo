"""A user's own broker credentials (Angel One), encrypted at rest. The secrets go in and are used on the server; no API ever returns them.

What is stored: client code, SmartAPI key, trading PIN and the TOTP secret (the base32 text behind the authenticator QR). They are needed to log in
for the user every day without anyone typing a code. `mode` stays 'paper': switching to real orders is a later step with its own safeguards.
"""
from __future__ import annotations

import re
import time

import pyotp

from app.security import crypto

CODE_RE = re.compile(r"^[A-Za-z0-9]{3,20}$")
MIN_GAP_S = 15.0                       # at most one live Angel One login per user per this many seconds (their login limit is strict)
_last_call: dict[str, float] = {}


class BrokerError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def clean_totp(secret: str) -> str:
    s = re.sub(r"\s+", "", secret or "").upper()
    try:
        pyotp.TOTP(s).now()
    except Exception as exc:
        raise BrokerError(400, "BAD_TOTP", "The TOTP secret must be the base32 text shown next to the authenticator QR code (letters A-Z and digits 2-7).") from exc
    return s


def validate(client_code: str, api_key: str, pin: str, totp_secret: str) -> tuple[str, str, str, str]:
    client_code, api_key, pin = (client_code or "").strip(), (api_key or "").strip(), (pin or "").strip()
    if not CODE_RE.match(client_code):
        raise BrokerError(400, "BAD_CLIENT_CODE", "Client code is your Angel One login id (letters and digits).")
    if len(api_key) < 6 or len(api_key) > 64 or " " in api_key:
        raise BrokerError(400, "BAD_API_KEY", "That does not look like a SmartAPI key.")
    if not pin.isdigit() or not 3 <= len(pin) <= 8:
        raise BrokerError(400, "BAD_PIN", "The Angel One PIN is made of digits only.")
    return client_code, api_key, pin, clean_totp(totp_secret)


def save(c, user_id: str, client_code: str, api_key: str, pin: str, totp_secret: str, actor_id: str) -> None:
    client_code, api_key, pin, totp = validate(client_code, api_key, pin, totp_secret)
    now = time.time()
    c.execute("INSERT INTO user_broker (user_id, broker, client_code, api_key_enc, pin_enc, totp_enc, mode, status, status_message, last_checked_at, updated_by, updated_at) "
              "VALUES (?, 'angelone', ?, ?, ?, ?, 'paper', 'untested', NULL, NULL, ?, ?) "
              "ON CONFLICT(user_id) DO UPDATE SET client_code=excluded.client_code, api_key_enc=excluded.api_key_enc, pin_enc=excluded.pin_enc, totp_enc=excluded.totp_enc, "
              "mode='paper', status='untested', status_message=NULL, last_checked_at=NULL, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
              (user_id, client_code, crypto.encrypt(api_key), crypto.encrypt(pin), crypto.encrypt(totp), actor_id, now))
    c.commit()
    _last_call.pop(user_id, None)


def delete(c, user_id: str) -> bool:
    n = c.execute("DELETE FROM user_broker WHERE user_id=?", (user_id,)).rowcount
    c.commit()
    _last_call.pop(user_id, None)
    return n > 0


def row(c, user_id: str):
    return c.execute("SELECT * FROM user_broker WHERE user_id=?", (user_id,)).fetchone()


def secrets(r) -> dict:
    """Decrypted login details. For the server's own login call only; never put the result in a response or a log."""
    return {"client_code": r["client_code"], "api_key": crypto.decrypt(r["api_key_enc"]), "pin": crypto.decrypt(r["pin_enc"]), "totp_secret": crypto.decrypt(r["totp_enc"])}


def mask(text: str, keep: int = 2) -> str:
    return text[:keep] + "•" * max(4, len(text) - keep) if text else ""


def public(r) -> dict:
    """What a screen may see: that it is set up, who it is, and how the last check went. Never a secret (not even the last digits of one)."""
    if r is None:
        return {"configured": False, "broker": "angelone", "mode": "paper", "status": "not_set", "status_message": None}
    return {"configured": True, "broker": r["broker"], "client_code": mask(r["client_code"]), "mode": r["mode"], "status": r["status"],
            "status_message": r["status_message"], "last_checked_at": r["last_checked_at"], "updated_at": r["updated_at"], "updated_by": r["updated_by"]}


def throttle(user_id: str) -> None:
    now = time.monotonic()
    last = _last_call.get(user_id)
    if last is not None and now - last < MIN_GAP_S:
        raise BrokerError(429, "TOO_FAST", f"Please wait {int(MIN_GAP_S - (now - last)) + 1} seconds before trying again (Angel One limits logins).")
    _last_call[user_id] = now


def set_status(c, user_id: str, status: str, message: str | None) -> None:
    c.execute("UPDATE user_broker SET status=?, status_message=?, last_checked_at=? WHERE user_id=?", (status, (message or "")[:300] or None, time.time(), user_id))
    c.commit()
