"""
Upstox's public instrument list (no login, no prices): which symbols exist and the `instrument_key` Upstox wants for each, e.g.
NSE_INDEX|Nifty Bank, NSE_EQ|INE002A01018 (RELIANCE), NSE_FO|48699 (BANKNIFTY futures 27 OCT 26).

Only what the Upstox charts page needs is kept: every NSE index, the NSE cash stocks (series EQ) and the live NSE futures. The ~2 MB download is parsed
once, cut down to about 3,500 rows and saved to data/upstox_instruments.json for the day, so a restart does not download it again.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import json
import time
from pathlib import Path

import httpx

URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
CACHE_PATH = Path("data/upstox_instruments.json")
FRESH_FOR_S = 12 * 3600
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ALIASES = {"BANKNIFTY": "NIFTY BANK", "NIFTY": "NIFTY 50"}        # the names people type, for the two indexes everyone looks at
KIND_ORDER = {"INDEX": 0, "EQ": 1, "FUT": 2}

_mem: tuple[float, list[dict]] | None = None
_lock = asyncio.Lock()


class InstrumentsUnavailable(Exception):
    pass


def normalise(rows: list[dict], today: dt.date | None = None) -> list[dict]:
    """Cut the full list down to indexes, cash stocks and live futures, each as {key, symbol, name, kind, lot, expiry, underlying}."""
    today = today or dt.datetime.now(IST).date()
    out: list[dict] = []
    for r in rows:
        seg, typ, key = r.get("segment"), r.get("instrument_type"), r.get("instrument_key")
        if not key:
            continue
        if seg == "NSE_INDEX":
            out.append({"key": key, "symbol": r.get("trading_symbol") or r.get("name") or "", "name": r.get("name") or "", "kind": "INDEX", "lot": None, "expiry": None, "underlying": None})
        elif seg == "NSE_EQ" and typ == "EQ":
            out.append({"key": key, "symbol": r.get("trading_symbol") or "", "name": r.get("short_name") or r.get("name") or "", "alt": r.get("name") or "", "kind": "EQ", "lot": None, "expiry": None, "underlying": None})
        elif seg == "NSE_FO" and typ == "FUT":
            ms = r.get("expiry")
            try:
                expiry = dt.datetime.fromtimestamp(float(ms) / 1000, IST).date()
            except (TypeError, ValueError, OverflowError, OSError):
                continue
            if expiry < today:
                continue
            out.append({"key": key, "symbol": r.get("trading_symbol") or "", "name": r.get("name") or "", "kind": "FUT", "lot": r.get("lot_size"), "expiry": expiry.isoformat(),
                        "underlying": r.get("underlying_symbol") or r.get("name") or ""})
    return out


def _read_disk() -> tuple[float, list[dict]] | None:
    try:
        return CACHE_PATH.stat().st_mtime, json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def _download() -> list[dict]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(URL)
    response.raise_for_status()
    return await asyncio.to_thread(lambda: normalise(json.loads(gzip.decompress(response.content))))


async def load() -> list[dict]:
    global _mem
    if _mem and time.time() - _mem[0] < FRESH_FOR_S:
        return _mem[1]
    async with _lock:
        if _mem and time.time() - _mem[0] < FRESH_FOR_S:
            return _mem[1]
        disk = _read_disk()
        if disk and time.time() - disk[0] < FRESH_FOR_S:
            _mem = disk
            return disk[1]
        try:
            rows = await _download()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            stale = disk or _mem
            if stale:                                    # an old list is far better than none: symbols hardly change from one day to the next
                _mem = (time.time(), stale[1])
                return stale[1]
            raise InstrumentsUnavailable(f"Could not load Upstox's instrument list: {exc}") from exc
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(json.dumps(rows), encoding="utf-8")
        except OSError:
            pass                                         # only a cache
        _mem = (time.time(), rows)
        return rows


def search(rows: list[dict], query: str, limit: int = 25) -> list[dict]:
    """Best matches first: exact symbol, then symbol starts with, then a word in the name / the futures' underlying contains it. Indexes before
    stocks before futures; futures nearest expiry first."""
    q = query.strip().upper()
    if not q:
        return []
    exact = {q, ALIASES.get(q, q)}                      # 'BANKNIFTY' is also the index called 'NIFTY BANK'
    scored: list[tuple[int, int, str, dict]] = []
    for r in rows:
        sym, name, under = r["symbol"].upper(), (r["name"] + " " + r.get("alt", "")).upper(), (r.get("underlying") or "").upper()
        if sym in exact or under == q:
            rank = 0
        elif sym.startswith(q) or under.startswith(q):
            rank = 1
        elif q in sym or q in name:
            rank = 2
        else:
            continue
        scored.append((rank, KIND_ORDER[r["kind"]], r.get("expiry") or "", r))
    scored.sort(key=lambda t: (t[0], t[1], t[2], t[3]["symbol"]))
    return [t[3] for t in scored[:limit]]
