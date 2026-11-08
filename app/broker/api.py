"""Broker connection endpoints: a user sets up and checks their own Angel One account; an admin may do the same for a user and sees only the status."""
from __future__ import annotations

import time

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth_api import db, require
from app.broker import account, orders, store
from app.db import audit
from app.ops import flags
from app.options import master
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


@router.post("/api/me/broker/preflight")
async def my_preflight(request: Request, c=Depends(db)):
    """Read-only dry check of the whole chain (login, funds, positions, orders, price, margin). Never sends an order."""
    uid = user_id_of(request)
    u = getattr(request.state, "user", None)
    out = await _run(account.preflight(c, uid))
    audit.append(c, "broker.preflight", actor_id=uid, actor_role=u["role"] if u else None, target_type="user", target_id=uid, detail={"ok": out["ok"], "failed": [x["name"] for x in out["steps"] if not x["ok"]]})
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


# ---- Top-Bottom: the person's real-order switch for each instrument ------------------------------------------------------------------------
class InstrumentSwitch(BaseModel):
    real: bool


def _owned_instrument(request: Request, instrument_id: str):
    from app import main as appmain
    u = getattr(request.state, "user", None)
    inst = appmain.engine.owned(instrument_id, u["id"] if u else None)
    if inst is None:
        raise AuthError(404, "NOT_FOUND", "Instrument not found.")
    return inst


@router.get("/api/me/broker/instruments")
def my_instruments(request: Request, c=Depends(db)):
    from app import main as appmain
    uid = user_id_of(request)
    u = getattr(request.state, "user", None)
    owner = u["id"] if u else None
    sw = store.instrument_switches(c, uid)
    r = store.row(c, uid)
    mode = store.effective_mode(r)
    items = [{"instrument_id": i.id, "symbol": i.symbol, "trading_symbol": i.trading_symbol, "real_on": sw.get(i.id, False), "real_effective": sw.get(i.id, False) and mode == "real"}
             for i in appmain.engine.instruments.values() if i.owner == owner]
    return {"account_mode": mode, "real_allowed": bool(r and r["real_allowed"]), "blockers": store.blockers(r), "instruments": items}


@router.put("/api/me/broker/instruments/{instrument_id}")
def set_my_instrument(instrument_id: str, body: InstrumentSwitch, request: Request, c=Depends(db)):
    inst = _owned_instrument(request, instrument_id)
    u = getattr(request.state, "user", None)
    uid = user_id_of(request)
    try:
        store.instrument_set(c, uid, instrument_id, inst.symbol, body.real, u["username"] if u else "local")
    except store.BrokerError as e:
        raise AuthError(e.status, e.code, e.message)
    audit.append(c, "broker.instrument", actor_id=uid, actor_role=u["role"] if u else None, target_type="user", target_id=uid,
                 detail={"instrument": inst.symbol, "instrument_id": instrument_id, "real": body.real})
    return my_instruments(request, c)


# ---- Top-Bottom real orders: what the executor holds and has done ---------------------------------------------------------------------------
def _executor():
    from app import main as appmain
    return getattr(appmain.engine, "real", None) if appmain.engine is not None else None


@router.get("/api/me/broker/tb")
def my_tb(request: Request, c=Depends(db)):
    rt = _executor()
    return {"instruments": rt.view(c, user_id_of(request)) if rt is not None else []}


@router.post("/api/me/broker/tb/{instrument_id}/clear-halt")
def my_tb_clear_halt(instrument_id: str, request: Request, c=Depends(db)):
    """After looking at Angel One, the person lets the executor carry on. Nothing is sent by this call."""
    rt = _executor()
    u = getattr(request.state, "user", None)
    uid = user_id_of(request)
    if rt is None or not rt.clear_halt(c, uid, instrument_id, u["username"] if u else "local"):
        raise AuthError(404, "NOT_HALTED", "This instrument is not halted.")
    audit.append(c, "broker.tb.clear_halt", actor_id=uid, actor_role=u["role"] if u else None, target_type="user", target_id=uid, detail={"instrument_id": instrument_id})
    return {"ok": True}


@router.post("/api/me/broker/tb/{instrument_id}/exit")
def my_tb_exit(instrument_id: str, request: Request, c=Depends(db)):
    """Exit now: the real switch for this instrument goes OFF (no new entry) and the executor closes the open real position with a LIMIT order."""
    inst = _owned_instrument(request, instrument_id)
    rt = _executor()
    u = getattr(request.state, "user", None)
    uid = user_id_of(request)
    actor = u["username"] if u else "local"
    try:
        store.instrument_set(c, uid, instrument_id, inst.symbol, False, actor)
    except store.BrokerError:
        pass
    out = rt.exit_now(c, uid, instrument_id, actor) if rt is not None else "nothing"
    audit.append(c, "broker.tb.exit_now", actor_id=uid, actor_role=u["role"] if u else None, target_type="user", target_id=uid, detail={"instrument_id": instrument_id, "result": out})
    msg = {"exiting": "Exiting: the stop is cancelled and a LIMIT exit order goes to Angel One now (or when the market opens).", "entry_cancelled": "The waiting entry is cancelled. Real is off for this instrument.",
           "halted": "This instrument is halted: check Angel One, clear the halt, then press Exit again (or square off at Angel One).", "nothing": "No real position to exit. Real is off for this instrument."}[out]
    return {"result": out, "message": msg}


