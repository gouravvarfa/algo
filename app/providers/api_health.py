"""Angel One API health, measured on the calls the app already makes (it never adds traffic of its own, except the developer's 'probe now').

Every Angel One HTTP request made through `transport()` is timed: endpoint, milliseconds, HTTP status, or the network error. The developer page
shows per endpoint the count, errors, rate-limit answers (403 / 429), and the median and 95th-percentile time over the last hour. Kept in memory only
(the last 3,000 calls), so a restart starts empty.
"""
from __future__ import annotations

import time
from collections import deque

import httpx

_EVENTS: deque = deque(maxlen=3000)          # (unix time, endpoint, ms, status or None, error name or None)
RATE_LIMIT_STATUSES = (403, 429)              # Angel One answers 403 "exceeding access rate" (and sometimes 429) when called too fast
_IP: dict = {"ip": None, "at": 0.0, "error": None}


class _Timed(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rstrip("/").rsplit("/", 1)[-1] or request.url.host
        t0 = time.perf_counter()
        try:
            r = await super().handle_async_request(request)
        except Exception as exc:
            _EVENTS.append((time.time(), name, (time.perf_counter() - t0) * 1000.0, None, type(exc).__name__))
            raise
        _EVENTS.append((time.time(), name, (time.perf_counter() - t0) * 1000.0, r.status_code, None))
        return r


def transport() -> httpx.AsyncBaseTransport:
    return _Timed()


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))], 1)


def summary(window_s: float = 3600.0) -> dict:
    since = time.time() - window_s
    by: dict[str, dict] = {}
    for t, name, ms, status, err in list(_EVENTS):
        if t < since:
            continue
        b = by.setdefault(name, {"endpoint": name, "calls": 0, "errors": 0, "rate_limited": 0, "_ms": [], "last_at": None, "last_error": None, "last_error_at": None})
        b["calls"] += 1
        b["_ms"].append(ms)
        b["last_at"] = t
        bad = err is not None or (status is not None and status >= 400)
        if bad:
            b["errors"] += 1
            b["last_error"], b["last_error_at"] = (err or f"HTTP {status}"), t
        if status in RATE_LIMIT_STATUSES:
            b["rate_limited"] += 1
    rows = []
    for b in by.values():
        ms = b.pop("_ms")
        rows.append({**b, "p50_ms": _pct(ms, 0.5), "p95_ms": _pct(ms, 0.95), "max_ms": round(max(ms), 1) if ms else None})
    rows.sort(key=lambda r: -r["calls"])
    total = sum(r["calls"] for r in rows)
    errs = sum(r["errors"] for r in rows)
    rl = sum(r["rate_limited"] for r in rows)
    status = "down" if total and errs == total else "degraded" if errs or rl else "ok" if total else "idle"
    return {"window_s": window_s, "calls": total, "errors": errs, "rate_limited": rl, "status": status, "endpoints": rows}


async def outbound_ip(max_age_s: float = 600.0) -> dict:
    """The public IP the server's requests leave from: the one that must be registered as the static IP at the broker. Cached for 10 minutes."""
    if _IP["ip"] and time.time() - _IP["at"] < max_age_s:
        return dict(_IP)
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get("https://api.ipify.org")
            _IP.update(ip=r.text.strip(), at=time.time(), error=None)
    except Exception as exc:
        _IP.update(at=time.time(), error=type(exc).__name__)
    return dict(_IP)


def clear() -> None:
    _EVENTS.clear()
