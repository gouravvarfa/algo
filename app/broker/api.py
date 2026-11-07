"""Broker connection endpoints: a user sets up and checks their own Angel One account; an admin may do the same for a user and sees only the status."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.auth_api import db, require
from app.broker import account, store
from app.db import audit
from app.ops import flags
from app.security import authz, passwords, users
from app.security.users import AuthError
from app.user_data import user_id_of

router = APIRouter()


class BrokerBody(BaseModel):
    client_code: str = Field(max_length=40)
    api_key: str = Field(max_length=100)
    pin: str = Field(max_length=20)
    totp_secret: str = Field(max_length=100)


async def _run(coro):
    try:
        return await coro
    except store.BrokerError as e:
        raise AuthError(e.status, e.code, e.message)


def _save(c, user_id: str, body: BrokerBody, actor_id: str, actor_role: str | None, actor_name: str | None) -> None:
    try:
        store.save(c, user_id, body.client_code, body.api_key, body.pin, body.totp_secret, actor_id)
    except store.BrokerError as e:
        raise AuthError(e.status, e.code, e.message)
    audit.append(c, "broker.save", actor_id=actor_id, actor_role=actor_role, target_type="user", target_id=user_id, detail={"actor": actor_name, "broker": "angelone"})


@router.get("/api/me/broker")
def my_broker(request: Request, c=Depends(db)):
    return store.public(store.row(c, user_id_of(request)))


@router.put("/api/me/broker")
def set_my_broker(body: BrokerBody, request: Request, c=Depends(db)):
    u = getattr(request.state, "user", None)
    uid = user_id_of(request)
    _save(c, uid, body, uid, u["role"] if u else None, u["username"] if u else "local")
    return store.public(store.row(c, uid))


@router.delete("/api/me/broker")
def delete_my_broker(request: Request, c=Depends(db)):
    u = getattr(request.state, "user", None)
    uid = user_id_of(request)
    store.delete(c, uid)
    audit.append(c, "broker.delete", actor_id=uid, actor_role=u["role"] if u else None, target_type="user", target_id=uid, detail={"actor": u["username"] if u else "local"})
    return store.public(None)


@router.post("/api/me/broker/test")
async def test_my_broker(request: Request, c=Depends(db)):
    u = getattr(request.state, "user", None)
    uid = user_id_of(request)
    out = await _run(account.check(c, uid))
    audit.append(c, "broker.test", actor_id=uid, actor_role=u["role"] if u else None, target_type="user", target_id=uid, detail={"result": out["status"]})
    return out


@router.get("/api/me/broker/summary")
async def my_broker_summary(request: Request, c=Depends(db)):
    return await _run(account.summary(c, user_id_of(request)))


# ---- admin: status for everybody, set up / remove for one user. Never a secret. ----------------------------------------------------------
@router.get("/api/admin/broker")
def admin_list(actor=Depends(require("users.read")), c=Depends(db)):
    rows = c.execute("SELECT b.*, u.username, u.name FROM user_broker b LEFT JOIN users u ON u.id = b.user_id ORDER BY b.updated_at DESC").fetchall()
    return {"accounts": [{**store.public(r), "user_id": r["user_id"], "username": r["username"], "name": r["name"]} for r in rows]}


def _managed(c, actor, user_id: str):
    t = users.get_user(c, user_id)
    if t is None or t["status"] == "DELETED":
        raise AuthError(404, "NOT_FOUND", "No such user.")
    if t["id"] != actor["id"] and not authz.can_manage(actor, t):
        raise AuthError(403, "FORBIDDEN", "You cannot change this account.")
    return t


@router.get("/api/admin/users/{user_id}/broker")
def admin_read(user_id: str, actor=Depends(require("users.read")), c=Depends(db)):
    return store.public(store.row(c, user_id))


@router.put("/api/admin/users/{user_id}/broker")
def admin_set(user_id: str, body: BrokerBody, actor=Depends(require("users.manage")), c=Depends(db)):
    _managed(c, actor, user_id)
    _save(c, user_id, body, actor["id"], actor["role"], actor["username"])
    return store.public(store.row(c, user_id))


@router.delete("/api/admin/users/{user_id}/broker")
def admin_delete(user_id: str, actor=Depends(require("users.manage")), c=Depends(db)):
    _managed(c, actor, user_id)
    store.delete(c, user_id)
    audit.append(c, "broker.delete", actor_id=actor["id"], actor_role=actor["role"], target_type="user", target_id=user_id, detail={"actor": actor["username"]})
    return store.public(None)


# ---- real mode: the person switches, with their password; an admin grants and limits; the master switch is the kill switch -------------------
class ModeBody(BaseModel):
    mode: str = Field(max_length=10)
    password: str = Field(default="", max_length=200)


_bad_pw: dict[str, list[float]] = {}
PW_TRIES, PW_WINDOW = 5, 600.0


@router.put("/api/me/broker/mode")
def set_my_mode(body: ModeBody, request: Request, c=Depends(db)):
    u = getattr(request.state, "user", None)
    uid = user_id_of(request)
    if body.mode == "real":
        if u is None:
            raise AuthError(403, "LOGIN_REQUIRED", "Real orders need a signed-in account with a password.")
        now = time.time()
        tries = [t for t in _bad_pw.get(uid, []) if now - t < PW_WINDOW]
        if len(tries) >= PW_TRIES:
            raise AuthError(429, "TOO_MANY", "Too many wrong passwords. Wait ten minutes.")
        full = users.get_user(c, uid)
        if not body.password or not passwords.verify_password(full["password_hash"], body.password):
            _bad_pw[uid] = tries + [now]
            raise AuthError(403, "BAD_PASSWORD", "Enter your account password to switch to real orders.")
        _bad_pw.pop(uid, None)
    try:
        store.set_mode(c, uid, body.mode, u["username"] if u else "local")
    except store.BrokerError as e:
        raise AuthError(e.status, e.code, e.message)
    audit.append(c, "broker.mode", actor_id=uid, actor_role=u["role"] if u else None, target_type="user", target_id=uid, detail={"mode": body.mode})
    return store.public(store.row(c, uid))


class GrantBody(BaseModel):
    allowed: bool
    max_lots: int = Field(default=1, ge=1, le=50)
    max_daily_loss: float | None = Field(default=None, gt=0)
    allowed_symbols: list[str] | None = None


@router.put("/api/admin/users/{user_id}/broker/real")
def admin_grant(user_id: str, body: GrantBody, actor=Depends(require("users.manage")), c=Depends(db)):
    _managed(c, actor, user_id)
    try:
        store.grant(c, user_id, body.allowed, body.max_lots, body.max_daily_loss, body.allowed_symbols, actor["username"])
    except store.BrokerError as e:
        raise AuthError(e.status, e.code, e.message)
    audit.append(c, "broker.grant", actor_id=actor["id"], actor_role=actor["role"], target_type="user", target_id=user_id,
                 detail={"actor": actor["username"], "allowed": body.allowed, "max_lots": body.max_lots, "max_daily_loss": body.max_daily_loss, "symbols": body.allowed_symbols})
    return store.public(store.row(c, user_id))


class MasterBody(BaseModel):
    enabled: bool


@router.get("/api/admin/broker/overview")
def admin_overview(actor=Depends(require("users.read")), c=Depends(db)):
    rows = c.execute("SELECT b.*, u.username, u.name FROM user_broker b LEFT JOIN users u ON u.id = b.user_id ORDER BY b.updated_at DESC").fetchall()
    accts = [{**store.public(r), "user_id": r["user_id"], "username": r["username"], "name": r["name"]} for r in rows]
    return {"master_on": bool(flags.get("real.enabled")), "can_turn_on": authz.is_owner(actor), "accounts": accts,
            "counts": {"connected": sum(a["status"] == "connected" for a in accts), "allowed": sum(a["real_allowed"] for a in accts),
                       "real_now": sum(a["effective_mode"] == "real" for a in accts), "total": len(accts)}}


@router.post("/api/admin/broker/master")
def admin_master(body: MasterBody, actor=Depends(require("users.manage")), c=Depends(db)):
    """OFF (the kill switch) is open to every admin and puts every account back to paper at once. ON is for the owner only."""
    if body.enabled and not authz.is_owner(actor):
        raise AuthError(403, "OWNER_ONLY", "Only the owner can switch real orders on for the platform.")
    flags.set_flag("real.enabled", body.enabled, updated_by=actor["username"])
    n = 0 if body.enabled else store.force_all_paper(c, actor["username"])
    audit.append(c, "broker.master", actor_id=actor["id"], actor_role=actor["role"], target_type="platform", target_id="real.enabled",
                 detail={"actor": actor["username"], "enabled": body.enabled, "accounts_back_to_paper": n})
    return {"master_on": body.enabled, "accounts_back_to_paper": n}
