"""Every test here gets its own empty platform database, its own encryption key, a cheap Argon2 (the real cost would make the suite slow),
and no old-style login settings in the environment."""
import time

import pytest
from argon2 import PasswordHasher
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import main
from app.db import platform_db
from app.security import mode, passwords

CHROME_WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0"


@pytest.fixture(autouse=True)
def isolated_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_db, "DB", tmp_path / "platform.db")
    monkeypatch.setenv("APP_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(passwords, "_ph", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    monkeypatch.setattr(passwords, "_dummy", None)
    for k in ("APP_USERNAME", "APP_PASSWORD_HASH", "APP_SECRET"):
        monkeypatch.delenv(k, raising=False)
    mode.reset()
    yield
    mode.reset()


@pytest.fixture
def db():
    c = platform_db.connect()
    yield c
    c.close()


@pytest.fixture
def new_client():
    """A browser: its own cookie jar and user agent. Call it twice for two devices."""
    made = []

    def make(user_agent: str = CHROME_WINDOWS) -> TestClient:
        cl = TestClient(main.app, headers={"user-agent": user_agent})     # no lifespan: no broker session, no background engine
        made.append(cl)
        return cl

    yield make
    for cl in made:
        cl.close()


@pytest.fixture
def client(new_client):
    return new_client()


@pytest.fixture(autouse=True)
def not_near_a_totp_boundary():
    """A TOTP code changes every 30 s. A test that takes two codes and crosses that boundary in between would fail by bad luck, so a test
    never starts in the last 3 s of a window (every test here runs well under 3 s). Costs at most a 3 s pause now and then."""
    into_window = time.time() % 30
    if into_window > 27:
        time.sleep(30 - into_window + 0.2)
    yield

