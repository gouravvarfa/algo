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
from pathlib import Path

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
    return {"ip_allow_list": nets, "ip_allow_list_on": bool(nets), "idle_timeout_min": None, "daily_signout": "16:00 IST",
            "absolute_timeout_h": sessions.ABSOLUTE_LIFETIME // 3600, "lockout": "5 failed attempts lock a username and an address for 15 minutes",
            "mfa": "password + authenticator code on every sign-in", "one_device": True}


# ---- admins can change what belongs to an account (never its password or authenticator secret: those are reset through a new setup link) ----
# Every change below is written to the audit log and shown to the person under "Account activity" (GET /api/me/activity).
def _managed(c, actor, user_id: str, allow_self: bool = False):
    t = _target(c, actor, user_id)
    if allow_self and t["id"] == actor["id"]:
        return t                                   # stock-futures strategies: an admin may assign them to their own account too
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
    from app import option_journal
    option_journal.sync(c, user_id, symbol, body.positions)
    audit.append(c, "admin.edit_positions", actor_id=actor["id"], actor_role=actor["role"], target_type="user", target_id=user_id,
                 detail={"actor": actor["username"], "symbol": symbol, "count": len(body.positions)})
    return {"ok": True, "count": len(body.positions)}


@router.delete("/api/admin/users/{user_id}/instruments/{instrument_id}")
def remove_instrument(user_id: str, instrument_id: str, actor=Depends(require("users.manage")), c=Depends(db)):
    from fastapi import HTTPException
    from app.ops import context
    _managed(c, actor, user_id, allow_self=True)
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


# ---- Top-Bottom strategy on / off per account --------------------------------------------------------------------------------------------
# Off = the engine stops evaluating that person's strategy (no new signals, entries or exits); pages and stored data keep working.
# A person can switch their own off and on. When an admin switches it off the account is "locked": only an admin can switch it back on.
class StrategyBody(BaseModel):
    enabled: bool


def strategy_state(user_id: str) -> dict:
    from app.ops import flags
    return {"enabled": flags.get("strategy.enabled", user_id=user_id) is not False, "locked": bool(flags.get("strategy.locked", user_id=user_id))}


@router.get("/api/me/strategy")
def my_strategy(request: Request):
    u = getattr(request.state, "user", None)
    return strategy_state(u["id"]) if u else {"enabled": True, "locked": False}


@router.post("/api/me/strategy")
def set_my_strategy(body: StrategyBody, request: Request, c=Depends(db)):
    from app.ops import flags
    u = getattr(request.state, "user", None)
    if u is None:
        raise AuthError(401, "LOGIN_REQUIRED", "Login required.")
    if body.enabled and strategy_state(u["id"])["locked"]:
        raise AuthError(403, "LOCKED", "An admin switched your strategy off. Ask them to switch it back on.")
    flags.set_flag("strategy.enabled", body.enabled, f"user:{u['id']}", updated_by=u["id"])
    audit.append(c, "strategy.on" if body.enabled else "strategy.off", actor_id=u["id"], actor_role=u["role"], target_type="user", target_id=u["id"], detail={"actor": u["username"], "by": "self"})
    return strategy_state(u["id"])


@router.post("/api/admin/users/{user_id}/strategy")
def set_user_strategy(user_id: str, body: StrategyBody, actor=Depends(require("users.manage")), c=Depends(db)):
    from app.ops import flags
    _managed(c, actor, user_id)
    flags.set_flag("strategy.enabled", body.enabled, f"user:{user_id}", updated_by=actor["id"])
    flags.set_flag("strategy.locked", not body.enabled, f"user:{user_id}", updated_by=actor["id"])
    audit.append(c, "strategy.on" if body.enabled else "strategy.off", actor_id=actor["id"], actor_role=actor["role"], target_type="user", target_id=user_id,
                 detail={"actor": actor["username"], "by": "admin"})
    return strategy_state(user_id)


# ---- strategies assigned by an admin -----------------------------------------------------------------------------------------------------
# A person gets the BankNifty strategy on their own; any other symbol (stock futures) is assigned to ONE account by an admin. The strategy is
# Top-Bottom on the daily candles of that futures contract, rolled from contract to contract with the same rule as BankNifty.
import re  # noqa: E402

_SYMBOL_RE = re.compile(r"^[A-Z0-9&-]{1,20}$")
HISTORY_SINCE = "2026-01-01"


def fo_stocks() -> list[str]:
    """The stock-futures underlyings an admin may assign (NSE's list of F&O stocks, kept in app/providers/fo_stocks.json)."""
    try:
        return json.loads((Path(__file__).resolve().parent / "providers" / "fo_stocks.json").read_text(encoding="utf8"))["symbols"]
    except (OSError, ValueError, KeyError):
        return []


@router.get("/api/admin/fo-stocks")
def fo_stock_list(_=Depends(require("users.read"))):
    return {"symbols": fo_stocks()}


class AssignBody(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    lots: int = Field(default=1, ge=1, le=50)


def _engine_or_503():
    from fastapi import HTTPException
    from app.ops import context
    if context.engine is None:
        raise HTTPException(503, "engine not running")
    return context.engine


@router.get("/api/admin/users/{user_id}/strategies")
def list_strategies(user_id: str, actor=Depends(require("users.read")), c=Depends(db)):
    _target(c, actor, user_id)
    e = _engine_or_503()
    out = []
    for i in e.instruments.values():
        if i.owner != user_id:
            continue
        live = ((e.snapshots.get(i.id) or {}).get("instrument") or {})       # the contract the strategy is on NOW (after any roll), not the one it started on
        out.append({"id": i.id, "symbol": i.symbol, "trading_symbol": live.get("trading_symbol") or i.trading_symbol, "lots": i.lots, "quantity": i.quantity,
                    "expiry": live.get("expiry") or i.expiry, "timeframe": i.timeframe, "fault": (e.faults.get(i.id) or {}).get("message")})
    return {"strategies": out}


@router.post("/api/admin/users/{user_id}/strategies")
async def assign_strategy(user_id: str, body: AssignBody, actor=Depends(require("users.manage")), c=Depends(db)):
    from fastapi import HTTPException
    from app.paper_trading.engine import PaperError
    _managed(c, actor, user_id, allow_self=True)
    e = _engine_or_503()
    symbol = body.symbol.strip().upper()
    if not _SYMBOL_RE.match(symbol) or symbol not in fo_stocks():
        raise AuthError(400, "BAD_SYMBOL", f"{symbol} is not in the F&O stock list. Pick one from the list.")
    if any(i.owner == user_id and i.symbol == symbol and i.timeframe == "1D" and i.instrument_type == "FUTURE" for i in e.instruments.values()):
        raise AuthError(409, "ALREADY_ASSIGNED", f"{symbol} is already assigned to this account.")
    lookback = (datetime.now(timezone.utc).date() - datetime.fromisoformat(HISTORY_SINCE).date()).days + 1
    try:
        inst = await e.add(symbol, "FUTURE", "1D", body.lots, lookback, owner=user_id)
    except PaperError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Angel One error: {exc}")
    audit.append(c, "admin.assign_strategy", actor_id=actor["id"], actor_role=actor["role"], target_type="user", target_id=user_id,
                 detail={"actor": actor["username"], "symbol": symbol, "lots": body.lots})
    return {"id": inst.id, "symbol": inst.symbol, "trading_symbol": inst.trading_symbol, "error": e.errors.get(inst.id)}
