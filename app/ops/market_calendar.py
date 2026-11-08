"""Exchange holidays and special sessions (Muhurat, Budget Sunday), kept by an admin.

  holiday  -> the market is CLOSED that day, even on a weekday: no strategy evaluation as 'open', no real order.
  special  -> a short extra session (often on a weekend). The strategy and real orders stay OFF in it (a one-hour, thin session is not a normal
              trading day for a daily-candle strategy); everybody sees a banner and the top bar says so.

Read on every market-hours question, so it is cached for a minute and never raises: a missing table or a busy database means 'no entry'.
"""
from __future__ import annotations

import datetime as dt
import time

from app.db import platform_db

KINDS = ("holiday", "special")
EXCHANGES = ("NSE", "MCX")
_CACHE_S = 60.0
_cache: dict | None = None
_cache_at = 0.0


def _load() -> dict:
    global _cache, _cache_at
    if _cache is not None and time.time() - _cache_at < _CACHE_S:
        return _cache
    data: dict = {}
    try:
        c = platform_db.connect()
        try:
            for r in c.execute("SELECT date, exchange, kind, start_time, end_time, note FROM market_calendar"):
                data[(r["date"], r["exchange"])] = dict(r)
        finally:
            c.close()
    except Exception:
        if _cache is not None:
            return _cache
    _cache, _cache_at = data, time.time()
    return data


def invalidate() -> None:
    global _cache
    _cache = None


def entry(day: dt.date, exchange: str = "NSE") -> dict | None:
    return _load().get((day.isoformat(), exchange))


def today_entries(now: dt.datetime) -> list[dict]:
    d = now.date().isoformat()
    return [v for (day, _), v in _load().items() if day == d]


def upcoming(c, days: int = 90) -> list[dict]:
    today = dt.date.today().isoformat()
    return [dict(r) for r in c.execute("SELECT date, exchange, kind, start_time, end_time, note, created_by, created_at FROM market_calendar WHERE date >= ? "
                                       "ORDER BY date, exchange LIMIT 200", ((dt.date.today() - dt.timedelta(days=7)).isoformat(),))
            if r["date"] <= (dt.date.today() + dt.timedelta(days=days)).isoformat() or r["date"] >= today]


def put(c, day: str, exchange: str, kind: str, start: str | None, end: str | None, note: str, actor: str) -> None:
    dt.date.fromisoformat(day)
    if exchange not in EXCHANGES or kind not in KINDS:
        raise ValueError("exchange must be NSE or MCX; kind holiday or special")
    if kind == "special":
        if not (start and end):
            raise ValueError("a special session needs a start and an end time")
        if dt.time.fromisoformat(start) >= dt.time.fromisoformat(end):
            raise ValueError("the session must end after it starts")
    c.execute("INSERT INTO market_calendar (date, exchange, kind, start_time, end_time, note, created_by, created_at) VALUES (?,?,?,?,?,?,?,?) "
              "ON CONFLICT(date, exchange) DO UPDATE SET kind=excluded.kind, start_time=excluded.start_time, end_time=excluded.end_time, note=excluded.note, "
              "created_by=excluded.created_by, created_at=excluded.created_at",
              (day, exchange, kind, start if kind == "special" else None, end if kind == "special" else None, note.strip()[:200], actor, time.time()))
    c.commit()
    invalidate()


def delete(c, day: str, exchange: str) -> bool:
    n = c.execute("DELETE FROM market_calendar WHERE date=? AND exchange=?", (day, exchange)).rowcount
    c.commit()
    invalidate()
    return bool(n)


# ---- automatic: NSE's own trading-holiday list ---------------------------------------------------------------------------------------------
NSE_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
AUTO_BY = "NSE (auto)"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*", "Referer": "https://www.nseindia.com/resources/exchange-communication-holidays"}
LAST_SYNC: dict = {"at": None, "added": 0, "error": None}


def parse_nse(data: dict) -> list[tuple[str, str]]:
    """(date, name) of every F&O trading holiday in NSE's answer. A '*' in NSE's list marks a day with a Muhurat session."""
    out = []
    for x in data.get("FO") or []:
        try:
            d = dt.datetime.strptime(x["tradingDate"], "%d-%b-%Y").date().isoformat()
        except (KeyError, ValueError):
            continue
        name = (x.get("description") or "Exchange holiday").strip()
        if name.endswith("*"):
            name = name.rstrip("* ") + " (Muhurat trading session)"
        out.append((d, name))
    return out


def apply_nse(c, days: list[tuple[str, str]]) -> int:
    """Adds NSE holidays; a day an admin entered by hand is never overwritten."""
    n = 0
    for d, name in days:
        r = c.execute("SELECT created_by FROM market_calendar WHERE date=? AND exchange='NSE'", (d,)).fetchone()
        if r is not None and r["created_by"] != AUTO_BY:
            continue
        c.execute("INSERT INTO market_calendar (date, exchange, kind, start_time, end_time, note, created_by, created_at) VALUES (?, 'NSE', 'holiday', NULL, NULL, ?, ?, ?) "
                  "ON CONFLICT(date, exchange) DO UPDATE SET note=excluded.note", (d, name, AUTO_BY, time.time()))
        n += 1
    c.commit()
    invalidate()
    return n


async def sync_nse() -> dict:
    import httpx
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=20.0, follow_redirects=True) as cl:
            r = await cl.get(NSE_URL)
            r.raise_for_status()
            days = parse_nse(r.json())
        if not days:
            raise ValueError("NSE answered without any F&O holiday")
        c = platform_db.connect()
        try:
            n = apply_nse(c, days)
        finally:
            c.close()
        LAST_SYNC.update(at=time.time(), added=n, error=None)
    except Exception as exc:
        LAST_SYNC.update(at=time.time(), error=f"{type(exc).__name__}: {str(exc)[:120]}")
    return dict(LAST_SYNC)


async def loop() -> None:
    """At start, then every 12 hours: NSE's list changes rarely, but a new year's list appears in December."""
    import asyncio
    while True:
        await sync_nse()
        await asyncio.sleep(12 * 3600)
