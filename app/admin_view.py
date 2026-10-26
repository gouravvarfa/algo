"""Admin: see how every user is doing, and open a user's dashboard exactly as they see it ("view as"), READ-ONLY.

It is a mirror, not impersonation: the admin never receives the user's session, password, authenticator secret or broker keys, so the
one-device-per-account rule and the audit trail stay honest. The same data the user's own pages read is returned here for the chosen user;
nothing in this file can change a user's data.
Opening a user's dashboard is written to the audit log (once per admin and user per 10 minutes) and the user can see it under
"Account activity" (GET /api/me/activity): the usual practice for support tools.
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from app import user_data
from app.auth_api import db, require
from app.db import audit
from app.ops import context
from app.paper_trading.engine import ALL
from app.security import authz, users
from app.security.users import AuthError

router = APIRouter()
_VIEW_AUDIT_EVERY = 600.0
_last_view: dict[tuple[str, str], float] = {}


def _engine():
    e = context.engine
    if e is None:
        raise HTTPException(503, "engine not running")
    return e


def _target(c, actor, user_id: str):
    t = c.execute("SELECT * FROM users WHERE id=? AND status != 'DELETED'", (user_id,)).fetchone()
    if t is None:
        raise AuthError(404, "NOT_FOUND", "No such user.")
    if not authz.can_view(actor, t):
        raise AuthError(403, "FORBIDDEN", "You cannot view this user.")
    return t


def _note_view(c, actor, target) -> None:
    key, now = (actor["id"], target["id"]), time.time()
    if now - _last_view.get(key, 0.0) >= _VIEW_AUDIT_EVERY:
        _last_view[key] = now
        audit.append(c, "admin.view_as", actor_id=actor["id"], actor_role=actor["role"], target_type="user", target_id=target["id"],
                     detail={"actor": actor["username"], "viewed": target["username"]})


def _summary(user_id: str) -> dict:
    e = context.engine
    out = {"instruments": 0, "open_positions": 0, "state": "no strategy", "unrealized": 0.0, "live_realized": 0.0, "live_trades": 0, "fault": None, "paused": False, "strategy_on": True, "strategy_locked": False}
    if e is None:
        return out
    from app.ops import flags
    out["paused"] = flags.engine_paused(user_id)
    out["strategy_on"] = flags.get("strategy.enabled", user_id=user_id) is not False
    out["strategy_locked"] = bool(flags.get("strategy.locked", user_id=user_id))
    for inst in e.instruments.values():
        if inst.owner != user_id:
            continue
        out["instruments"] += 1
        snap = e.snapshots.get(inst.id)
        if inst.id in e.faults:
            out["fault"] = e.faults[inst.id]["message"]
        if not snap:
            continue
        st = snap.get("stats") or {}
        out["unrealized"] += st.get("unrealized") or 0
        out["live_realized"] += st.get("live_realized") or 0
        out["live_trades"] += st.get("live_trades") or 0
        if snap.get("position"):
            out["open_positions"] += 1
            out["state"] = snap["position"]["direction"]
        elif out["state"] == "no strategy":
            out["state"] = "waiting for a System Point" if snap.get("waiting") else "FLAT"
    out["unrealized"], out["live_realized"] = round(out["unrealized"], 2), round(out["live_realized"], 2)
    return out


@router.get("/api/admin/live-users")
def live_users(actor=Depends(require("users.view_as")), c=Depends(db)):
    """The Users screen: every user with online status and how their paper strategy is doing right now."""
    rows = []
    for u in users.list_users(c):
        t = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        rows.append({**u, "can_view": authz.can_view(actor, t), **_summary(u["id"])})
    return {"users": rows}


@router.get("/api/admin/users/{user_id}/overview")
def view_overview(user_id: str, actor=Depends(require("users.view_as")), c=Depends(db)):
    t = _target(c, actor, user_id)
    _note_view(c, actor, t)
    return {"viewing": {"id": t["id"], "username": t["username"], "name": t["name"], "role": t["role"]}, "summary": _summary(user_id),
            **_engine().overview(owner=user_id), "timeframes": [], "angelone_configured": True}


@router.get("/api/admin/users/{user_id}/instruments/{instrument_id}")
def view_instrument(user_id: str, instrument_id: str, actor=Depends(require("users.view_as")), c=Depends(db)):
    t = _target(c, actor, user_id)
    _note_view(c, actor, t)
    e = _engine()
    if e.owned(instrument_id, user_id) is None:
        raise HTTPException(404, "Instrument not found")
    snap = e.snapshots.get(instrument_id)
    if snap is None:
        raise HTTPException(503, e.errors.get(instrument_id, "Not evaluated yet"))
    return snap


@router.get("/api/admin/users/{user_id}/journal")
def view_journal(user_id: str, limit: int = 300, actor=Depends(require("users.view_as")), c=Depends(db)):
    t = _target(c, actor, user_id)
    _note_view(c, actor, t)
    return _engine().journal(max(1, min(limit, 5000)), owner=user_id)


@router.get("/api/admin/users/{user_id}/dry-run")
def view_dry_run(user_id: str, limit: int = 300, actor=Depends(require("users.view_as")), c=Depends(db)):
    from app.paper_trading import dryrun
    t = _target(c, actor, user_id)
    _note_view(c, actor, t)
    return dryrun.read(user_id, max(1, min(limit, 5000)))


@router.get("/api/admin/users/{user_id}/sim-positions")
def view_sim_positions(user_id: str, symbol: str = "NIFTY", actor=Depends(require("users.view_as")), c=Depends(db)):
    t = _target(c, actor, user_id)
    _note_view(c, actor, t)
    return user_data.read_positions(c, user_id, user_data._symbol(symbol))


@router.get("/api/admin/users/{user_id}/settings")
def view_settings(user_id: str, actor=Depends(require("users.view_as")), c=Depends(db)):
    t = _target(c, actor, user_id)
    return {"settings": user_data.read_settings(c, user_id)}


@router.get("/api/me/activity")
def my_activity(request: Request, limit: int = 50, c=Depends(db)):
    """Transparency: who opened MY dashboard, and when."""
    u = getattr(request.state, "user", None)
    if u is None:
        return {"views": []}
    rows = c.execute("SELECT ts, action, detail_json FROM audit_log WHERE action IN ('admin.view_as','admin.edit_profile','admin.edit_positions','admin.remove_instrument','admin.assign_strategy','admin.edit_setting','user.limits','strategy.off','strategy.on') AND target_id=? ORDER BY id DESC LIMIT ?",
                     (u["id"], max(1, min(limit, 200)))).fetchall()
    return {"views": [{"at": r["ts"], "by": (json.loads(r["detail_json"]) or {}).get("actor"), "what": r["action"]} for r in rows]}
