"""More for the admin console: per-user limits (enforced on the paper simulator), internal notes, CSV exports, trends, bulk actions,
an announcement banner and an optional IP allow-list for the admin and developer APIs.

Everything here is paper-only housekeeping; nothing sends an order anywhere. Limits are checked on the server when positions are saved,
so a browser edit cannot get around them.
"""
from __future__ import annotations

import csv
import io
import ipaddress
import json
import os
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.auth_api import client_ip, db, require
from app.db import audit
from app.security import authz, users
from app.security.users import AuthError

router = APIRouter()
SYMBOLS = ("NIFTY", "BANKNIFTY")


# ---- optional IP allow-list for /api/admin and /api/dev --------------------------------------------------------------------------------
def allowed_networks() -> list:
    out = []
    for part in os.environ.get("ADMIN_ALLOWED_IPS", "").split(","):
        part = part.strip()
        if part:
            try:
                out.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                continue
    return out


def ip_allowed(ip: str) -> bool:
    """True when no allow-list is configured (the default) or the address is inside one of the listed networks."""
    nets = allowed_networks()
    if not nets:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in nets)


def admin_path(path: str) -> bool:
    return path.startswith(("/api/admin", "/api/dev")) or path == "/metrics"


def ip_gate(request: Request):
    """Called from the auth gate for admin / developer APIs only. Returns a 403 response or None."""
    if admin_path(request.url.path) and not ip_allowed(client_ip(request)):
        from app.auth_api import err
        return err(403, "IP_NOT_ALLOWED", "The admin and developer areas can only be opened from an allowed network.")
    return None


# ---- limits ----------------------------------------------------------------------------------------------------------------------------
def get_limits(c, user_id: str) -> dict:
    r = c.execute("SELECT max_lots, allowed_symbols, updated_at FROM user_limits WHERE user_id=?", (user_id,)).fetchone()
    return {"max_lots": r["max_lots"] if r else None, "allowed_symbols": json.loads(r["allowed_symbols"]) if r and r["allowed_symbols"] else None,
            "updated_at": r["updated_at"] if r else None}


def check_positions(c, user_id: str, symbol: str, positions: list[dict]) -> None:
    """Raise when saving these positions would break the account's limits. Called by the simulator's save route."""
    lim = get_limits(c, user_id)
    if lim["allowed_symbols"] is not None and symbol not in lim["allowed_symbols"] and positions:
        raise AuthError(403, "LIMIT_SYMBOL", f"Your account is not allowed to hold {symbol} positions.")
    if lim["max_lots"] is not None:
        total = sum(abs(int(p.get("lots") or 0)) for p in positions if not p.get("closed"))
        others = 0
        for r in c.execute("SELECT symbol, data_json FROM user_sim_positions WHERE user_id=? AND symbol!=?", (user_id, symbol)):
            others += sum(abs(int(p.get("lots") or 0)) for p in json.loads(r["data_json"]) if not p.get("closed"))
        if total + others > lim["max_lots"]:
            raise AuthError(403, "LIMIT_LOTS", f"Your account is limited to {lim['max_lots']} lots in total; this would make {total + others}.")


class LimitsBody(BaseModel):
    max_lots: int | None = Field(default=None, ge=0, le=10_000)
    allowed_symbols: list[str] | None = None


def _target(c, actor, user_id: str):
    t = users.get_user(c, user_id)
    if t is None or t["status"] == "DELETED":
        raise AuthError(404, "NOT_FOUND", "No such user.")
    return t


@router.get("/api/admin/users/{user_id}/limits")
def read_limits(user_id: str, actor=Depends(require("users.read")), c=Depends(db)):
    _target(c, actor, user_id)
    return get_limits(c, user_id)


