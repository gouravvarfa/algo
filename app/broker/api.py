"""Broker connection endpoints: a user sets up and checks their own Angel One account; an admin may do the same for a user and sees only the status."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.auth_api import db, require
from app.broker import account, store
from app.db import audit
from app.security import authz, users
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
