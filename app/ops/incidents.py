"""Incident log: every isolated failure, with what the system did about it. Stored in the platform database (table `incidents`) and
mirrored in a small in-memory ring so the developer panel still works if the database is busy. Recording NEVER raises: reporting a
problem must not become a second problem.
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque

from app.db import platform_db

log = logging.getLogger("ops")
_recent: deque = deque(maxlen=500)


def record(component: str, message: str, severity: str = "error", user_id: str | None = None, detail: dict | None = None,
           auto_action: str | None = None) -> None:
    now = time.time()
    entry = {"id": None, "ts": now, "component": component, "user_id": user_id, "severity": severity, "message": message[:500],
             "detail": detail, "auto_action": auto_action, "resolved_at": None}
    try:
        c = platform_db.connect()
        try:
            cur = c.execute("INSERT INTO incidents (ts, component, user_id, severity, message, detail_json, auto_action) VALUES (?,?,?,?,?,?,?)",
                            (now, component, user_id, severity, entry["message"], json.dumps(detail) if detail else None, auto_action))
            c.commit()
            entry["id"] = cur.lastrowid
        finally:
            c.close()
    except Exception:
        log.exception("could not store incident (kept in memory only)")
    _recent.append(entry)
    log.warning("incident [%s] %s%s", component, message[:200], f" -> {auto_action}" if auto_action else "")


def recent(limit: int = 100) -> list[dict]:
    try:
        c = platform_db.connect()
        try:
            rows = c.execute("SELECT id, ts, component, user_id, severity, message, detail_json, auto_action, resolved_at FROM incidents "
                             "ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
        finally:
            c.close()
        return [{"id": r["id"], "ts": r["ts"], "component": r["component"], "user_id": r["user_id"], "severity": r["severity"], "message": r["message"],
                 "detail": json.loads(r["detail_json"]) if r["detail_json"] else None, "auto_action": r["auto_action"], "resolved_at": r["resolved_at"]}
                for r in rows]
    except Exception:
        return list(reversed(_recent))[:limit]


def grouped(limit: int = 500) -> list[dict]:
    """Errors grouped by cause: count, first / last seen, affected users."""
    groups: dict[tuple, dict] = {}
    for e in recent(limit):
        key = (e["component"], e["message"][:80])
        g = groups.setdefault(key, {"component": e["component"], "message": e["message"][:80], "count": 0, "first_seen": e["ts"], "last_seen": e["ts"],
                                    "users": set(), "last_action": e["auto_action"]})
        g["count"] += 1
        g["first_seen"], g["last_seen"] = min(g["first_seen"], e["ts"]), max(g["last_seen"], e["ts"])
        if e["user_id"]:
            g["users"].add(e["user_id"])
    out = [{**g, "users": sorted(g["users"])} for g in groups.values()]
    out.sort(key=lambda g: -g["last_seen"])
    return out


def resolve(incident_id: int) -> None:
    c = platform_db.connect()
    try:
        c.execute("UPDATE incidents SET resolved_at=? WHERE id=? AND resolved_at IS NULL", (time.time(), incident_id))
        c.commit()
    finally:
        c.close()


def count_since(seconds: float, severity: str | None = None) -> int:
    try:
        c = platform_db.connect()
        try:
            q, args = "SELECT COUNT(*) FROM incidents WHERE ts >= ?", [time.time() - seconds]
            if severity:
                q, args = q + " AND severity = ?", args + [severity]
            return c.execute(q, args).fetchone()[0]
        finally:
            c.close()
    except Exception:
        return sum(1 for e in _recent if e["ts"] >= time.time() - seconds)
