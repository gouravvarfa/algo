"""HTTP side of the login system: login / logout / me / heartbeat, the one-time setup link, and the admin's user management.

Business rules live in app/security/*; this file only translates HTTP to those calls, sets the cookies, and writes the audit log.
Every error body is {"error": CODE, "message": text}. Routes here are plain `def`, so the slow Argon2 check runs in FastAPI's thread pool
and never blocks the live market-data loop.
"""
from __future__ import annotations

import hmac
import os
import re
import time
from typing import Iterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.db import audit, platform_db
from app.security import authz, mode, ratelimit, sessions, users
from app.security.users import AuthError

router = APIRouter()

PUBLIC_PATHS = {"/login", "/healthz", "/api/auth/login", "/api/auth/mode", "/app"}
PUBLIC_PREFIXES = ("/setup/", "/api/auth/setup/", "/app/")   # /app/ = the React shell and its files (no data in them: every API call is still checked)
_SAFE_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")


# ---- helpers -----------------------------------------------------------------------------------------------------------------------
def db() -> Iterator:
    c = platform_db.connect()
    try:
        yield c
    finally:
        c.close()


def err(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": code, "message": message, **extra}, status_code=status)


def handle_auth_error(_: Request, exc: AuthError) -> JSONResponse:
    return err(exc.status, exc.code, exc.message)


_LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def _behind_trusted_proxy(request: Request) -> bool:
    """X-Real-IP / X-Forwarded-Proto are written by a reverse proxy (nginx). They are believed only when the TCP peer IS that proxy: on the
    same machine (loopback) or listed in APP_TRUSTED_PROXIES (comma separated). From anyone else they are just text an attacker typed."""
    peer = request.client.host if request.client else ""
    listed = {p.strip() for p in os.environ.get("APP_TRUSTED_PROXIES", "").split(",") if p.strip()}
    return peer in _LOOPBACK or peer in listed


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "?"
    return (request.headers.get("x-real-ip") or peer) if _behind_trusted_proxy(request) else peer


def _secure(request: Request) -> bool:
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme) if _behind_trusted_proxy(request) else request.url.scheme
    return scheme == "https"


def _set_cookies(resp: JSONResponse, request: Request, token: str, csrf: str) -> None:
    # session id: invisible to scripts. CSRF token: readable by our own page script (it copies it into a request header);
    # an attacking site can neither read it (same-origin) nor have the browser send our cookies cross-site (SameSite=Strict).
    resp.set_cookie(sessions.COOKIE, token, max_age=sessions.ABSOLUTE_LIFETIME, httponly=True, samesite="strict", secure=_secure(request))
    resp.set_cookie(sessions.CSRF_COOKIE, csrf, max_age=sessions.ABSOLUTE_LIFETIME, httponly=False, samesite="strict", secure=_secure(request))


def clear_cookies(resp) -> None:
    resp.delete_cookie(sessions.COOKIE)
    resp.delete_cookie(sessions.CSRF_COOKIE)


def _typed(name: str) -> str:
    """A username for the audit log. If what was typed does not look like a username (e.g. a password pasted in the wrong box) it is not logged."""
    n = (name or "").strip().lower()
    return n if _SAFE_USERNAME.match(n) else "(not a valid username)"


def _public_user(u) -> dict:
    return {"id": u["id"], "username": u["username"], "name": u["name"], "email": u["email"], "role": u["role"], "status": u["status"]}


def end_current_session(request: Request, reason: str = "logout") -> None:
    sid = getattr(request.state, "session_id", None)
    if not sid:
        return
    c = platform_db.connect()
    try:
        if sessions.end(c, sid, reason):
            u = request.state.user
            audit.append(c, "logout", actor_id=u["id"], actor_role=u["role"])
    finally:
        c.close()


# ---- the gate (called by the middleware in main.py when platform mode is on) -------------------------------------------------------
def gate(request: Request):
    """Returns a response to send instead of the page (login redirect, 401, CSRF refusal), or None to let the request through.
    A request that passes carries request.state.user / .session_id for the routes below."""
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return None
    c = platform_db.connect()
    try:
        row = sessions.lookup(c, request.cookies.get(sessions.COOKIE))
    finally:
        c.close()
    if row is None:
        if path.startswith("/api/"):
            c = platform_db.connect()
            try:
                why = sessions.ended_reason(c, request.cookies.get(sessions.COOKIE))
            finally:
                c.close()
            resp = JSONResponse({"error": "LOGIN_REQUIRED", "message": "Login required.", "detail": "login required", "reason": why}, status_code=401)
        else:
            resp = RedirectResponse("/login", status_code=303)
        if request.cookies.get(sessions.COOKIE) or request.cookies.get(sessions.CSRF_COOKIE):
            clear_cookies(resp)        # a dead session must not leave its readable CSRF cookie behind: the pages would think they are still signed in
        return resp
    request.state.user = {"id": row["user_id"], "username": row["username"], "name": row["name"], "role": row["role"], "created_by": row["created_by"]}
    request.state.session_id, request.state.csrf = row["id"], row["csrf_token"]
    from app import admin_extra
    blocked = admin_extra.ip_gate(request)
    if blocked is not None:
        return blocked
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and path.startswith("/api/"):
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), row["csrf_token"]):
            return err(403, "CSRF", "Missing or wrong CSRF token. Reload the page.")
    return None