@router.put("/api/admin/users/{user_id}/limits")
def set_limits(user_id: str, body: LimitsBody, actor=Depends(require("users.manage")), c=Depends(db)):
    t = _target(c, actor, user_id)
    if not authz.can_manage(actor, t):
        raise AuthError(403, "FORBIDDEN", "You cannot change this account.")
    syms = None
    if body.allowed_symbols is not None:
        syms = [s.upper() for s in body.allowed_symbols]
        if any(s not in SYMBOLS for s in syms):
            raise AuthError(400, "BAD_SYMBOL", f"symbols must be among {list(SYMBOLS)}")
    c.execute("INSERT INTO user_limits (user_id, max_lots, allowed_symbols, updated_by, updated_at) VALUES (?,?,?,?,?) "
              "ON CONFLICT(user_id) DO UPDATE SET max_lots=excluded.max_lots, allowed_symbols=excluded.allowed_symbols, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
              (user_id, body.max_lots, json.dumps(syms) if syms is not None else None, actor["id"], time.time()))
    c.commit()
    audit.append(c, "user.limits", actor_id=actor["id"], actor_role=actor["role"], target_type="user", target_id=user_id,
                 detail={"actor": actor["username"], "max_lots": body.max_lots, "allowed_symbols": syms})
    return get_limits(c, user_id)


@router.get("/api/me/limits")
def my_limits(request: Request, c=Depends(db)):
    u = getattr(request.state, "user", None)
    return get_limits(c, u["id"]) if u else {"max_lots": None, "allowed_symbols": None, "updated_at": None}


# ---- notes -----------------------------------------------------------------------------------------------------------------------------
class NoteBody(BaseModel):
    note: str = Field(min_length=1, max_length=1000)


@router.get("/api/admin/users/{user_id}/notes")
def read_notes(user_id: str, actor=Depends(require("users.read")), c=Depends(db)):
    _target(c, actor, user_id)
    rows = c.execute("SELECT id, author_name, note, created_at FROM user_notes WHERE user_id=? ORDER BY id DESC LIMIT 100", (user_id,)).fetchall()
    return {"notes": [dict(r) for r in rows]}


@router.post("/api/admin/users/{user_id}/notes")
def add_note(user_id: str, body: NoteBody, actor=Depends(require("users.manage")), c=Depends(db)):
    _target(c, actor, user_id)
    c.execute("INSERT INTO user_notes (user_id, author_id, author_name, note, created_at) VALUES (?,?,?,?,?)", (user_id, actor["id"], actor["name"], body.note.strip(), time.time()))
    c.commit()
    audit.append(c, "user.note", actor_id=actor["id"], actor_role=actor["role"], target_type="user", target_id=user_id)
    return read_notes(user_id, actor, c)


