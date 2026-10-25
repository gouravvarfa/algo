"""
Credential store for Angel One, so a developer can connect from the Developer console instead of editing .env on the server.
Saved details win over the .env values (settings.angelone_*); with no saved file the .env values are used, so either path still works.

The file (angelone_credentials.json, gitignored) holds ONE encrypted blob (Fernet, the same key as the authenticator secrets: APP_ENC_KEY,
see app/security/crypto.py). A file written by an older version, with the four values in plain text, is still read, and is replaced by an
encrypted one the next time the details are saved. The PIN and the TOTP secret are never sent to a browser: status() returns only a masked
client code and the dates.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ValidationError

from app.core.config import settings
from app.security import crypto

CREDENTIAL_PATH = Path("angelone_credentials.json")
log = logging.getLogger("app.credentials")


class AngelOneCredentials(BaseModel):
    api_key: str
    client_code: str
    pin: str
    totp_secret: str
    # Set only on a real, verified successful login — never backdated,
    # never set just because credentials were saved.
    connected_at: str | None = None
    updated_by: str | None = None          # the developer who saved them (username), for the status line


def _read_saved() -> AngelOneCredentials | None:
    if not CREDENTIAL_PATH.exists():
        return None
    try:
        raw = json.loads(CREDENTIAL_PATH.read_text())
        if "enc" in raw:
            raw = json.loads(crypto.decrypt(raw["enc"]))
        return AngelOneCredentials.model_validate(raw)
    except (ValueError, InvalidToken, ValidationError, OSError) as exc:
        log.warning("angelone_credentials.json cannot be read (%s: wrong APP_ENC_KEY, or a damaged file): ignoring it", type(exc).__name__)
        return None


def get_credentials() -> AngelOneCredentials | None:
    saved = _read_saved()
    if saved is not None:
        return saved
    if settings.angelone_configured:
        return AngelOneCredentials(
            api_key=settings.angelone_api_key,
            client_code=settings.angelone_client_code,
            pin=settings.angelone_pin,
            totp_secret=settings.angelone_totp_secret,
        )
    return None


def save_credentials(credentials: AngelOneCredentials) -> None:
    tmp = CREDENTIAL_PATH.with_name(CREDENTIAL_PATH.name + ".tmp")
    tmp.write_text(json.dumps({"enc": crypto.encrypt(json.dumps(credentials.model_dump()))}))
    try:
        os.chmod(tmp, 0o600)                  # owner-only where the OS supports it (no effect on Windows)
    except OSError:
        pass
    tmp.replace(CREDENTIAL_PATH)              # never a half-written file


def clear_credentials() -> None:
    if CREDENTIAL_PATH.exists():
        CREDENTIAL_PATH.unlink()


def is_configured() -> bool:
    return get_credentials() is not None


def _file_is_encrypted() -> bool:
    try:
        return "enc" in json.loads(CREDENTIAL_PATH.read_text())
    except (OSError, ValueError):
        return False


def mask_client_code(code: str) -> str:
    return code if len(code) <= 3 else code[0] + "•" * (len(code) - 3) + code[-2:]


def status() -> dict:
    """What the Developer console may show: where the details come from and a masked client code. Never the key, the PIN or the secret."""
    saved = _read_saved()
    creds = saved or get_credentials()
    return {
        "configured": creds is not None,
        "source": "saved" if saved else "env" if creds else "none",
        "client_code": mask_client_code(creds.client_code) if creds else None,
        "connected_at": creds.connected_at if creds else None,
        "updated_by": creds.updated_by if creds else None,
        "encrypted": _file_is_encrypted(),
    }
