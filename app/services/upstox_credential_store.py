"""
Credential store for Upstox, the same idea as angelone_credential_store.py: a developer connects Upstox from the Developer console.

Upstox logs in with OAuth, not with a PIN and a TOTP secret: the app's API key + secret + redirect URI identify the APP (saved once), and a person
logs in on Upstox's own page to get an ACCESS TOKEN, which Upstox ends every day at about 03:30 IST. So this store keeps two things:
the app details (long lived) and the access token (until the next 03:30). The file (upstox_credentials.json, gitignored) is ONE encrypted blob
(Fernet, key APP_ENC_KEY, see app/security/crypto.py). The API secret and the token are never sent to a browser.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path

from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ValidationError

from app.security import crypto

CREDENTIAL_PATH = Path("upstox_credentials.json")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
log = logging.getLogger("app.credentials")


class UpstoxCredentials(BaseModel):
    api_key: str
    api_secret: str
    redirect_uri: str
    access_token: str | None = None
    token_expires_at: str | None = None      # ISO time in IST: Upstox ends the token at the next 03:30
    user_id: str | None = None               # who logged in on Upstox (their id and name, for the status line)
    user_name: str | None = None
    connected_at: str | None = None          # set only after a real, successful login
    updated_by: str | None = None            # the developer (username) who last changed it


def next_token_expiry(now: dt.datetime | None = None) -> dt.datetime:
    """A token issued now stops working at the next 03:30 IST."""
    now = (now or dt.datetime.now(IST)).astimezone(IST)
    end = now.replace(hour=3, minute=30, second=0, microsecond=0)
    return end if end > now else end + dt.timedelta(days=1)


def get() -> UpstoxCredentials | None:
    if not CREDENTIAL_PATH.exists():
        return None
    try:
        raw = json.loads(crypto.decrypt(json.loads(CREDENTIAL_PATH.read_text())["enc"]))
        return UpstoxCredentials.model_validate(raw)
    except (ValueError, KeyError, InvalidToken, ValidationError, OSError) as exc:
        log.warning("upstox_credentials.json cannot be read (%s: wrong APP_ENC_KEY, or a damaged file): ignoring it", type(exc).__name__)
        return None


def save(credentials: UpstoxCredentials) -> None:
    tmp = CREDENTIAL_PATH.with_name(CREDENTIAL_PATH.name + ".tmp")
    tmp.write_text(json.dumps({"enc": crypto.encrypt(json.dumps(credentials.model_dump()))}))
    try:
        os.chmod(tmp, 0o600)                  # owner-only where the OS supports it (no effect on Windows)
    except OSError:
        pass
    tmp.replace(CREDENTIAL_PATH)              # never a half-written file


def clear() -> None:
    if CREDENTIAL_PATH.exists():
        CREDENTIAL_PATH.unlink()


def mask(text: str) -> str:
    return text if len(text) <= 6 else text[:3] + "•" * (len(text) - 5) + text[-2:]


def token_state(c: UpstoxCredentials | None, now: dt.datetime | None = None) -> str:
    """none (no app details) | app_only (details saved, nobody logged in) | connected | expired (the token ended: log in again)"""
    if c is None:
        return "none"
    if not c.access_token:
        return "app_only"
    try:
        ends = dt.datetime.fromisoformat(c.token_expires_at) if c.token_expires_at else None
    except ValueError:
        ends = None
    return "expired" if ends is not None and (now or dt.datetime.now(IST)) >= ends else "connected"


def status(now: dt.datetime | None = None) -> dict:
    """What the Developer console may show: no secret, no token, no full key."""
    c = get()
    return {
        "state": token_state(c, now),
        "api_key": mask(c.api_key) if c else None,
        "redirect_uri": c.redirect_uri if c else None,
        "user_id": c.user_id if c else None,
        "user_name": c.user_name if c else None,
        "connected_at": c.connected_at if c else None,
        "token_expires_at": c.token_expires_at if c else None,
        "updated_by": c.updated_by if c else None,
    }
