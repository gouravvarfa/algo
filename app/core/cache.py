"""
Cache abstraction. Defaults to an in-process TTL cache (CACHE_URL=memory://)
so the app runs with zero external dependencies; set CACHE_URL=redis://... in
.env to switch to Redis without touching call sites.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app.core.config import settings


class MemoryCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < time.time():
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        self._store[key] = (time.time() + ttl_seconds, value)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)


class FileCache:
    """
    Pickle-file-backed cache that survives process restarts. Used as the dev
    default (CACHE_URL=file://...) so repeated local runs/tests don't re-spend
    Tapetide's daily MCP call quota (the free tier is 50 calls/day — hit
    during development of this app) fetching data that hasn't gone stale.
    Not multi-process safe; fine for a single local dev/backend process.
    """

    def __init__(self, path: str) -> None:
        import pickle

        self._pickle = pickle
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._store: dict[str, tuple[float, Any]] = self._load()

    def _load(self) -> dict[str, tuple[float, Any]]:
        if not self._path.exists():
            return {}
        try:
            with open(self._path, "rb") as f:
                return self._pickle.load(f)
        except Exception:
            return {}

    def _save(self) -> None:
        with open(self._path, "wb") as f:
            self._pickle.dump(self._store, f)

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < time.time():
            del self._store[key]
            self._save()
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        self._store[key] = (time.time() + ttl_seconds, value)
        self._save()

    def delete(self, key: str) -> None:
        self._store.pop(key, None)
        self._save()


class RedisCache:
    def __init__(self, url: str) -> None:
        import redis

        self._client = redis.Redis.from_url(url)

    def get(self, key: str) -> Any | None:
        import pickle

        raw = self._client.get(key)
        return pickle.loads(raw) if raw is not None else None

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        import pickle

        self._client.set(key, pickle.dumps(value), ex=ttl_seconds)

    def delete(self, key: str) -> None:
        self._client.delete(key)


def _build_cache():
    if settings.cache_url.startswith("redis://"):
        return RedisCache(settings.cache_url)
    if settings.cache_url.startswith("file://"):
        return FileCache(settings.cache_url[len("file://"):])
    return MemoryCache()


cache = _build_cache()

# Suggested TTLs by data kind (section 25: daily data cached longer than intraday-ish/live data).
TTL_LIVE_QUOTE = 30
TTL_DAILY_OHLCV = 6 * 3600
TTL_INDEX_HISTORY = 6 * 3600
TTL_UNIVERSE = 12 * 3600
TTL_SECTOR_PERFORMANCE = 6 * 3600
TTL_INDICATOR_SNAPSHOT = 6 * 3600
