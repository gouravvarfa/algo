"""Encryption of small secrets at rest (the TOTP secret) with Fernet (AES-128-CBC + HMAC).

The key comes from APP_ENC_KEY (a Fernet key: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())").
On a hosted server put it in the environment file next to APP_SECRET. If it is not set, a key is created once in data/enc.key so local use
works with no setup. Lose the key and every stored TOTP secret is unreadable (users would need a TOTP reset), so back it up with the database.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet

KEY_FILE = Path(__file__).resolve().parents[2] / "data" / "enc.key"
log = logging.getLogger("app.security")
_warned = False


def _key() -> bytes:
    env = os.environ.get("APP_ENC_KEY")
    if env:
        return env.encode()
    global _warned
    if not _warned:
        _warned = True
        log.warning("APP_ENC_KEY is not set: using the key file %s. That is fine on a development machine; on a hosted server set APP_ENC_KEY in "
                    "the environment, so the key is not stored next to the database it protects.", KEY_FILE)
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes().strip()
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    KEY_FILE.write_bytes(key)
    try:
        os.chmod(KEY_FILE, 0o600)          # owner-only where the OS supports it (no effect on Windows)
    except OSError:
        pass
    return key


def encrypt(text: str) -> str:
    return Fernet(_key()).encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    return Fernet(_key()).decrypt(token.encode()).decode()
