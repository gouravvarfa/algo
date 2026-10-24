"""User lifecycle and credential checks.

  INVITED --(setup link: password + QR + first code)--> ACTIVE <--(enable / disable)--> DISABLED
  INVITED / ACTIVE / DISABLED --(remove)--> DELETED (soft: the row and its history stay)

Nobody signs up on their own: an admin (or scripts/create_owner.py for the very first user) creates the user and gets a one-time setup
link, valid 24 h and usable once. Only the SHA-256 of the link is stored. Functions raise AuthError for anything the caller should show,
and InvalidCredentials for a failed login (the caller never says which part was wrong).
"""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
import uuid

from app.db.platform_db import write_tx
from app.security import authz, crypto, passwords, sessions, totp

INVITE_TTL_SECONDS = 24 * 3600
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")


class AuthError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class InvalidCredentials(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason                 # for the audit log only; never sent to the client


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _insert_invite(c: sqlite3.Connection, user_id: str, now: float) -> str:
    """Replace any unused link of this user with a fresh one. Caller owns the transaction."""
    c.execute("DELETE FROM user_invites WHERE user_id=? AND used_at IS NULL", (user_id,))
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO user_invites (id, user_id, token_hash, expires_at) VALUES (?,?,?,?)",
              (uuid.uuid4().hex, user_id, _hash(token), now + INVITE_TTL_SECONDS))
    return token