_FUNDS: dict[str, tuple[float, dict]] = {}


@router.get("/api/me/broker/funds")
async def my_funds(request: Request, c=Depends(db)):
    """Margin of the connected Angel One account for the header. Read-only, kept for 60 s, and it reuses the app's session (no fresh login per page)."""
    from app.broker import tb_real
    uid = user_id_of(request)
    r = store.row(c, uid)
    if r is None or r["status"] != "connected":
        return {"connected": False}
    hit = _FUNDS.get(uid)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        f = await (await tb_real.angel_broker_for(c, uid)).funds()
    except Exception:
        orders._drop(uid)
        return {"connected": True, "error": "Angel One not reachable right now"}
    out = {"connected": True, **account._funds(f), "mode": store.effective_mode(r), "at": time.time()}
    _FUNDS[uid] = (time.time(), out)
    return out


@router.get("/api/admin/broker/tb")
def admin_tb(limit: int = 100, actor=Depends(require("users.read")), c=Depends(db)):
    rt = _executor()
    names = {r["id"]: (r["username"], r["name"]) for r in c.execute("SELECT id, username, name FROM users")}
    state = [{**x, "username": (names.get(x["user_id"]) or (None, None))[0], "name": (names.get(x["user_id"]) or (None, None))[1]} for x in (rt.view(c, None) if rt is not None else [])]
    orders_ = [dict(r) | {"username": (names.get(r["user_id"]) or (None, None))[0], "name": (names.get(r["user_id"]) or (None, None))[1]}
               for r in c.execute("SELECT * FROM real_tb_orders ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 300)),))]
    return {"instruments": state, "orders": orders_}


# ---- real orders: preview -> send (once) -> cancel; the checks live in app/broker/orders.py --------------------------------------------------
async def _orders(coro):
    """Run an order step; a refusal comes back as JSON with the failed check list, so the screen can show exactly why."""
    try:
        return await coro
    except orders.OrderError as e:
        return JSONResponse({"error": e.code, "message": e.message, "checks": e.checks}, status_code=e.status)
    except store.BrokerError as e:
        return JSONResponse({"error": e.code, "message": e.message, "checks": None}, status_code=e.status)


@router.get("/api/me/broker/contracts")
async def my_contracts(symbol: str = "NIFTY", request: Request = None, c=Depends(db)):
    sym = symbol.upper()
    if sym not in store.SYMBOLS:
        raise AuthError(400, "BAD_SYMBOL", f"symbol must be one of {list(store.SYMBOLS)}")
    info = await asyncio.to_thread(master.load_underlying, sym)
    exps = sorted({k.expiry.isoformat() for k in info.contracts})
    return {"symbol": sym, "expiries": exps[:6], "lot_size": info.contracts[0].lot_size if info.contracts else None,
            "strikes": {e: sorted({k.strike for k in info.contracts if k.expiry.isoformat() == e}) for e in exps[:6]}}


@router.post("/api/me/broker/orders/preview")
async def order_preview(body: dict, request: Request, c=Depends(db)):
    return await _orders(orders.preview(c, user_id_of(request), body))


@router.post("/api/me/broker/orders/{order_id}/send")
async def order_send(order_id: str, request: Request, c=Depends(db)):
    return await _orders(orders.send(c, user_id_of(request), order_id, getattr(request.state, "user", None)))


@router.post("/api/me/broker/orders/{order_id}/cancel")
async def order_cancel(order_id: str, request: Request, c=Depends(db)):
    return await _orders(orders.cancel(c, user_id_of(request), order_id, getattr(request.state, "user", None)))


@router.get("/api/me/broker/orders")
def my_orders(request: Request, c=Depends(db)):
    return {"orders": orders.listing(c, user_id_of(request))}


@router.get("/api/admin/broker/orders")
def admin_orders(limit: int = 100, actor=Depends(require("users.read")), c=Depends(db)):
    out = orders.listing(c, None, limit)
    names = {r["id"]: (r["username"], r["name"]) for r in c.execute("SELECT id, username, name FROM users")}
    uid = {r["id"]: r["user_id"] for r in c.execute("SELECT id, user_id FROM broker_orders")}
    return {"orders": [{**o, "user_id": uid.get(o["id"]), "username": (names.get(uid.get(o["id"])) or (None, None))[0], "name": (names.get(uid.get(o["id"])) or (None, None))[1]} for o in out]}
