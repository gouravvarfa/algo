"""A user's own broker credentials (Angel One), encrypted at rest. The secrets go in and are used on the server; no API ever returns them.

What is stored: client code, SmartAPI key, trading PIN and the TOTP secret (the base32 text behind the authenticator QR). They are needed to log in
for the user every day without anyone typing a code. `mode` stays 'paper': switching to real orders is a later step with its own safeguards.
"""
from __future__ import annotations

import json
import re
import time

import pyotp

from app.ops import flags
from app.security import crypto

SYMBOLS = ("NIFTY", "BANKNIFTY")
CODE_RE = re.compile(r"^[A-Za-z0-9]{3,20}$")
MAX_REAL_LOTS = 50                     # the person chooses the lots per instrument; this only stops a typo like 500
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


def blockers(r) -> list[str]:
    """Everything that currently stops this account from sending a real order (empty = real orders may be sent). Shown on screen, so a person sees why."""
    out = []
    if r is None:
        return ["No Angel One account is connected."]
    if not flags.get("real.enabled"):
        out.append("Real orders are switched off for everyone (master switch).")
    if not r["real_allowed"]:
        out.append("An admin has not allowed real orders for this account.")
    if r["status"] != "connected":
        out.append("The Angel One login has not been verified (use Test connection).")
    return out


def effective_mode(r) -> str:
    """'real' only when the person chose real AND nothing blocks it. Everything else is paper, whatever was clicked."""
    return "real" if r is not None and r["mode"] == "real" and not blockers(r) else "paper"


def limits(r) -> dict:
    syms = json.loads(r["allowed_symbols"]) if r["allowed_symbols"] else list(SYMBOLS)
    return {"max_lots": r["max_lots"], "max_daily_loss": r["max_daily_loss"], "allowed_symbols": syms}


def public(r) -> dict:
    """What a screen may see: that it is set up, who it is, the mode and how the last check went. Never a secret (not even the last digits of one)."""
    master = bool(flags.get("real.enabled"))
    if r is None:
        return {"configured": False, "broker": "angelone", "mode": "paper", "effective_mode": "paper", "status": "not_set", "status_message": None,
                "real_allowed": False, "real_master_on": master, "real_blockers": blockers(None)}
    return {"configured": True, "broker": r["broker"], "client_code": mask(r["client_code"]), "mode": r["mode"], "effective_mode": effective_mode(r),
            "status": r["status"], "status_message": r["status_message"], "last_checked_at": r["last_checked_at"], "updated_at": r["updated_at"],
            "updated_by": r["updated_by"], "real_allowed": bool(r["real_allowed"]), "real_master_on": master, "options_orders": bool(flags.get("real.options")), "real_blockers": blockers(r),
            **limits(r), "mode_changed_at": r["mode_changed_at"], "mode_changed_by": r["mode_changed_by"]}


def set_mode(c, user_id: str, mode: str, actor: str) -> None:
    """Switch paper/real. Real needs every blocker cleared; paper is always allowed."""
    r = row(c, user_id)
    if r is None:
        raise BrokerError(404, "NOT_SET", "Connect your Angel One account first.")
    if mode not in ("paper", "real"):
        raise BrokerError(400, "BAD_MODE", "Mode is paper or real.")
    if mode == "real":
        b = blockers(r)
        if b:
            raise BrokerError(403, "REAL_BLOCKED", " ".join(b))
    c.execute("UPDATE user_broker SET mode=?, mode_changed_at=?, mode_changed_by=? WHERE user_id=?", (mode, time.time(), actor, user_id))
    if mode == "paper":
        c.execute("UPDATE user_instrument_real SET real_on=0, changed_at=?, changed_by=? WHERE user_id=? AND real_on=1", (time.time(), actor, user_id))   # coming back later never re-arms them
    c.commit()


