"""TOTP (RFC 6238, 30-second step, 6 digits) with replay protection, the setup QR code, and one-time backup codes."""
from __future__ import annotations

import hmac
import re
import secrets

import pyotp
import segno

from app.security import passwords

STEP_SECONDS = 30
TOLERANCE = 1                        # accept the previous and the next step too (clock drift)
ISSUER = "Algo Trading"
BACKUP_CODE_COUNT = 10
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"        # no 0/o/1/i/l: a code read off paper is not misread
_TOTP_RE = re.compile(r"^\d{6}$")


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER)


def qr_svg(uri: str) -> str:
    """The setup QR as an inline SVG (no image file, nothing leaves the server)."""
    return segno.make(uri, error="m").svg_inline(scale=5, border=2, dark="#0f172a", light="#ffffff")


def match_step(secret: str, code: str, now: float, last_used_step: int | None) -> int | None:
    """The time-step the code belongs to, or None if the code is wrong or its step was already used."""
    if not _TOTP_RE.match(code or ""):
        return None
    totp = pyotp.TOTP(secret, interval=STEP_SECONDS)
    current = int(now // STEP_SECONDS)
    for step in range(current - TOLERANCE, current + TOLERANCE + 1):
        if hmac.compare_digest(totp.at(step * STEP_SECONDS), code) and (last_used_step is None or step > last_used_step):
            return step
    return None


def is_totp_shaped(code: str) -> bool:
    return bool(_TOTP_RE.match(code or ""))


def new_backup_codes() -> list[str]:
    return ["".join(secrets.choice(_ALPHABET) for _ in range(8)) for _ in range(BACKUP_CODE_COUNT)]


def display_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def normalize_backup_code(raw: str) -> str:
    return re.sub(r"[\s-]", "", raw or "").lower()


def hash_backup_code(code: str) -> str:
    return passwords.hash_password(normalize_backup_code(code))


def verify_backup_code(stored: str, code: str) -> bool:
    return passwords.verify_password(stored, normalize_backup_code(code))
