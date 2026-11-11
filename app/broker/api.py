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
    orders._drop(user_id)                                         # new login details: the day's session of the old ones is not used again
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
    orders._drop(uid)
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
    orders._drop(user_id)
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
    lots: int | None = Field(default=None, ge=1, le=50)


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
    items = [{"instrument_id": i.id, "symbol": i.symbol, "trading_symbol": i.trading_symbol, "real_on": sw.get(i.id, False), "real_effective": sw.get(i.id, False) and mode == "real",
              "real_lots": store.instrument_lots(c, uid, i.id), "lot_size": _active(i)["lot_size"], "contract": _active(i)["trading_symbol"], "paper_lots": getattr(i, "lots", None) or 1}
             for i in appmain.engine.instruments.values() if i.owner == owner]
    return {"account_mode": mode, "real_allowed": bool(r and r["real_allowed"]), "max_lots": store.MAX_REAL_LOTS, "blockers": store.blockers(r), "instruments": items}


@router.put("/api/me/broker/instruments/{instrument_id}")
def set_my_instrument(instrument_id: str, body: InstrumentSwitch, request: Request, c=Depends(db)):
    inst = _owned_instrument(request, instrument_id)
    u = getattr(request.state, "user", None)
    uid = user_id_of(request)
    try:
        store.instrument_set(c, uid, instrument_id, inst.symbol, body.real, u["username"] if u else "local", body.lots)
    except store.BrokerError as e:
        raise AuthError(e.status, e.code, e.message)
    audit.append(c, "broker.instrument", actor_id=uid, actor_role=u["role"] if u else None, target_type="user", target_id=uid,
                 detail={"instrument": inst.symbol, "instrument_id": instrument_id, "real": body.real, "lots": body.lots})
    return my_instruments(request, c)


