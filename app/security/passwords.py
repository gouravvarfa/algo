"""Password hashing: Argon2id with a per-hash random salt (argon2-cffi defaults)."""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_LENGTH = 12
MAX_LENGTH = 200                     # a bound on the work an attacker can ask for with a huge "password"
_ph = PasswordHasher()
_dummy: str | None = None


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(stored: str | None, password: str) -> bool:
    if not stored:
        return False
    try:
        return _ph.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


def burn_time(password: str) -> None:
    """Spend the same time as a real check, so an unknown username cannot be told apart from a wrong password by timing."""
    global _dummy
    if _dummy is None:
        _dummy = _ph.hash("not-a-real-password")
    verify_password(_dummy, password)


def policy_error(password: str) -> str | None:
    if len(password) < MIN_LENGTH:
        return f"Password must be at least {MIN_LENGTH} characters."
    if len(password) > MAX_LENGTH:
        return f"Password must be at most {MAX_LENGTH} characters."
    if len(set(password)) < 5:
        return "Password is too repetitive."
    return None