def require(permission: str):
    def dependency(request: Request) -> dict:
        u = getattr(request.state, "user", None)
        if u is None:
            raise AuthError(401, "LOGIN_REQUIRED", "Login required.")
        if not authz.has(u["role"], permission):
            raise AuthError(403, "FORBIDDEN", "You do not have permission for this.")
        return u
    return dependency


# ---- login / logout / me / heartbeat -----------------------------------------------------------------------------------------------
def takeover_allowed() -> bool:
    """Signing the other device out from the login page can be switched off on a deployment: APP_ALLOW_TAKEOVER=0."""
    return os.environ.get("APP_ALLOW_TAKEOVER", "1").strip().lower() not in ("0", "false", "no", "off")


class LoginBody(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=500)
    totp: str = Field(max_length=40)           # the 6-digit code, or a one-time backup code
    replace_existing: bool = False             # "sign the other device out and sign in here": still needs the right password AND code


@router.get("/api/auth/mode")
def auth_mode():
    return {"mode": mode.current()}


@router.post("/api/auth/login")
def login(body: LoginBody, request: Request, c=Depends(db)):
    now, ip = time.time(), client_ip(request)
    ks = ratelimit.keys(body.username, ip)
    who = _typed(body.username)
    if ratelimit.is_locked(c, ks, now):
        audit.append(c, "login.locked", detail={"username": who, "ip": ip})
        return err(423, "LOCKED", "Too many attempts. Try again in 15 minutes.")
    try:
        user, undo = users.authenticate_with_undo(c, body.username, body.password, body.totp, now)
    except users.InvalidCredentials as exc:
        ratelimit.record_failure(c, ks, now)
        audit.append(c, "login.failed", detail={"username": who, "reason": exc.reason, "ip": ip})
        return err(401, "INVALID_CREDENTIALS", "Invalid credentials")        # never says which part was wrong
    try:
        s = sessions.create(c, user["id"], request.headers.get("user-agent"), ip, now, replace=body.replace_existing and takeover_allowed())
    except sessions.AlreadyLoggedIn as exc:
        undo()                       # the credentials were right: give the code back so it can be used once the other device has signed out
        audit.append(c, "login.refused", actor_id=user["id"], actor_role=user["role"], detail={"device": exc.device, "ip": ip})
        return err(409, "ALREADY_LOGGED_IN", "This account is already logged in on another device. Log out there first.",
                   device=exc.device, since=sessions.iso_ist(exc.since), last_active=sessions.iso_ist(exc.last_active),
                   can_take_over=takeover_allowed())
    if s["taken_over"]:
        audit.append(c, "login.takeover", actor_id=user["id"], actor_role=user["role"],
                     detail={"signed_out_device": s["taken_over"]["device"], "new_device": sessions.device_label(request.headers.get("user-agent")), "ip": ip})
    ratelimit.clear(c, ks[0])
    audit.append(c, "login.success", actor_id=user["id"], actor_role=user["role"], detail={"ip": ip, "device": sessions.device_label(request.headers.get("user-agent"))})
    resp = JSONResponse({"ok": True, "user": _public_user(user)})
    _set_cookies(resp, request, s["token"], s["csrf"])
    return resp


@router.post("/api/auth/logout")
def logout(request: Request):
    end_current_session(request)
    resp = JSONResponse({"ok": True})
    clear_cookies(resp)
    return resp


@router.get("/api/auth/me")
def me(request: Request, u=Depends(require("session.self")), c=Depends(db)):
    s = c.execute("SELECT device_label, started_at, last_seen_at FROM sessions WHERE id=?", (request.state.session_id,)).fetchone()
    return {"user": {**{k: u[k] for k in ("id", "username", "name", "role")}, "owner": authz.is_owner(u)},
            "permissions": authz.permissions(u["role"]),
            "session": {"device": s["device_label"], "started_at": sessions.iso_ist(s["started_at"]), "last_active": sessions.iso_ist(s["last_seen_at"])},
            "csrf_token": request.state.csrf}


class PasswordBody(BaseModel):
    current_password: str = Field(max_length=500)
    new_password: str = Field(max_length=500)