# ---- Top-Bottom real orders: what the executor holds and has done ---------------------------------------------------------------------------
def _active(inst) -> dict:
    """The contract the strategy trades NOW (after any expiry roll), from the engine's latest snapshot; the instrument record keeps the contract it was created with."""
    from app import main as appmain
    act = ((getattr(appmain.engine, "snapshots", None) or {}).get(inst.id) or {}).get("instrument") or {}
    lots = getattr(inst, "lots", None) or 1
    return {"trading_symbol": act.get("trading_symbol") or getattr(inst, "trading_symbol", None), "token": str(act.get("token") or getattr(inst, "token", "")), "exch_seg": act.get("exch_seg") or getattr(inst, "exch_seg", "NFO"),
            "lot_size": act.get("lot_size") or getattr(inst, "lot_size", None) or (getattr(inst, "quantity", 0) // lots) or 1}


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


@router.get("/api/me/broker/tb-real-trades")
def my_tb_real_trades_all(request: Request, c=Depends(db)):
    """The Real Trades page: every REAL Top-Bottom trade of this person (only what Angel One filled), all instruments together, kept apart from paper."""
    from app import main as appmain
    from app.broker import tb_real
    uid = user_id_of(request)
    rt = _executor()
    states = {x["instrument_id"]: x for x in (rt.view(c, uid) if rt is not None else [])}
    sw = {r["instrument_id"]: dict(r) for r in c.execute("SELECT instrument_id, symbol, real_on, changed_at FROM user_instrument_real WHERE user_id=?", (uid,))}
    ids = set(sw) | {r["instrument_id"] for r in c.execute("SELECT DISTINCT instrument_id FROM real_tb_orders WHERE user_id=?", (uid,))}
    trades, instruments = [], []
    for iid in sorted(ids):
        inst = appmain.engine.owned(iid, uid)
        per_qty = inst.pnl_units / inst.quantity if inst is not None and inst.quantity else 1.0
        st = states.get(iid) or {}
        sym = (inst.symbol if inst is not None else None) or (sw.get(iid) or {}).get("symbol") or st.get("symbol") or iid
        for t in tb_real.RealTB.real_trades(c, uid, iid, per_qty):
            trades.append({**t, "instrument_id": iid, "symbol": sym})
        instruments.append({"instrument_id": iid, "symbol": sym, "real_on": bool((sw.get(iid) or {}).get("real_on")), "position": st.get("position"),
                            "stop_target": st.get("stop_target"), "halted": st.get("halted")})
    trades.sort(key=lambda t: t.get("entry_at") or 0, reverse=True)
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    return {"trades": trades, "instruments": instruments, "realised": round(sum(t.get("pnl_amount") or 0 for t in closed), 2), "closed": len(closed),
            "wins": sum(1 for t in closed if (t.get("pnl_amount") or 0) > 0), "open": sum(1 for t in trades if t.get("status") == "OPEN")}


@router.get("/api/me/broker/tb/{instrument_id}/trades")
def my_tb_trades(instrument_id: str, request: Request, c=Depends(db)):
    """Real trades only (Angel One fills), plus what the executor holds now, for the Real view of the Top-Bottom page."""
    from app.broker import tb_real
    inst = _owned_instrument(request, instrument_id)
    per_qty = inst.pnl_units / inst.quantity if inst.quantity else 1.0       # MCX gold: lots x point value per point, not the quantity in grams
    uid = user_id_of(request)
    rt = _executor()
    st = next((x for x in (rt.view(c, uid) if rt is not None else []) if x["instrument_id"] == instrument_id), None)
    r = c.execute("SELECT real_on, changed_at FROM user_instrument_real WHERE user_id=? AND instrument_id=?", (uid, instrument_id)).fetchone()
    return {"trades": tb_real.RealTB.real_trades(c, uid, instrument_id, per_qty), "state": st, "real_on": bool(r and r["real_on"]), "since": r["changed_at"] if r else None}


_MARGIN: dict[tuple[str, str], tuple[float, dict]] = {}


@router.get("/api/me/broker/tb/{instrument_id}/margin")
async def my_tb_margin(instrument_id: str, request: Request, c=Depends(db)):
    """How much margin one real order of this instrument needs, from Angel One's own margin calculator, next to the cash available. Read-only, kept 2 min."""
    from app.broker import tb_real
    inst = _owned_instrument(request, instrument_id)
    uid = user_id_of(request)
    r = store.row(c, uid)
    if r is None or r["status"] != "connected":
        return {"connected": False}
    hit = _MARGIN.get((uid, instrument_id))
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    lots = store.instrument_lots(c, uid, instrument_id)
    ac = _active(inst)
    qty = lots * ac["lot_size"]
    try:
        b = await tb_real.angel_broker_for(c, uid)
        f = await b.funds()
    except Exception as exc:
        orders.drop_if_dead(uid, exc)                             # only an expired session is forgotten: a busy Angel One is asked again next time
        return {"connected": True, "error": f"Angel One not reachable: {str(exc)[:160] or type(exc).__name__}"}
    need, err, ltp = {"BUY": None, "SELL": None}, None, None
    for side in ("BUY", "SELL"):                                  # one side per call: a BUY+SELL basket would net the margin out
        try:
            m = await b.margin([{"exchange": ac["exch_seg"], "qty": qty, "price": 0, "productType": "CARRYFORWARD", "token": ac["token"], "tradeType": side, "orderType": "MARKET"}])
            need[side] = account._num(m.get("totalMarginRequired"))
        except Exception as exc:
            err = f"Angel One margin calculator: {str(exc)[:160] or type(exc).__name__}"
        await asyncio.sleep(0.6)                                  # Angel One rate-limits the margin API
    try:
        ltp = await b.ltp(ac["exch_seg"], ac["trading_symbol"], ac["token"])
    except Exception:
        ltp = None
    cash = account._num(f.get("availablecash"))
    worst = max((v for v in need.values() if v is not None), default=None)
    out = {"connected": True, "contract": ac["trading_symbol"], "lots": lots, "qty": qty, "ltp": ltp, "lot_size": qty // lots if lots else None, "need_buy": need["BUY"], "need_sell": need["SELL"], "available": cash,
           "enough": None if worst is None or cash is None else cash >= worst, "short_by": None if worst is None or cash is None or cash >= worst else round(worst - cash, 2), "at": time.time(),
           **({"error": err} if err else {})}
    if err:
        return out                                                # not cached: the next look asks again
    _MARGIN[(uid, instrument_id)] = (time.time(), out)
    return out


_POS: dict[str, tuple[float, dict]] = {}


@router.get("/api/me/broker/positions")
async def my_positions(request: Request, c=Depends(db)):
    """Today's positions in the connected Angel One account with P&L (Angel One's own numbers), each marked when this app manages it. Read-only, kept 10 s."""
    from app.broker import tb_real
    uid = user_id_of(request)
    r = store.row(c, uid)
    if r is None or r["status"] != "connected":
        return {"connected": False}
    hit = _POS.get(uid)
    if hit and time.time() - hit[0] < 10:
        return hit[1]
    try:
        b = await tb_real.angel_broker_for(c, uid)
        rows = await b.positions()
    except Exception as exc:
        orders.drop_if_dead(uid, exc)
        return {"connected": True, "error": f"Angel One not reachable ({type(exc).__name__})"}
    rt = _executor()
    managed = {x["position"]["contract"]: x for x in (rt.view(c, uid) if rt is not None else []) if x["position"]}
    pos = []
    for p in rows or []:
        q = account._position(p)
        m = managed.get(q["symbol"])
        pos.append({**q, "app": bool(m), "instrument_id": m["instrument_id"] if m else None, "stop": m["stop_target"] if m else None})
    pos.sort(key=lambda x: (x["net_qty"] in (0, None), x["symbol"] or ""))
    out = {"connected": True, "positions": pos, "open": sum(1 for x in pos if x["net_qty"]), "total_pnl": round(sum(x["pnl"] or 0 for x in pos), 2), "at": time.time()}
    _POS[uid] = (time.time(), out)
    return out


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
    except Exception as exc:
        orders.drop_if_dead(uid, exc)
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


# ---- one user's activity, for the admin's user page ---------------------------------------------------------------------------------------
@router.get("/api/admin/users/{user_id}/activity")
def admin_user_activity(user_id: str, limit: int = 200, actor=Depends(require("users.read")), c=Depends(db)):
    """Everything about one person's broker and real trading in one list, newest first: real orders (Top-Bottom and options), halts the executor
    holds now, and every settings change (connect, test, mode, switches, lots, admin grants, exits) from the audit log. Never a secret."""
    import datetime as _dt
    import json as _json
    limit = max(10, min(limit, 500))
    items: list[dict] = []
    for r in c.execute("SELECT created_at, updated_at, symbol, contract, role, side, qty, order_type, price, trigger_price, status, message, filled_avg FROM real_tb_orders "
                       "WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, limit)):
        px = f" @ {r['price']}" if r["price"] is not None else ""
        trg = f" trigger {r['trigger_price']}" if r["trigger_price"] is not None else ""
        fill = f" · filled {r['filled_avg']}" if r["filled_avg"] else ""
        items.append({"at": r["created_at"], "type": "order", "status": r["status"], "title": f"Top-Bottom {r['role']} {r['side']} {r['qty']} {r['contract']}",
                      "detail": f"{r['order_type']}{px}{trg} · {r['status']}{fill}" + (f" · {r['message']}" if r["message"] else "")})
    for r in c.execute("SELECT created_at, trading_symbol, side, qty, price, intent, status, message FROM broker_orders WHERE user_id=? AND status!='previewed' "
                       "ORDER BY created_at DESC LIMIT ?", (user_id, limit)):
        items.append({"at": r["created_at"], "type": "order", "status": r["status"], "title": f"Option {r['intent']} {r['side']} {r['qty']} {r['trading_symbol']}",
                      "detail": f"LIMIT @ {r['price']} · {r['status']}" + (f" · {r['message']}" if r["message"] else "")})
    for r in c.execute("SELECT state_json, updated_at FROM real_tb_state WHERE user_id=?", (user_id,)):
        st = _json.loads(r["state_json"])
        if st.get("halted"):
            items.append({"at": r["updated_at"], "type": "halt", "status": "halted", "title": f"HALTED: {st.get('symbol')}", "detail": st["halted"]})
    for r in c.execute("SELECT ts, action, actor_id, actor_role, detail_json FROM audit_log WHERE (target_id=? OR actor_id=?) "
                       "AND (action LIKE 'broker.%' OR action LIKE 'user.%' OR action LIKE 'auth.%' OR action LIKE 'credits.%') ORDER BY id DESC LIMIT ?", (user_id, user_id, limit)):
        try:
            at = _dt.datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        d = _json.loads(r["detail_json"] or "{}")
        d = {k: v for k, v in d.items() if k not in ("password", "pin", "totp", "api_key", "totp_secret")}
        by = "self" if r["actor_id"] == user_id else (r["actor_role"] or "system")
        items.append({"at": at, "type": "change", "status": None, "title": r["action"], "detail": (", ".join(f"{k}: {v}" for k, v in d.items()) + f" · by {by}").strip(" ·")})
    items.sort(key=lambda x: x["at"], reverse=True)
    return {"items": items[:limit]}
