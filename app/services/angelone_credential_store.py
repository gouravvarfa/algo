"""
Local credential store for Angel One, so the user can connect from the
frontend instead of hand-editing .env. Falls back to .env values
(settings.angelone_*) if no saved file exists, so either path still works.

Local-dev-only storage: plaintext JSON on disk (gitignored) — appropriate
for a single-user local app, not a multi-tenant deployment.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from app.core.config import settings

CREDENTIAL_PATH = Path("angelone_credentials.json")


class AngelOneCredentials(BaseModel):
    api_key: str
    client_code: str
    pin: str
    totp_secret: str
    # Set only on a real, verified successful login — never backdated,
    # never set just because credentials were saved.
    connected_at: str | None = None


def get_credentials() -> AngelOneCredentials | None:
    if CREDENTIAL_PATH.exists():
        return AngelOneCredentials.model_validate(json.loads(CREDENTIAL_PATH.read_text()))
    if settings.angelone_configured:
        return AngelOneCredentials(
            api_key=settings.angelone_api_key,
            client_code=settings.angelone_client_code,
            pin=settings.angelone_pin,
            totp_secret=settings.angelone_totp_secret,
        )
    return None


def save_credentials(credentials: AngelOneCredentials) -> None:
    CREDENTIAL_PATH.write_text(json.dumps(credentials.model_dump(), indent=2))


def clear_credentials() -> None:
    if CREDENTIAL_PATH.exists():
        CREDENTIAL_PATH.unlink()


def is_configured() -> bool:
    return get_credentials() is not None