# ---- CSV exports (formula-looking cells are neutralised so a spreadsheet never runs them) ----------------------------------------------
def _cell(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _csv(name: str, header: list[str], rows) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([_cell(x) for x in r])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/api/admin/users.csv")
def users_csv(actor=Depends(require("users.read")), c=Depends(db)):
    rows = users.list_users(c)
    audit.append(c, "export.users", actor_id=actor["id"], actor_role=actor["role"], detail={"rows": len(rows)})
    return _csv("users.csv", ["username", "name", "role", "status", "online"],
                [(r["username"], r["name"], r["role"], r["status"], r.get("online")) for r in rows])


@router.get("/api/admin/audit.csv")
def audit_csv(q: str = "", limit: int = 5000, actor=Depends(require("audit.read")), c=Depends(db)):
    limit = max(1, min(limit, 20000))
    like = f"%{q.strip().lower()}%"
    rows = c.execute("SELECT id, ts, actor_role, action, target_type, target_id, detail_json FROM audit_log "
                     "WHERE ?='%%' OR lower(action || ' ' || coalesce(target_type,'') || ' ' || coalesce(detail_json,'')) LIKE ? ORDER BY id DESC LIMIT ?", (like, like, limit)).fetchall()
    audit.append(c, "export.audit", actor_id=actor["id"], actor_role=actor["role"], detail={"rows": len(rows), "filter": q.strip()[:60]})
    return _csv("audit.csv", ["id", "time_utc", "actor_role", "action", "target_type", "target_id", "detail"], [tuple(r) for r in rows])


# ---- trends ----------------------------------------------------------------------------------------------------------------------------
@router.get("/api/admin/trends")
def trends(days: int = 30, _=Depends(require("audit.read")), c=Depends(db)):
    days = max(7, min(days, 90))
    start = (datetime.now(timezone.utc) - timedelta(days=days - 1)).date()
    labels = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    idx = {d: i for i, d in enumerate(labels)}
    logins, failed, joined = [0] * days, [0] * days, [0] * days
    people: list[set] = [set() for _ in range(days)]
    for r in c.execute("SELECT substr(ts,1,10) d, action, actor_id FROM audit_log WHERE ts >= ? AND action IN ('login.success','login.failed','login.locked','user.setup_complete')", (labels[0],)):
        i = idx.get(r["d"])
        if i is None:
            continue
        if r["action"] == "login.success":
            logins[i] += 1
            people[i].add(r["actor_id"])
        elif r["action"] in ("login.failed", "login.locked"):
            failed[i] += 1
        else:
            joined[i] += 1
    return {"days": labels, "logins": logins, "failed": failed, "joined": joined, "active_users": [len(p) for p in people]}


# ---- bulk actions ----------------------------------------------------------------------------------------------------------------------
class BulkBody(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)
    action: str


@router.post("/api/admin/users/bulk")
def bulk(body: BulkBody, actor=Depends(require("users.manage")), c=Depends(db)):
    if body.action not in ("disable", "enable", "logout"):
        raise AuthError(400, "BAD_ACTION", "action must be disable, enable or logout")
    done, failed = [], []
    for uid in dict.fromkeys(body.ids):
        try:
            if body.action == "logout":
                users.force_logout(c, actor, uid)
            else:
                users.set_status(c, actor, uid, "DISABLED" if body.action == "disable" else "ACTIVE")
            done.append(uid)
        except AuthError as e:
            failed.append({"id": uid, "reason": e.message if hasattr(e, "message") else str(e)})
    audit.append(c, f"user.bulk_{body.action}", actor_id=actor["id"], actor_role=actor["role"], detail={"done": len(done), "failed": len(failed)})
    return {"done": done, "failed": failed}


# ---- announcement banner ---------------------------------------------------------------------------------------------------------------
class AnnouncementBody(BaseModel):
    message: str = Field(min_length=1, max_length=300)
    level: str = "info"


@router.get("/api/announcement")
def announcement(c=Depends(db)):
    r = c.execute("SELECT message, level, updated_at FROM announcement WHERE id=1").fetchone()
    return {"announcement": dict(r) if r else None}


@router.put("/api/admin/announcement")
def set_announcement(body: AnnouncementBody, actor=Depends(require("users.manage")), c=Depends(db)):
    level = body.level if body.level in ("info", "warn", "bad") else "info"
    c.execute("INSERT INTO announcement (id, message, level, set_by, updated_at) VALUES (1,?,?,?,?) "
              "ON CONFLICT(id) DO UPDATE SET message=excluded.message, level=excluded.level, set_by=excluded.set_by, updated_at=excluded.updated_at",
              (body.message.strip(), level, actor["id"], time.time()))
    c.commit()
    audit.append(c, "announcement.set", actor_id=actor["id"], actor_role=actor["role"], detail={"level": level})
    return announcement(c)


@router.delete("/api/admin/announcement")
def clear_announcement(actor=Depends(require("users.manage")), c=Depends(db)):
    c.execute("DELETE FROM announcement WHERE id=1")
    c.commit()
    audit.append(c, "announcement.clear", actor_id=actor["id"], actor_role=actor["role"])
    return {"announcement": None}


@router.get("/api/admin/security-policy")
def security_policy(_=Depends(require("users.read"))):
    from app.security import sessions
    nets = [str(n) for n in allowed_networks()]
    return {"ip_allow_list": nets, "ip_allow_list_on": bool(nets), "idle_timeout_min": sessions.IDLE_TIMEOUT // 60,
            "absolute_timeout_h": sessions.ABSOLUTE_LIFETIME // 3600, "lockout": "5 failed attempts lock a username and an address for 15 minutes",
            "mfa": "password + authenticator code on every sign-in", "one_device": True}


# ---- admins can change what belongs to an account (never its password or authenticator secret: those are reset through a new setup link) ----
# Every change below is written to the audit log and shown to the person under "Account activity" (GET /api/me/activity).
def _managed(c, actor, user_id: str):
    t = _target(c, actor, user_id)
    if not authz.can_manage(actor, t):
        raise AuthError(403, "FORBIDDEN", "You cannot change this account.")
    return t


class ProfileBody(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    username: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=200)


@router.patch("/api/admin/users/{user_id}/profile")
def edit_profile(user_id: str, body: ProfileBody, actor=Depends(require("users.manage")), c=Depends(db)):
    import sqlite3
    t = _managed(c, actor, user_id)
    name = (body.name if body.name is not None else t["name"]).strip()
    username = (body.username if body.username is not None else t["username"]).strip().lower()
    email = (body.email if body.email is not None else t["email"])
    email = (email or "").strip() or None
    if not name:
        raise AuthError(400, "INVALID_NAME", "Name is required.")
    if not users._USERNAME_RE.match(username):
        raise AuthError(400, "INVALID_USERNAME", "Username must be 3-32 characters: lowercase letters, digits, dot, dash, underscore.")
    try:
        c.execute("UPDATE users SET name=?, username=?, email=? WHERE id=?", (name, username, email, user_id))
        c.commit()
    except sqlite3.IntegrityError:
        raise AuthError(409, "USERNAME_TAKEN", "That username is already taken.") from None
    audit.append(c, "admin.edit_profile", actor_id=actor["id"], actor_role=actor["role"], target_type="user", target_id=user_id,
                 detail={"actor": actor["username"], "from": {"name": t["name"], "username": t["username"]}, "to": {"name": name, "username": username}})
    return {"ok": True, "name": name, "username": username, "email": email}


class PositionsEdit(BaseModel):
    positions: list[dict] = Field(max_length=300)


@router.put("/api/admin/users/{user_id}/sim-positions")
def edit_positions(user_id: str, body: PositionsEdit, symbol: str = "NIFTY", actor=Depends(require("users.manage")), c=Depends(db)):
    _managed(c, actor, user_id)
    symbol = symbol.upper()
    if symbol not in SYMBOLS:
        raise AuthError(400, "BAD_SYMBOL", f"symbol must be one of {list(SYMBOLS)}")
    c.execute("INSERT INTO user_sim_positions (user_id, symbol, data_json, updated_at) VALUES (?,?,?,?) "
              "ON CONFLICT(user_id, symbol) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at",
              (user_id, symbol, json.dumps(body.positions, separators=(",", ":")), time.time()))
    c.commit()
    audit.append(c, "admin.edit_positions", actor_id=actor["id"], actor_role=actor["role"], target_type="user", target_id=user_id,
                 detail={"actor": actor["username"], "symbol": symbol, "count": len(body.positions)})
    return {"ok": True, "count": len(body.positions)}


@router.delete("/api/admin/users/{user_id}/instruments/{instrument_id}")
def remove_instrument(user_id: str, instrument_id: str, actor=Depends(require("users.manage")), c=Depends(db)):
    from fastapi import HTTPException
    from app.ops import context
    _managed(c, actor, user_id)
    if context.engine is None:
        raise HTTPException(503, "engine not running")
    if not context.engine.remove(instrument_id, owner=user_id):
        raise AuthError(404, "NOT_FOUND", "That strategy does not belong to this user.")
    audit.append(c, "admin.remove_instrument", actor_id=actor["id"], actor_role=actor["role"], target_type="user", target_id=user_id,
                 detail={"actor": actor["username"], "instrument": instrument_id})
    return {"ok": True}


class SettingEdit(BaseModel):
    value: bool | int | float | str | list | dict | None = None


@router.put("/api/admin/users/{user_id}/settings/{key}")
def edit_setting(user_id: str, key: str, body: SettingEdit, actor=Depends(require("users.manage")), c=Depends(db)):
    _managed(c, actor, user_id)
    if not key.replace(".", "").replace("_", "").isalnum() or len(key) > 60:
        raise AuthError(400, "BAD_KEY", "Invalid setting name.")
    c.execute("INSERT INTO user_settings (user_id, key, value_json, updated_at) VALUES (?,?,?,?) "
              "ON CONFLICT(user_id, key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at", (user_id, key, json.dumps(body.value), time.time()))
    c.commit()
    audit.append(c, "admin.edit_setting", actor_id=actor["id"], actor_role=actor["role"], target_type="user", target_id=user_id, detail={"actor": actor["username"], "key": key})
    return {"ok": True}