def grant(c, user_id: str, allowed: bool, max_lots: int, max_daily_loss: float | None, symbols: list[str] | None, actor: str) -> None:
    """An admin allows (or withdraws) real orders for one account and sets its limits. Withdrawing also puts the account back to paper at once."""
    r = row(c, user_id)
    if r is None:
        raise BrokerError(404, "NOT_SET", "This user has not connected an Angel One account.")
    syms = [x.upper() for x in symbols] if symbols is not None else None
    if syms is not None and (not syms or any(x not in SYMBOLS for x in syms)):
        raise BrokerError(400, "BAD_SYMBOL", f"symbols must be among {list(SYMBOLS)}")
    if not 1 <= max_lots <= 50:
        raise BrokerError(400, "BAD_LOTS", "Max lots must be between 1 and 50.")
    if max_daily_loss is not None and max_daily_loss <= 0:
        raise BrokerError(400, "BAD_LOSS", "Daily loss limit must be a positive amount (or empty for no cap).")
    c.execute("UPDATE user_broker SET real_allowed=?, max_lots=?, max_daily_loss=?, allowed_symbols=?, "
              "mode=CASE WHEN ?=0 THEN 'paper' ELSE mode END, mode_changed_at=CASE WHEN ?=0 AND mode='real' THEN ? ELSE mode_changed_at END, "
              "mode_changed_by=CASE WHEN ?=0 AND mode='real' THEN ? ELSE mode_changed_by END WHERE user_id=?",
              (1 if allowed else 0, max_lots, max_daily_loss, json.dumps(syms) if syms is not None else None, 1 if allowed else 0, 1 if allowed else 0, time.time(),
               1 if allowed else 0, actor, user_id))
    if not allowed:
        c.execute("UPDATE user_instrument_real SET real_on=0, changed_at=?, changed_by=? WHERE user_id=? AND real_on=1", (time.time(), actor, user_id))
    c.commit()


def force_all_paper(c, actor: str) -> int:
    """The kill switch: every account back to paper. Returns how many were on real."""
    n = c.execute("UPDATE user_broker SET mode='paper', mode_changed_at=?, mode_changed_by=? WHERE mode='real'", (time.time(), actor)).rowcount
    c.execute("UPDATE user_instrument_real SET real_on=0, changed_at=?, changed_by=? WHERE real_on=1", (time.time(), actor))
    c.commit()
    return n


def instrument_switches(c, user_id: str) -> dict[str, bool]:
    return {r["instrument_id"]: bool(r["real_on"]) for r in c.execute("SELECT instrument_id, real_on FROM user_instrument_real WHERE user_id=?", (user_id,))}


def instrument_set(c, user_id: str, instrument_id: str, symbol: str, on: bool, actor: str, lots: int | None = None) -> None:
    """The person's real-order switch for one instrument. OFF is always allowed; ON only while the whole account is in effective real mode."""
    if on:
        r = row(c, user_id)
        if effective_mode(r) != "real":
            raise BrokerError(403, "REAL_BLOCKED", "Real orders cannot be switched on for an instrument: " + (" ".join(blockers(r)) or "your account is on paper. Switch the account to REAL in Settings first."))
        if lots is not None and not (1 <= int(lots) <= MAX_REAL_LOTS):
            raise BrokerError(400, "LOTS", f"Lots must be between 1 and {MAX_REAL_LOTS}.")
    c.execute("INSERT INTO user_instrument_real (user_id, instrument_id, symbol, real_on, changed_at, changed_by) VALUES (?,?,?,?,?,?) "
              "ON CONFLICT(user_id, instrument_id) DO UPDATE SET real_on=excluded.real_on, symbol=excluded.symbol, changed_at=excluded.changed_at, changed_by=excluded.changed_by",
              (user_id, instrument_id, symbol, 1 if on else 0, time.time(), actor))
    if on and lots is not None:
        c.execute("UPDATE user_instrument_real SET real_lots=? WHERE user_id=? AND instrument_id=?", (int(lots), user_id, instrument_id))
    c.commit()


def instrument_lots(c, user_id: str, instrument_id: str) -> int:
    r = c.execute("SELECT real_lots FROM user_instrument_real WHERE user_id=? AND instrument_id=?", (user_id, instrument_id)).fetchone()
    return int(r["real_lots"]) if r and r["real_lots"] else 1


def instrument_real(c, user_id: str, instrument_id: str) -> bool:
    """True only when this instrument's switch is on AND the account is still in effective real mode. The question the order step will ask before every order."""
    on = c.execute("SELECT real_on FROM user_instrument_real WHERE user_id=? AND instrument_id=?", (user_id, instrument_id)).fetchone()
    return bool(on and on["real_on"]) and effective_mode(row(c, user_id)) == "real"


def throttle(user_id: str) -> None:
    now = time.monotonic()
    last = _last_call.get(user_id)
    if last is not None and now - last < MIN_GAP_S:
        raise BrokerError(429, "TOO_FAST", f"Please wait {int(MIN_GAP_S - (now - last)) + 1} seconds before trying again (Angel One limits logins).")
    _last_call[user_id] = now


def set_status(c, user_id: str, status: str, message: str | None) -> None:
    c.execute("UPDATE user_broker SET status=?, status_message=?, last_checked_at=? WHERE user_id=?", (status, (message or "")[:300] or None, time.time(), user_id))
    c.commit()
