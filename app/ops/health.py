"""System health: one tile per component, each OK / DEGRADED / DOWN (or N/A when the component is not running) with a one-line reason.

Everything here only READS state the app already keeps (counters, timestamps, the feed's own status, the database); it never calls
Angel One and never changes anything, so asking for health cannot cause a problem itself.
"""
from __future__ import annotations

import datetime as dt
import shutil
import time
from pathlib import Path

from app.db import audit, platform_db
from app.ops import circuit, context, incidents, selftests, stats

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
OK, DEGRADED, DOWN, NA = "OK", "DEGRADED", "DOWN", "N/A"


def _tile(key: str, title: str, status: str, reason: str, **metrics) -> dict:
    return {"key": key, "title": title, "status": status, "reason": reason, "metrics": metrics, "checked_at": time.time()}


def _market_open() -> bool:
    from app.paper_trading.engine import market_open
    return market_open()


def api_tile() -> dict:
    s = stats.request_summary()
    if s["requests"] == 0:
        return _tile("api", "API server", OK, "no requests in the last 5 minutes", uptime_s=round(time.time() - context.started_at), **{k: s[k] for k in ("p50_ms", "p95_ms", "p99_ms")})
    st = DOWN if s["error_pct"] > 25 else DEGRADED if s["error_pct"] > 3 else OK
    return _tile("api", "API server", st, f"{s['requests']} requests in 5 min, {s['error_pct']}% server errors, p95 {s['p95_ms']} ms",
                 uptime_s=round(time.time() - context.started_at), p50_ms=s["p50_ms"], p95_ms=s["p95_ms"], p99_ms=s["p99_ms"], error_pct=s["error_pct"])


def database_tile() -> dict:
    t0 = time.perf_counter()
    try:
        c = platform_db.connect()
        try:
            c.execute("SELECT 1").fetchone()
            version = platform_db.version(c)
        finally:
            c.close()
    except Exception as exc:
        return _tile("database", "Database", DOWN, f"cannot open the platform database: {str(exc)[:120]}")
    ms = (time.perf_counter() - t0) * 1000
    free_gb = shutil.disk_usage(DATA_DIR if DATA_DIR.exists() else DATA_DIR.parent).free / 1e9
    st = DOWN if free_gb < 0.5 else DEGRADED if free_gb < 3 or ms > 500 else OK
    return _tile("database", "Database", st, f"query {ms:.1f} ms, schema v{version}, disk free {free_gb:.1f} GB", query_ms=round(ms, 2), schema_version=version, disk_free_gb=round(free_gb, 2))


def feed_tile() -> dict:
    e = context.engine
    if e is None:
        return _tile("feed", "Market feed (WebSocket)", NA, "engine not running")
    f = e.feed.status()
    market = _market_open()
    metrics = dict(connected=f["connected"], last_tick_age_s=f["last_tick_age_s"], reconnects=f["reconnects"], subscribed=f["subscribed"], error=f["error"])
    if not f["connected"]:
        return _tile("feed", "Market feed (WebSocket)", DOWN if market else DEGRADED, f"not connected{' during market hours' if market else ' (market closed)'}: {f['error'] or 'reconnecting'}", **metrics)
    age = f["last_tick_age_s"]
    if market and (age is None or age > 30):
        return _tile("feed", "Market feed (WebSocket)", DEGRADED if (age or 999) < 120 else DOWN, f"connected but the last tick is {age}s old during market hours", **metrics)
    return _tile("feed", "Market feed (WebSocket)", OK, f"connected, {f['subscribed']} tokens, last tick {age}s ago" + ("" if market else " (market closed)"), **metrics)


