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
