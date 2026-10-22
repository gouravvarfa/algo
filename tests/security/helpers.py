import time
from dataclasses import dataclass, field

import pyotp

from app.security import mode, users

PASSWORD = "correct horse battery staple"


@dataclass
class Account:
    id: str
    username: str
    password: str
    secret: str
    role: str
    backup: list[str] = field(default_factory=list)

    def next_backup(self) -> str:
        """Each backup code works once; tests use them as extra one-time second factors (the TOTP of one 30 s window can be used only once)."""
        return self.backup.pop(0)


def make_user(c, username: str = "rahul", role: str = "user", created_by: str | None = "seed", password: str = PASSWORD,
              now: float | None = None) -> Account:
    """An ACTIVE user, set up through the real invite -> QR -> first-code flow. Setup happens an hour in the past, so the current
    time-step is still unused and the first login can use a normal code."""
    now = time.time() - 3600 if now is None else now
    user, token = users.create_user(c, name=username.title(), username=username, email=None, role=role, created_by=created_by, now=now)
    secret = users.begin_setup(c, token, now)["secret"].replace(" ", "")
    user, backup = users.complete_setup(c, token, password, pyotp.TOTP(secret).at(now), now)
    mode.reset()
    return Account(user["id"], username, password, secret, role, list(backup))


def make_owner(c, username: str = "owner") -> Account:
    return make_user(c, username, role="developer", created_by=None)


def totp_now(secret: str, steps: int = 0) -> str:
    return pyotp.TOTP(secret).at(time.time() + 30 * steps)


def login(client, acct: Account, code: str | None = None, password: str | None = None):
    return client.post("/api/auth/login", json={"username": acct.username, "password": password or acct.password,
                                                "totp": code if code is not None else totp_now(acct.secret)})


def api(client, method: str, url: str, **kw):
    """A request with the CSRF header, as the page script would send it."""
    headers = {**kw.pop("headers", {}), "X-CSRF-Token": client.cookies.get("algo_csrf") or ""}
    return client.request(method, url, headers=headers, **kw)
