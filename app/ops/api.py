"""Developer panel and admin summary API.

  /api/dev/*            developer only: health board, function status, self-tests, circuit breakers, flags / kill switches, errors, sessions, engines
  /api/admin/system-summary   admin and developer: the small "is everything fine" widget
  /metrics              Prometheus text format (developer permission; put it behind the internal network in production)
Reads need `system.read`; anything that CHANGES something needs `system.manage` and is written to the audit log.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.auth_api import db, require
from app.db import audit
from app.ops import circuit, context, flags, health, incidents, selftests, stats
from app.security import sessions
from app.security.users import AuthError

router = APIRouter()


def _audit(c, actor, action: str, target_type: str | None = None, target_id: str | None = None, detail: dict | None = None) -> None:
    audit.append(c, action, actor_id=actor["id"], actor_role=actor["role"], target_type=target_type, target_id=target_id, detail=detail)


# ---- read ---------------------------------------------------------------------------------------------------------------------------
@router.get("/api/dev/health")
def dev_health(_=Depends(require("system.read"))):
    tiles = health.all_tiles()
    return {"overall": health.overall(tiles), "checked_at": time.time(), "tiles": tiles}


@router.get("/api/dev/functions")
def dev_functions(_=Depends(require("system.read"))):
    return {"functions": stats.snapshot(), "requests": stats.request_summary()}


@router.get("/api/dev/stream")
def dev_stream(_=Depends(require("system.read"))):
    hub = context.stream_hub
    eng = context.engine
    return {"enabled": bool(flags.get("feature.stream")), "available": hub is not None, **(hub.stats() if hub is not None else {}),
            "engine": eng.sweep_stats() if eng is not None and hasattr(eng, "sweep_stats") else None}


@router.get("/api/dev/errors")
def dev_errors(limit: int = 100, _=Depends(require("system.read"))):
    return {"groups": incidents.grouped(), "recent": incidents.recent(limit)}


@router.get("/api/dev/selftest")
def dev_selftest_last(_=Depends(require("system.read"))):
    return selftests.last()


@router.get("/api/dev/circuits")
def dev_circuits(_=Depends(require("system.read"))):
    return {"circuits": circuit.all_info()}


@router.get("/api/dev/flags")
def dev_flags(_=Depends(require("system.read"))):
    return {"flags": flags.all_flags()}


@router.get("/api/dev/sessions")
def dev_sessions(_=Depends(require("system.read")), c=Depends(db)):
    now = time.time()
    rows = c.execute("SELECT s.user_id, u.username, u.role, s.started_at, s.last_seen_at, s.last_activity_at, s.device_label FROM sessions s "
                     "JOIN users u ON u.id = s.user_id WHERE s.ended_at IS NULL ORDER BY s.started_at DESC").fetchall()
    return {"sessions": [{"user_id": r["user_id"], "username": r["username"], "role": r["role"], "device": r["device_label"], "started_at": sessions.iso_ist(r["started_at"]),
                          "last_seen_at": sessions.iso_ist(r["last_seen_at"]), "online": now - r["last_seen_at"] < sessions.HEARTBEAT_DEAD,
                          "idle_s": round(now - r["last_activity_at"])} for r in rows]}


@router.get("/api/dev/engines")
def dev_engines(_=Depends(require("system.read"))):
    e = context.engine
    if e is None:
        return {"running": False, "instruments": []}
    faults = getattr(e, "faults", {})
    return {"running": True, "paused_globally": flags.engine_paused(), "feed": e.feed.status(),
            "instruments": [{"id": i.id, "symbol": i.symbol, "trading_symbol": i.trading_symbol, "timeframe": i.timeframe,
                             "owner": getattr(i, "owner", None), "paused": flags.engine_paused(getattr(i, "owner", None)),
                             "fault": faults.get(i.id)} for i in e.instruments.values()]}


@router.get("/api/admin/system-summary")
def admin_system_summary(_=Depends(require("users.read"))):
    tiles = {t["key"]: t for t in health.all_tiles()}
    pick = lambda k: {"status": tiles[k]["status"], "reason": tiles[k]["reason"]} if k in tiles else None
    return {"overall": health.overall(list(tiles.values())), "feed": pick("feed"), "engine": pick("engine"), "users": pick("users"), "incidents": pick("incidents")}


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(_=Depends(require("system.read"))):
    lines = ["# HELP algo_function_calls_total Calls of tracked functions", "# TYPE algo_function_calls_total counter"]
    for f in stats.snapshot():
        n = f["name"].replace('"', "")
        lines += [f'algo_function_calls_total{{name="{n}"}} {f["calls_total"]}', f'algo_function_errors_total{{name="{n}"}} {f["errors_total"]}']
        if f["avg_ms"] is not None:
            lines.append(f'algo_function_avg_ms{{name="{n}"}} {f["avg_ms"]}')
    for b in circuit.all_info():
        lines.append(f'algo_circuit_open{{name="{b["name"]}"}} {1 if b["state"] == "OPEN" else 0}')
    r = stats.request_summary()
    lines += [f"algo_http_requests_5m {r['requests']}", f"algo_http_errors_5m {r['errors_5xx']}"]
    for t in health.all_tiles():
        lines.append(f'algo_component_up{{component="{t["key"]}"}} {0 if t["status"] == health.DOWN else 1}')
    return "\n".join(lines) + "\n"


# ---- change (developer only, audited) ---------------------------------------------------------------------------------------------------
@router.post("/api/dev/selftest/run")
def dev_selftest_run(actor=Depends(require("system.manage")), c=Depends(db)):
    res = selftests.run_all()
    _audit(c, actor, "dev.selftest", detail={"passed": res["passed"], "failed": res["failed"]})
    return res


class CircuitAction(BaseModel):
    action: str = Field(pattern="^(open|close|release)$")      # open = force open, close = force closed, release = back to automatic


@router.post("/api/dev/circuits/{name}")
def dev_circuit_set(name: str, body: CircuitAction, actor=Depends(require("system.manage")), c=Depends(db)):
    b = circuit.breaker(name)
    b.force({"open": "open", "close": "closed", "release": None}[body.action])
    _audit(c, actor, f"dev.circuit.{body.action}", target_type="circuit", target_id=name)
    incidents.record(f"circuit.{name}", f"manually set to '{body.action}' by {actor['username']}", severity="info", auto_action=body.action)
    return b.info()


class FlagBody(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    value: bool | int | float | str
    scope: str = "global"


@router.put("/api/dev/flags")
def dev_flag_set(body: FlagBody, actor=Depends(require("system.manage")), c=Depends(db)):
    try:
        flags.set_flag(body.key, body.value, body.scope, updated_by=actor["id"])
    except ValueError as exc:
        raise AuthError(400, "BAD_SCOPE", str(exc))
    _audit(c, actor, "dev.flag.set", target_type="flag", target_id=f"{body.key}@{body.scope}", detail={"value": body.value})
    return {"flags": flags.all_flags()}


@router.delete("/api/dev/flags")
def dev_flag_clear(key: str, scope: str = "global", actor=Depends(require("system.manage")), c=Depends(db)):
    flags.clear_flag(key, scope)
    _audit(c, actor, "dev.flag.clear", target_type="flag", target_id=f"{key}@{scope}")
    return {"flags": flags.all_flags()}


class PauseBody(BaseModel):
    paused: bool = True


@router.post("/api/dev/users/{user_id}/pause-engine")
def dev_pause_user_engine(user_id: str, body: PauseBody, actor=Depends(require("system.manage")), c=Depends(db)):
    flags.set_flag("engine.paused", body.paused, f"user:{user_id}", updated_by=actor["id"])
    _audit(c, actor, "dev.engine.pause" if body.paused else "dev.engine.resume", target_type="user", target_id=user_id)
    incidents.record("engine", f"{actor['username']} {'paused' if body.paused else 'resumed'} the engine of user {user_id}", severity="info", user_id=user_id,
                     auto_action="paused" if body.paused else "resumed")
    return {"user_id": user_id, "paused": body.paused}


@router.post("/api/dev/engines/pause-all")
def dev_pause_all(body: PauseBody, actor=Depends(require("system.manage")), c=Depends(db)):
    """The global kill switch: no strategy engine evaluates. Nothing else is stopped (login, pages and stored data keep working)."""
    flags.set_flag("engines.paused", body.paused, "global", updated_by=actor["id"])
    _audit(c, actor, "dev.kill_switch.on" if body.paused else "dev.kill_switch.off", target_type="engines")
    incidents.record("engine", f"global kill switch {'ON' if body.paused else 'OFF'} by {actor['username']}", severity="warning", auto_action="engines paused" if body.paused else "engines resumed")
    return {"engines_paused": body.paused}


@router.post("/api/dev/incidents/{incident_id}/resolve")
def dev_incident_resolve(incident_id: int, actor=Depends(require("system.manage")), c=Depends(db)):
    incidents.resolve(incident_id)
    _audit(c, actor, "dev.incident.resolve", target_type="incident", target_id=str(incident_id))
    return {"ok": True}


# ---- more developer screens: login attempts, roles, system info, live logs -----------------------------------------------------------------
LOGIN_ACTIONS = ("login.success", "login.failed", "login.locked", "login.refused", "login.takeover", "password.change_failed", "user.setup_complete")


@router.get("/api/dev/login-attempts")
def dev_login_attempts(limit: int = 200, only_failed: bool = False, _=Depends(require("audit.read")), c=Depends(db)):
    """Who tried to sign in, from where (address only) and what happened: the failed ones are what a developer watches for."""
    marks = ",".join("?" * len(LOGIN_ACTIONS))
    extra = " AND action IN ('login.failed','login.locked','login.refused','password.change_failed')" if only_failed else ""
    rows = c.execute(f"SELECT id, ts, actor_id, action, detail_json FROM audit_log WHERE action IN ({marks}){extra} ORDER BY id DESC LIMIT ?",
                     (*LOGIN_ACTIONS, max(1, min(limit, 500)))).fetchall()
    hour_ago = time.time() - 3600
    fails = c.execute("SELECT COUNT(*) FROM login_failures WHERE ts >= ?", (hour_ago,)).fetchone()[0]
    return {"failed_last_hour": fails, "entries": [{"id": r["id"], "ts": r["ts"], "action": r["action"], "actor_id": r["actor_id"], "detail": r["detail_json"]} for r in rows]}


@router.get("/api/dev/roles")
def dev_roles(_=Depends(require("system.read"))):
    from app.security import authz
    return {"roles": [{"role": r, "rank": authz.RANK[r], "permissions": authz.permissions(r)} for r in authz.ROLES],
            "rules": ["Roles are cumulative: developer includes admin, admin includes user.",
                      "Developers create admins; only the owner (the first developer) creates developers; admins create users.",
                      "Nobody can disable, remove or sign out themselves, and the last admin / developer cannot be removed.",
                      "Everyone with the 'view as' permission sees users ranked below them, read-only; secrets (passwords, authenticator keys, broker keys) are never shown."]}


@router.get("/api/dev/system-info")
def dev_system_info(_=Depends(require("system.read")), c=Depends(db)):
    import os
    import platform
    import sys
    from pathlib import Path
    data = Path(__file__).resolve().parents[2] / "data"
    files = []
    for f in sorted(data.rglob("*.db")) if data.exists() else []:
        try:
            files.append({"name": str(f.relative_to(data)), "mb": round(f.stat().st_size / 1e6, 2)})
        except OSError:
            pass
    counts = {k: c.execute(q).fetchone()[0] for k, q in (("users", "SELECT COUNT(*) FROM users WHERE status!='DELETED'"), ("sessions_active", "SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL"),
                                                          ("audit_rows", "SELECT COUNT(*) FROM audit_log"), ("incidents", "SELECT COUNT(*) FROM incidents"))}
    checks = [
        {"name": "Encryption key", "ok": bool(os.environ.get("APP_ENC_KEY")), "note": "APP_ENC_KEY protects the authenticator secrets. Without it a key file next to the database is used: set it on a server."},
        {"name": "Takeover from the login page", "ok": os.environ.get("APP_ALLOW_TAKEOVER", "1").strip().lower() in ("0", "false", "no", "off"), "note": "off is the stricter setting (APP_ALLOW_TAKEOVER=0)"},
        {"name": "Angel One credentials file", "ok": Path("angelone_credentials.json").exists() or bool(os.environ.get("ANGELONE_API_KEY")), "note": "needed for live data"},
    ]
    import fastapi
    return {"python": sys.version.split()[0], "platform": platform.platform(), "fastapi": fastapi.__version__, "uptime_s": round(time.time() - context.started_at),
            "data_dir": str(data), "databases": files, "counts": counts, "checks": checks, "server_time": time.time()}


@router.get("/api/dev/live-logs")
def dev_live_logs(lines: int = 300, level: str = "", _=Depends(require("system.read"))):
    """The tail of the server's error log (data/server.err when started with start.bat / the background launcher). Secrets are masked before sending."""
    import re
    from pathlib import Path
    f = Path(__file__).resolve().parents[2] / "data" / "server.err"
    if not f.exists():
        return {"available": False, "lines": [], "note": "No log file at data/server.err. On a server read it with: journalctl -u algo-trading -f"}
    size = f.stat().st_size
    with f.open("rb") as fh:
        fh.seek(max(0, size - 200_000))
        text = fh.read().decode("utf-8", "replace")
    out = text.splitlines()[-max(10, min(lines, 1000)):]
    out = [re.sub(r"(?i)(token|password|secret|totp|cookie|authorization)([\"'=: ]+)[^\s,\"']+", r"\1\2***", ln) for ln in out]
    if level:
        out = [ln for ln in out if level.upper() in ln]
    return {"available": True, "size_kb": round(size / 1024), "lines": out}