def angel_rest_tile() -> dict:
    b = next((x for x in circuit.all_info() if x["name"] == "angelone.rest"), None)
    try:
        from app.providers import call_metrics
        calls = call_metrics.snapshot().angelone_calls
    except Exception:
        calls = None
    if b is None:
        return _tile("angel_rest", "Angel One REST", OK, "no calls made yet", calls_today=calls)
    st = DOWN if b["state"] == "OPEN" else DEGRADED if b["state"] == "HALF_OPEN" or b["failures_in_window"] >= max(2, b["threshold"] // 2) else OK
    why = f"circuit {b['state']}" + (f", last error: {b['last_error']}" if b["last_error"] and st != OK else "")
    return _tile("angel_rest", "Angel One REST", st, why, calls_today=calls, **{k: b[k] for k in ("state", "failures_in_window", "opened_count", "total_calls", "total_failures")})


def engine_tile() -> dict:
    from app.ops import flags
    e = context.engine
    if e is None:
        return _tile("engine", "Strategy engine", NA, "not running")
    paused = flags.engine_paused()
    faults = getattr(e, "faults", {})
    last = e.last_tick
    age = None if last is None else (time.time() - last.replace(tzinfo=None).timestamp() if last.tzinfo is None else time.time() - last.timestamp())
    market = _market_open()
    metrics = dict(paused=paused, instruments=len(e.instruments), faults=len(faults), last_evaluation_age_s=None if age is None else round(age, 1))
    if paused:
        return _tile("engine", "Strategy engine", DEGRADED, "PAUSED by a developer kill switch: screens show the last stored state", **metrics)
    if faults:
        return _tile("engine", "Strategy engine", DEGRADED, f"{len(faults)} instrument(s) in FAULT: " + "; ".join(f"{k}: {v['message'][:60]}" for k, v in list(faults.items())[:3]), **metrics)
    if market and (age is None or age > 600):
        return _tile("engine", "Strategy engine", DOWN, f"no evaluation for {'ever' if age is None else str(round(age)) + 's'} during market hours", **metrics)
    if market and age > 120:
        return _tile("engine", "Strategy engine", DEGRADED, f"last evaluation {round(age)}s ago", **metrics)
    return _tile("engine", "Strategy engine", OK, f"{len(e.instruments)} instrument(s), last evaluation {'n/a' if age is None else str(round(age)) + 's ago'}", **metrics)


def collector_tile() -> dict:
    from app.options import collector
    st = collector.status()
    today = dt.datetime.now().date().isoformat()
    last = st.get("last_run_date")
    bad = [k for k in ("NIFTY", "BANKNIFTY") if k in st and not st[k].get("ok", True)]
    now = dt.datetime.now()
    due = now.weekday() < 5 and now.time() >= collector.RUN_AFTER and last != today
    if bad:
        return _tile("collector", "Option collector (daily download)", DEGRADED, f"last run failed for {', '.join(bad)}", last_run_date=last)
    if due and now.time() > dt.time(17, 0):
        return _tile("collector", "Option collector (daily download)", DEGRADED, "today's download has not completed (due 15:45, expected by 17:00)", last_run_date=last)
    got = {k: st[k].get("option_minutes") for k in ("NIFTY", "BANKNIFTY") if k in st}
    return _tile("collector", "Option collector (daily download)", OK, f"last run {last or 'never'}; option minutes saved: {got or 'n/a'}", last_run_date=last, option_minutes=got)


def users_tile() -> dict:
    try:
        c = platform_db.connect()
        try:
            now = time.time()
            online = c.execute("SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL AND ? - last_seen_at < 120", (now,)).fetchone()[0]
            total = c.execute("SELECT COUNT(*) FROM users WHERE status='ACTIVE'").fetchone()[0]
            fails = c.execute("SELECT COUNT(*) FROM login_failures WHERE ts >= ?", (now - 3600,)).fetchone()[0]
        finally:
            c.close()
    except Exception as exc:
        return _tile("users", "Users and sessions", DOWN, f"cannot read sessions: {str(exc)[:100]}")
    st = DEGRADED if fails >= 20 else OK
    return _tile("users", "Users and sessions", st, f"{online} online, {total} active users, {fails} failed logins in the last hour", online=online, active_users=total, failed_logins_hour=fails)


def audit_tile() -> dict:
    try:
        c = platform_db.connect()
        try:
            ok, bad_id = audit.verify_chain(c)
        finally:
            c.close()
    except Exception as exc:
        return _tile("audit", "Audit log", DOWN, f"cannot verify: {str(exc)[:100]}")
    return _tile("audit", "Audit log", OK if ok else DOWN, "hash chain intact" if ok else f"CHAIN BROKEN at row {bad_id}: the log was tampered with", chain_ok=ok, broken_at=bad_id)


def selftests_tile() -> dict:
    r = selftests.last()
    if r["at"] is None:
        return _tile("selftests", "Known-answer self-tests", NA, "not run yet")
    return _tile("selftests", "Known-answer self-tests", OK if r["failed"] == 0 else DOWN, f"{r['passed']} passed, {r['failed']} failed", passed=r["passed"], failed=r["failed"], at=r["at"])


def functions_tile() -> dict:
    rows = stats.snapshot()
    bad = [r for r in rows if r["status"] == "FAULTY"]
    deg = [r for r in rows if r["status"] == "DEGRADED"]
    # Angel One refusing requests (403 / rate limit) is the broker's side: the engine keeps running on the stored candles, so it is DEGRADED, not DOWN
    if bad and all(r["name"].startswith("angelone.") for r in bad):
        return _tile("functions", "Tracked functions", DEGRADED, "Angel One is refusing requests (" + ", ".join(r["name"] for r in bad[:4]) + "); the engine uses the stored candles meanwhile",
                     faulty=len(bad), degraded=len(deg), total=len(rows))
    if bad:
        return _tile("functions", "Tracked functions", DOWN, "FAULTY: " + ", ".join(r["name"] for r in bad[:4]), faulty=len(bad), degraded=len(deg), total=len(rows))
    if deg:
        return _tile("functions", "Tracked functions", DEGRADED, "degraded: " + ", ".join(r["name"] for r in deg[:4]), faulty=0, degraded=len(deg), total=len(rows))
    return _tile("functions", "Tracked functions", OK, f"{len(rows)} functions, no errors above the limit", faulty=0, degraded=0, total=len(rows))


def incidents_tile() -> dict:
    total = incidents.count_since(3600)
    notes = incidents.count_since(3600, "info")       # informational records (e.g. a developer released a circuit by hand) are not problems
    n = total - notes
    text = f"{n} problem(s) in the last hour" + (f", {notes} note(s)" if notes else "")
    return _tile("incidents", "Incidents (last hour)", OK if n == 0 else DEGRADED if n < 20 else DOWN, text, count_hour=n)


def host_tile() -> dict:
    free = shutil.disk_usage(DATA_DIR if DATA_DIR.exists() else DATA_DIR.parent)
    info = {"disk_free_gb": round(free.free / 1e9, 2), "disk_total_gb": round(free.total / 1e9, 1)}
    try:
        import psutil  # optional
        info.update(cpu_pct=psutil.cpu_percent(interval=None), mem_pct=psutil.virtual_memory().percent)
    except Exception:
        pass
    pct_free = free.free / free.total * 100
    st = DOWN if pct_free < 3 else DEGRADED if pct_free < 10 or info.get("mem_pct", 0) > 92 else OK
    return _tile("host", "Host", st, f"disk {info['disk_free_gb']} GB free" + (f", CPU {info['cpu_pct']}%, memory {info['mem_pct']}%" if "cpu_pct" in info else ""), **info)


TILES = [api_tile, database_tile, feed_tile, angel_rest_tile, engine_tile, collector_tile, users_tile, audit_tile, selftests_tile, functions_tile, incidents_tile, host_tile]


def all_tiles() -> list[dict]:
    out = []
    for fn in TILES:
        try:
            out.append(fn())
        except Exception as exc:           # one broken check must never blank the whole board
            out.append(_tile(fn.__name__.replace("_tile", ""), fn.__name__.replace("_tile", "").title(), DOWN, f"the health check itself failed: {type(exc).__name__}: {str(exc)[:120]}"))
    return out


def overall(tiles: list[dict]) -> str:
    states = {t["status"] for t in tiles}
    return DOWN if DOWN in states else DEGRADED if DEGRADED in states else OK
