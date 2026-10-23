"""Feature flags and kill switches, changeable at runtime by a developer without a deploy.

A flag has a key and a scope: 'global' or 'user:<id>'. A user-scoped value wins over the global one. Values are JSON (true/false for
switches). Reads are cached for a couple of seconds so the engine can ask on every tick without touching the database each time.
Known switches (all default to the safe, normal value):
  engines.paused      true  -> NO strategy engine evaluates (global kill switch; screens keep showing the last stored state, labelled)
  engine.paused       true  -> that one user's engine stops (scope user:<id>)
  feature.live_chain  false -> the live option chain endpoint answers "switched off"
  feature.margin      false -> the margin calculator is not called
  feature.collector   false -> the daily option download does not run
"""
from __future__ import annotations

import json
import time

from app.db import platform_db

DEFAULTS = {"engines.paused": False, "engine.paused": False, "feature.live_chain": True, "feature.margin": True, "feature.collector": True}
_CACHE_SECONDS = 2.0
_cache: dict | None = None
_cache_at = 0.0


def _load() -> dict:
    global _cache, _cache_at
    now = time.time()
    if _cache is not None and now - _cache_at < _CACHE_SECONDS:
        return _cache
    data: dict[tuple[str, str], object] = {}
    try:
        c = platform_db.connect()
        try:
            for r in c.execute("SELECT key, scope, value_json FROM feature_flags"):
                data[(r["key"], r["scope"])] = json.loads(r["value_json"])
        finally:
            c.close()
    except Exception:
        if _cache is not None:
            return _cache                     # database busy: keep the last known switches rather than silently flipping them
    _cache, _cache_at = data, now
    return data


def get(key: str, user_id: str | None = None, default=None):
    data = _load()
    if user_id is not None and (key, f"user:{user_id}") in data:
        return data[(key, f"user:{user_id}")]
    if (key, "global") in data:
        return data[(key, "global")]
    return DEFAULTS.get(key, default)


def set_flag(key: str, value, scope: str = "global", updated_by: str | None = None) -> None:
    global _cache
    if scope != "global" and not scope.startswith("user:"):
        raise ValueError("scope must be 'global' or 'user:<id>'")
    c = platform_db.connect()
    try:
        c.execute("INSERT INTO feature_flags (key, scope, value_json, updated_by, updated_at) VALUES (?,?,?,?,?) "
                  "ON CONFLICT(key, scope) DO UPDATE SET value_json=excluded.value_json, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                  (key, scope, json.dumps(value), updated_by, time.time()))
        c.commit()
    finally:
        c.close()
    _cache = None


def clear_flag(key: str, scope: str = "global") -> None:
    global _cache
    c = platform_db.connect()
    try:
        c.execute("DELETE FROM feature_flags WHERE key=? AND scope=?", (key, scope))
        c.commit()
    finally:
        c.close()
    _cache = None


def all_flags() -> list[dict]:
    rows = []
    try:
        c = platform_db.connect()
        try:
            rows = [dict(r) for r in c.execute("SELECT key, scope, value_json, updated_by, updated_at FROM feature_flags ORDER BY key, scope")]
        finally:
            c.close()
    except Exception:
        pass
    out = [{"key": r["key"], "scope": r["scope"], "value": json.loads(r["value_json"]), "updated_by": r["updated_by"], "updated_at": r["updated_at"], "default": DEFAULTS.get(r["key"])} for r in rows]
    have = {(o["key"], o["scope"]) for o in out}
    out += [{"key": k, "scope": "global", "value": v, "updated_by": None, "updated_at": None, "default": v} for k, v in DEFAULTS.items() if (k, "global") not in have]
    return sorted(out, key=lambda o: (o["key"], o["scope"]))


def engine_paused(user_id: str | None = None) -> bool:
    return bool(get("engines.paused")) or (user_id is not None and bool(get("engine.paused", user_id=user_id)))


def reset_cache() -> None:
    global _cache
    _cache = None