@router.post("/api/auth/change-password")
def change_password(body: PasswordBody, request: Request, u=Depends(require("session.self")), c=Depends(db)):
    now, ip = time.time(), client_ip(request)
    ks = ratelimit.keys(u["username"], ip)               # a stolen session must not be able to guess the password at leisure
    if ratelimit.is_locked(c, ks, now):
        return err(423, "LOCKED", "Too many attempts. Try again in 15 minutes.")
    try:
        users.change_password(c, u["id"], body.current_password, body.new_password)
    except users.InvalidCredentials:
        ratelimit.record_failure(c, ks, now)
        audit.append(c, "password.change_failed", actor_id=u["id"], actor_role=u["role"], detail={"ip": ip})
        return err(401, "WRONG_PASSWORD", "The current password is not correct.")
    audit.append(c, "password.change", actor_id=u["id"], actor_role=u["role"], target_type="user", target_id=u["id"])
    return {"ok": True}


class Beat(BaseModel):
    active: bool = False                        # did the person touch the page since the last ping?


@router.post("/api/auth/heartbeat")
def heartbeat(body: Beat, request: Request, _=Depends(require("session.self")), c=Depends(db)):
    sessions.heartbeat(c, request.state.session_id, body.active)
    return {"ok": True}


# ---- one-time setup link -----------------------------------------------------------------------------------------------------------
class SetupBody(BaseModel):
    password: str = Field(max_length=500)
    totp: str = Field(max_length=40)


@router.get("/api/auth/setup/{token}")
def setup_info(token: str, c=Depends(db)):
    return users.begin_setup(c, token)


@router.post("/api/auth/setup/{token}")
def setup_complete(token: str, body: SetupBody, c=Depends(db)):
    user, codes = users.complete_setup(c, token, body.password, body.totp)
    audit.append(c, "user.setup_complete", actor_id=user["id"], actor_role=user["role"], target_type="user", target_id=user["id"])
    return {"ok": True, "username": user["username"], "backup_codes": codes}


# ---- admin: user management --------------------------------------------------------------------------------------------------------
class NewUser(BaseModel):
    name: str = Field(max_length=100)
    username: str = Field(max_length=40)
    email: str | None = Field(default=None, max_length=200)
    role: str = "user"


class StatusBody(BaseModel):
    status: str


def _link(request: Request, token: str) -> dict:
    path = f"/setup/{token}"
    return {"setup_path": path, "setup_url": str(request.base_url).rstrip("/") + path, "expires_in_hours": users.INVITE_TTL_SECONDS // 3600}


@router.post("/api/admin/users")
def admin_create_user(body: NewUser, request: Request, actor=Depends(require("users.create")), c=Depends(db)):
    if not authz.can_create_role(actor, body.role):
        raise AuthError(403, "FORBIDDEN", "Developers create admins; only the owner creates developers.")
    user, token = users.create_user(c, name=body.name, username=body.username, email=body.email, role=body.role, created_by=actor["id"])
    audit.append(c, "user.create", actor_id=actor["id"], actor_role=actor["role"], target_type="user", target_id=user["id"],
                 detail={"username": user["username"], "role": user["role"]})
    return {"user": _public_user(user), **_link(request, token)}


@router.get("/api/admin/users")
def admin_list_users(_=Depends(require("users.read")), c=Depends(db)):
    return {"users": users.list_users(c)}


@router.patch("/api/admin/users/{user_id}")
def admin_set_status(user_id: str, body: StatusBody, actor=Depends(require("users.manage")), c=Depends(db)):
    user = users.set_status(c, actor, user_id, body.status)
    audit.append(c, "user.disable" if user["status"] == "DISABLED" else "user.enable", actor_id=actor["id"], actor_role=actor["role"],
                 target_type="user", target_id=user_id)
    return {"user": _public_user(user)}


@router.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: str, actor=Depends(require("users.manage")), c=Depends(db)):
    users.delete_user(c, actor, user_id)
    audit.append(c, "user.delete", actor_id=actor["id"], actor_role=actor["role"], target_type="user", target_id=user_id)
    return {"ok": True}


@router.post("/api/admin/users/{user_id}/force-logout")
def admin_force_logout(user_id: str, actor=Depends(require("users.manage")), c=Depends(db)):
    n = users.force_logout(c, actor, user_id)
    audit.append(c, "user.force_logout", actor_id=actor["id"], actor_role=actor["role"], target_type="user", target_id=user_id, detail={"sessions_ended": n})
    return {"ok": True, "sessions_ended": n}


@router.post("/api/admin/users/{user_id}/reset-totp")
def admin_reset_totp(user_id: str, request: Request, actor=Depends(require("users.manage")), c=Depends(db)):
    user, token = users.reset_totp(c, actor, user_id)
    audit.append(c, "user.reset_totp", actor_id=actor["id"], actor_role=actor["role"], target_type="user", target_id=user_id)
    return {"user": _public_user(user), **_link(request, token)}


@router.get("/api/admin/audit")
def admin_audit(limit: int = 100, before_id: int | None = None, _=Depends(require("audit.read")), c=Depends(db)):
    limit = max(1, min(limit, 500))
    rows = c.execute("SELECT id, ts, actor_id, actor_role, action, target_type, target_id, detail_json FROM audit_log "
                     "WHERE (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?", (before_id, before_id, limit)).fetchall()
    return {"entries": [dict(r) for r in rows]}