def get_user(c: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    return c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def create_user(c: sqlite3.Connection, *, name: str, username: str, email: str | None, role: str, created_by: str | None,
                now: float | None = None) -> tuple[sqlite3.Row, str]:
    """Create an INVITED user. Returns (user row, one-time setup token)."""
    now = time.time() if now is None else now
    name, username, email = (name or "").strip(), (username or "").strip().lower(), (email or "").strip() or None
    if not name or len(name) > 100:
        raise AuthError(400, "INVALID_NAME", "Name is required (at most 100 characters).")
    if not _USERNAME_RE.match(username):
        raise AuthError(400, "INVALID_USERNAME", "Username must be 3-32 characters: lowercase letters, digits, dot, dash, underscore.")
    if role not in authz.ROLES:
        raise AuthError(400, "INVALID_ROLE", "Role must be user, admin or developer.")
    if email and len(email) > 200:
        raise AuthError(400, "INVALID_EMAIL", "Email is too long.")
    uid = uuid.uuid4().hex
    try:
        with write_tx(c):
            c.execute("INSERT INTO users (id, username, name, email, role, status, created_by, created_at) VALUES (?,?,?,?,?,'INVITED',?,?)",
                      (uid, username, name, email, role, created_by, now))
            token = _insert_invite(c, uid, now)
    except sqlite3.IntegrityError:
        raise AuthError(409, "USERNAME_TAKEN", "That username is already taken (the names of removed users stay reserved). Pick another one, e.g. add a number.") from None
    return get_user(c, uid), token


def issue_invite(c: sqlite3.Connection, user_id: str, now: float | None = None) -> str:
    now = time.time() if now is None else now
    with write_tx(c):
        c.execute("UPDATE users SET totp_secret_enc=NULL WHERE id=? AND status='INVITED'", (user_id,))     # a new link gets a new QR code
        return _insert_invite(c, user_id, now)


def _find_invite(c: sqlite3.Connection, token: str, now: float) -> tuple[sqlite3.Row, sqlite3.Row]:
    bad = AuthError(404, "INVALID_LINK", "This setup link is invalid, already used, or has expired. Ask your admin for a new one.")
    if not token or len(token) > 200:
        raise bad
    inv = c.execute("SELECT * FROM user_invites WHERE token_hash=?", (_hash(token),)).fetchone()
    if inv is None or inv["used_at"] is not None or inv["expires_at"] <= now:
        raise bad
    user = get_user(c, inv["user_id"])
    if user is None or user["status"] != "INVITED":
        raise bad
    return inv, user


def begin_setup(c: sqlite3.Connection, token: str, now: float | None = None) -> dict:
    """Show the setup page's data. The authenticator secret is made on the first call and then kept for this invite, so reloading the page
    or opening the link in a second tab shows the SAME QR code (a new secret on every load made codes from an earlier scan fail).
    A new invite (admin reset / reset_user.py) clears the secret, so a new link always gets a new one."""
    now = time.time() if now is None else now
    _, user = _find_invite(c, token, now)
    if user["status"] == "INVITED" and user["totp_secret_enc"]:
        secret = crypto.decrypt(user["totp_secret_enc"])
    else:
        secret = totp.new_secret()
        with c:
            c.execute("UPDATE users SET totp_secret_enc=? WHERE id=?", (crypto.encrypt(secret), user["id"]))
    uri = totp.provisioning_uri(secret, user["username"])
    return {"name": user["name"], "username": user["username"], "qr_svg": totp.qr_svg(uri),
            "secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))}


def complete_setup(c: sqlite3.Connection, token: str, password: str, code: str, now: float | None = None) -> tuple[sqlite3.Row, list[str]]:
    """Set the password, confirm the authenticator with its first code, activate the user. Returns (user, backup codes to show once)."""
    now = time.time() if now is None else now
    inv, user = _find_invite(c, token, now)
    problem = passwords.policy_error(password)
    if problem:
        raise AuthError(400, "WEAK_PASSWORD", problem)
    if not user["totp_secret_enc"]:
        raise AuthError(400, "SETUP_NOT_STARTED", "Reload the setup page to get a QR code first.")
    step = totp.match_step(crypto.decrypt(user["totp_secret_enc"]), (code or "").strip(), now, None)
    if step is None:
        raise AuthError(400, "INVALID_CODE", "That code is not correct. Wait for the next code in your app and try again.")
    plain = totp.new_backup_codes()
    hashed = [totp.hash_backup_code(x) for x in plain]            # slow on purpose; done before the write lock is taken
    pw_hash = passwords.hash_password(password)
    with write_tx(c):
        if c.execute("UPDATE user_invites SET used_at=? WHERE id=? AND used_at IS NULL", (now, inv["id"])).rowcount != 1:
            raise AuthError(404, "INVALID_LINK", "This setup link was just used.")
        c.execute("UPDATE users SET password_hash=?, status='ACTIVE', totp_last_used_step=? WHERE id=?", (pw_hash, step, user["id"]))
        c.execute("DELETE FROM backup_codes WHERE user_id=?", (user["id"],))
        c.executemany("INSERT INTO backup_codes (id, user_id, code_hash) VALUES (?,?,?)", [(uuid.uuid4().hex, user["id"], h) for h in hashed])
    return get_user(c, user["id"]), [totp.display_code(x) for x in plain]


def authenticate_with_undo(c: sqlite3.Connection, username: str, password: str, code: str, now: float | None = None):
    """Password AND second factor (6-digit TOTP, or a one-time backup code). Returns (user, undo). Raises InvalidCredentials; the caller must
    give one generic message. The second factor is spent as soon as it is accepted (so a stolen code cannot be replayed). If the login then
    fails for a reason that is not the user's fault (another device is already signed in), the caller calls undo() to give the code back."""
    now = time.time() if now is None else now
    user = c.execute("SELECT * FROM users WHERE username=?", ((username or "").strip(),)).fetchone()
    if user is None or user["status"] != "ACTIVE" or not user["password_hash"] or not user["totp_secret_enc"]:
        passwords.burn_time(password[:passwords.MAX_LENGTH])
        raise InvalidCredentials("unknown_or_inactive_user")
    if len(password) > passwords.MAX_LENGTH or not passwords.verify_password(user["password_hash"], password):
        raise InvalidCredentials("bad_password")
    code = (code or "").strip()
    if totp.is_totp_shaped(code):
        previous = user["totp_last_used_step"]
        step = totp.match_step(crypto.decrypt(user["totp_secret_enc"]), code, now, previous)
        if step is None:
            raise InvalidCredentials("bad_code")
        with write_tx(c):      # claim the step: of two simultaneous logins with the same code, only one can win
            won = c.execute("UPDATE users SET totp_last_used_step=? WHERE id=? AND (totp_last_used_step IS NULL OR totp_last_used_step < ?)",
                            (step, user["id"], step)).rowcount == 1
        if not won:
            raise InvalidCredentials("code_replayed")

        def undo() -> None:
            with write_tx(c):
                c.execute("UPDATE users SET totp_last_used_step=? WHERE id=? AND totp_last_used_step=?", (previous, user["id"], step))
        return user, undo
    for row in c.execute("SELECT id, code_hash FROM backup_codes WHERE user_id=? AND used_at IS NULL", (user["id"],)).fetchall():
        if totp.verify_backup_code(row["code_hash"], code):
            with write_tx(c):
                won = c.execute("UPDATE backup_codes SET used_at=? WHERE id=? AND used_at IS NULL", (now, row["id"])).rowcount == 1
            if won:
                def undo(row_id=row["id"]) -> None:
                    with write_tx(c):
                        c.execute("UPDATE backup_codes SET used_at=NULL WHERE id=? AND used_at=?", (row_id, now))
                return user, undo
            break
    raise InvalidCredentials("bad_code")


def authenticate(c: sqlite3.Connection, username: str, password: str, code: str, now: float | None = None) -> sqlite3.Row:
    return authenticate_with_undo(c, username, password, code, now)[0]


def change_password(c: sqlite3.Connection, user_id: str, current: str, new: str) -> None:
    """A signed-in user changes their own password. The current password must be given again (a stolen session alone cannot do it)."""
    user = get_user(c, user_id)
    if user is None or user["status"] != "ACTIVE":
        raise AuthError(404, "NOT_FOUND", "No such user.")
    if len(current) > passwords.MAX_LENGTH or not passwords.verify_password(user["password_hash"], current):
        raise InvalidCredentials("bad_current_password")
    problem = passwords.policy_error(new)
    if problem:
        raise AuthError(400, "WEAK_PASSWORD", problem)
    if new == current:
        raise AuthError(400, "SAME_PASSWORD", "The new password must be different from the current one.")
    with write_tx(c):
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (passwords.hash_password(new), user_id))


# ---- admin operations --------------------------------------------------------------------------------------------------------------
def _target(c: sqlite3.Connection, actor, target_id: str) -> sqlite3.Row:
    t = get_user(c, target_id)
    if t is None or t["status"] == "DELETED":
        raise AuthError(404, "NOT_FOUND", "No such user.")
    if not authz.can_manage(actor, t):
        raise AuthError(403, "FORBIDDEN", "You cannot manage this user.")
    return t


def _not_last_admin(c: sqlite3.Connection, t: sqlite3.Row) -> None:
    """Never leave the platform without anyone who can administer it (lock-out protection)."""
    if t["status"] != "ACTIVE" or t["role"] == "user":
        return
    admins = c.execute("SELECT COUNT(*) FROM users WHERE id!=? AND status='ACTIVE' AND role IN ('admin','developer')", (t["id"],)).fetchone()[0]
    if admins == 0:
        raise AuthError(409, "LAST_ADMIN", "This is the last active admin; the platform would be locked out.")
    if t["role"] == "developer" and c.execute("SELECT COUNT(*) FROM users WHERE id!=? AND status='ACTIVE' AND role='developer'", (t["id"],)).fetchone()[0] == 0:
        raise AuthError(409, "LAST_DEVELOPER", "This is the last active developer; the platform would be locked out.")


def list_users(c: sqlite3.Connection, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    out = []
    for r in c.execute("SELECT u.id, u.username, u.name, u.email, u.role, u.status, u.created_at, s.device_label, s.started_at, s.last_seen_at "
                       "FROM users u LEFT JOIN sessions s ON s.user_id = u.id AND s.ended_at IS NULL "
                       "WHERE u.status != 'DELETED' ORDER BY u.created_at"):
        online = r["last_seen_at"] is not None and now - r["last_seen_at"] < sessions.HEARTBEAT_DEAD
        out.append({"id": r["id"], "username": r["username"], "name": r["name"], "email": r["email"], "role": r["role"], "status": r["status"],
                    "online": online, "device": r["device_label"] if online else None,
                    "last_active": sessions.iso_ist(r["last_seen_at"]) if r["last_seen_at"] else None})
    return out


def set_status(c: sqlite3.Connection, actor, target_id: str, status: str, now: float | None = None) -> sqlite3.Row:
    now = time.time() if now is None else now
    t = _target(c, actor, target_id)
    if status == "DISABLED":
        if t["status"] not in ("ACTIVE", "INVITED"):
            raise AuthError(409, "BAD_STATE", "Only an active or invited user can be disabled.")
        _not_last_admin(c, t)
        with write_tx(c):
            c.execute("UPDATE users SET status='DISABLED', disabled_at=? WHERE id=?", (now, t["id"]))
            c.execute("UPDATE sessions SET ended_at=?, end_reason='disabled' WHERE user_id=? AND ended_at IS NULL", (now, t["id"]))
    elif status == "ACTIVE":
        if t["status"] != "DISABLED":
            raise AuthError(409, "BAD_STATE", "Only a disabled user can be enabled.")
        back_to = "ACTIVE" if (t["password_hash"] and t["totp_secret_enc"]) else "INVITED"   # never finished setup -> needs a link again
        with write_tx(c):
            c.execute("UPDATE users SET status=?, disabled_at=NULL WHERE id=?", (back_to, t["id"]))
    else:
        raise AuthError(400, "INVALID_STATUS", "Status must be ACTIVE or DISABLED.")
    return get_user(c, t["id"])


def delete_user(c: sqlite3.Connection, actor, target_id: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    t = _target(c, actor, target_id)
    _not_last_admin(c, t)
    with write_tx(c):
        c.execute("UPDATE users SET status='DELETED', deleted_at=? WHERE id=?", (now, t["id"]))
        c.execute("UPDATE sessions SET ended_at=?, end_reason='disabled' WHERE user_id=? AND ended_at IS NULL", (now, t["id"]))
        c.execute("UPDATE user_invites SET used_at=? WHERE user_id=? AND used_at IS NULL", (now, t["id"]))


def force_logout(c: sqlite3.Connection, actor, target_id: str, now: float | None = None) -> int:
    t = _target(c, actor, target_id)
    return sessions.end_all(c, t["id"], "forced", now)


def reset_totp(c: sqlite3.Connection, actor, target_id: str, now: float | None = None) -> tuple[sqlite3.Row, str]:
    """Lost phone: discard the old secret and backup codes, close every session, block login until the new setup link is used."""
    now = time.time() if now is None else now
    t = _target(c, actor, target_id)
    if t["status"] == "DISABLED":
        raise AuthError(409, "BAD_STATE", "Enable the user first.")
    _not_last_admin(c, t)
    token = _reset_credentials(c, t["id"], now)
    return get_user(c, t["id"]), token


def _reset_credentials(c: sqlite3.Connection, user_id: str, now: float) -> str:
    """Throw away the authenticator secret and backup codes, close every session, block login, and issue a new setup link. In that link
    the person chooses a NEW password and scans a NEW QR code, so this is also how a forgotten password is replaced."""
    with write_tx(c):
        c.execute("UPDATE users SET totp_secret_enc=NULL, totp_last_used_step=NULL, status='INVITED' WHERE id=?", (user_id,))
        c.execute("DELETE FROM backup_codes WHERE user_id=?", (user_id,))
        c.execute("UPDATE sessions SET ended_at=?, end_reason='reset' WHERE user_id=? AND ended_at IS NULL", (now, user_id))
        return _insert_invite(c, user_id, now)


def reset_account_as_operator(c: sqlite3.Connection, username: str, now: float | None = None) -> tuple[sqlite3.Row, str]:
    """For the person who runs the server (scripts/reset_user.py): the way back in for someone nobody above them can reset, such as the
    owner locked out of their own account. Whoever can run this already controls the machine and the database, so it asks for no login."""
    now = time.time() if now is None else now
    user = c.execute("SELECT * FROM users WHERE username=?", ((username or "").strip(),)).fetchone()
    if user is None or user["status"] == "DELETED":
        raise AuthError(404, "NOT_FOUND", f"No user named '{username}'.")
    return get_user(c, user["id"]), _reset_credentials(c, user["id"], now)
