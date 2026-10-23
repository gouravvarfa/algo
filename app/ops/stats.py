"""Function and request statistics for the developer panel.

`@tracked("name")` wraps a function (sync or async) and records, per name: calls, errors, duration, last success, last error. It adds
microseconds, never changes the return value, and re-raises every exception unchanged. A recent window (last hour) is kept in memory so
calls/hour, average and p95 are real numbers, not guesses. Nothing here talks to the network or the database.
"""
from __future__ import annotations

import functools
import inspect
import threading
import time
from collections import deque
from dataclasses import dataclass, field

WINDOW_SECONDS = 3600
_MAX_SAMPLES = 20000


@dataclass
class FnStat:
    name: str
    calls: int = 0
    errors: int = 0
    last_success: float | None = None
    last_error: float | None = None
    last_error_msg: str | None = None
    samples: deque = field(default_factory=lambda: deque(maxlen=_MAX_SAMPLES))      # (ts, ms, ok)


_lock = threading.Lock()
_stats: dict[str, FnStat] = {}


def _get(name: str) -> FnStat:
    s = _stats.get(name)
    if s is None:
        with _lock:
            s = _stats.setdefault(name, FnStat(name))
    return s


def record(name: str, ms: float, ok: bool, error: BaseException | None = None) -> None:
    s = _get(name)
    now = time.time()
    s.calls += 1
    s.samples.append((now, ms, ok))
    if ok:
        s.last_success = now
    else:
        s.errors += 1
        s.last_error = now
        s.last_error_msg = f"{type(error).__name__}: {str(error)[:200]}" if error is not None else "error"


def tracked(name: str):
    """Decorator: `@tracked("top_bottom.run_intraday")`. Works on `def` and `async def`."""
    def wrap(fn):
        _get(name)                      # shows up on the board even before its first call
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*a, **kw):
                t0 = time.perf_counter()
                try:
                    out = await fn(*a, **kw)
                except BaseException as exc:
                    record(name, (time.perf_counter() - t0) * 1000, False, exc)
                    raise
                record(name, (time.perf_counter() - t0) * 1000, True)
                return out
            return awrapper

        @functools.wraps(fn)
        def wrapper(*a, **kw):
            t0 = time.perf_counter()
            try:
                out = fn(*a, **kw)
            except BaseException as exc:
                record(name, (time.perf_counter() - t0) * 1000, False, exc)
                raise
            record(name, (time.perf_counter() - t0) * 1000, True)
            return out
        return wrapper
    return wrap


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def snapshot(now: float | None = None) -> list[dict]:
    """One row per tracked function: window numbers (last hour) plus lifetime totals and a status.
    OK: no error in the window (or errors under 2%) · DEGRADED: errors 2% to 20% · FAULTY: more than 20% errors, or the last 3 calls failed."""
    now = time.time() if now is None else now
    out = []
    for s in sorted(_stats.values(), key=lambda x: x.name):
        win = [x for x in list(s.samples) if now - x[0] <= WINDOW_SECONDS]
        n = len(win)
        errs = sum(1 for x in win if not x[2])
        ms = [x[1] for x in win]
        last3 = list(s.samples)[-3:]
        rate = errs / n if n else 0.0
        if n and (rate > 0.20 or (len(last3) == 3 and all(not x[2] for x in last3))):
            status = "FAULTY"
        elif n and rate >= 0.02:
            status = "DEGRADED"
        else:
            status = "OK" if s.calls else "IDLE"
        out.append({"name": s.name, "status": status, "calls_total": s.calls, "errors_total": s.errors, "calls_hour": n, "errors_hour": errs,
                    "error_pct": round(rate * 100, 2), "avg_ms": round(sum(ms) / n, 3) if n else None, "p95_ms": round(_pct(ms, 0.95), 3) if n else None,
                    "last_success": s.last_success, "last_error": s.last_error, "last_error_msg": s.last_error_msg})
    return out


def reset() -> None:
    with _lock:
        _stats.clear()


# ---- HTTP request metrics (per route template, recorded by the middleware in app/main.py) ------------------------------------------
_req: deque = deque(maxlen=50000)       # (ts, route, ms, status)


def record_request(route: str, ms: float, status: int) -> None:
    _req.append((time.time(), route, ms, status))


def request_summary(now: float | None = None) -> dict:
    now = time.time() if now is None else now
    win = [x for x in list(_req) if now - x[0] <= 300]
    ms = [x[2] for x in win]
    errs = sum(1 for x in win if x[3] >= 500)
    by_route: dict[str, list] = {}
    for _, route, m, st in win:
        by_route.setdefault(route, []).append((m, st))
    routes = [{"route": r, "count": len(v), "avg_ms": round(sum(m for m, _ in v) / len(v), 2), "errors": sum(1 for _, st in v if st >= 500)}
              for r, v in by_route.items()]
    routes.sort(key=lambda x: -x["count"])
    return {"window_s": 300, "requests": len(win), "errors_5xx": errs, "error_pct": round(errs / len(win) * 100, 2) if win else 0.0,
            "p50_ms": round(_pct(ms, 0.5), 2) if ms else None, "p95_ms": round(_pct(ms, 0.95), 2) if ms else None,
            "p99_ms": round(_pct(ms, 0.99), 2) if ms else None, "routes": routes[:15]}
