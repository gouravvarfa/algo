"""
Resolves NIFTY/BANKNIFTY/stock symbols to Angel One's exchange + instrument
token, via Angel One's own public instrument master file (no auth, no market
prices — just a symbol directory). Same real source URL as Angel One's
published SmartAPI docs.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from app.core.cache import cache

SCRIP_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
CACHE_KEY = "angelone:scrip_master:v3"  # v3: broadened to include NFO index futures for Top-Bottom Backtesting
CACHE_TTL_SECONDS = 24 * 3600

# NSE: equities ("") + indices ("AMXIDX"). NFO: stock futures ("FUTSTK") and
# index futures ("FUTIDX", e.g. NIFTY/BANKNIFTY) — options ("OPTSTK"/"OPTIDX")
# are deliberately excluded, this project never trades options.
RELEVANT_NSE_TYPES = {"", "AMXIDX"}
RELEVANT_NFO_TYPES = {"FUTSTK", "FUTIDX"}


@dataclass
class ScripMatch:
    token: str
    trading_symbol: str
    exch_seg: str


# The raw scrip master is ~35 MB of JSON (~150k rows) and parsing it peaks at
# roughly 200+ MB of Python objects. A scan runs several stocks concurrently and
# they all resolve their symbol on the very first call, before anything is
# cached — without single-flight every one of them would download + parse its
# own copy at the same moment (4x the peak), which exhausts a small (512 MB)
# host. So concurrent callers share ONE in-flight download.
_inflight: dict[int, "asyncio.Future[list[dict]]"] = {}


async def _download_and_filter() -> list[dict]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(SCRIP_MASTER_URL)
    response.raise_for_status()
    rows = response.json()
    del response  # drop the 35 MB raw body before building the filtered list

    filtered = [
        row
        for row in rows
        if (row.get("exch_seg") == "NSE" and row.get("instrumenttype") in RELEVANT_NSE_TYPES)
        or (row.get("exch_seg") == "NFO" and row.get("instrumenttype") in RELEVANT_NFO_TYPES)
    ]
    del rows  # the full ~150k-row list is no longer needed
    cache.set(CACHE_KEY, filtered, CACHE_TTL_SECONDS)
    return filtered


async def _fetch_scrip_master() -> list[dict]:
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached

    loop = asyncio.get_running_loop()
    key = id(loop)
    task = _inflight.get(key)
    if task is None or task.done():
        task = loop.create_task(_download_and_filter())
        _inflight[key] = task
    return await task


async def resolve_index(name: str) -> ScripMatch | None:
    """name e.g. 'NIFTY', 'BANKNIFTY' (Angel's index `name` field, not the tradingsymbol)."""
    rows = await _fetch_scrip_master()
    for row in rows:
        if row.get("instrumenttype") == "AMXIDX" and row.get("name") == name:
            return ScripMatch(token=row["token"], trading_symbol=row["symbol"], exch_seg="NSE")
    return None


# NSE cash-segment series, in preference order. "EQ" is the normal rolling
# segment; "BE"/"BZ" are the trade-to-trade/surveillance series — still real,
# fully tradable cash listings with the same historical-candle support, and
# the ONLY listing for stocks such as HEG, HFCL, STLTECH and INDIAGLYCO.
# Matching "-EQ" alone silently made ~240 NSE stocks unresolvable.
EQUITY_SERIES = ("EQ", "BE", "BZ")


async def resolve_equity(symbol: str) -> ScripMatch | None:
    """symbol e.g. 'RELIANCE' -> resolves its NSE cash listing (EQ, else BE/BZ)."""
    rows = await _fetch_scrip_master()
    by_series: dict[str, dict] = {}
    for row in rows:
        if row.get("instrumenttype") != "" or row.get("name") != symbol:
            continue
        series = str(row.get("symbol", "")).rpartition("-")[2]
        if series in EQUITY_SERIES:
            by_series.setdefault(series, row)

    for series in EQUITY_SERIES:
        row = by_series.get(series)
        if row is not None:
            return ScripMatch(token=row["token"], trading_symbol=row["symbol"], exch_seg="NSE")
    return None


async def resolve_stock_future(symbol: str) -> ScripMatch | None:
    """
    symbol e.g. 'RELIANCE' -> resolves the NEAREST-EXPIRY NFO stock future
    (FUTSTK) contract for that underlying. Returns None if the symbol has no
    listed futures contract (not every NIFTY 200 stock has one) — callers
    must treat that as "unavailable for this symbol", never fabricate one.
    """
    import datetime as dt

    rows = await _fetch_scrip_master()
    candidates = [
        row for row in rows
        if row.get("exch_seg") == "NFO" and row.get("instrumenttype") == "FUTSTK" and row.get("name") == symbol
    ]
    if not candidates:
        return None

    def _expiry_key(row: dict) -> dt.datetime:
        try:
            return dt.datetime.strptime(row.get("expiry", ""), "%d%b%Y")
        except ValueError:
            return dt.datetime.max  # unparsable expiry sorts last, never picked over a real one

    nearest = min(candidates, key=_expiry_key)
    return ScripMatch(token=nearest["token"], trading_symbol=nearest["symbol"], exch_seg="NFO")


def _parse_expiry(raw: str) -> "dt.datetime":
    import datetime as dt

    try:
        return dt.datetime.strptime(raw, "%d%b%Y")
    except ValueError:
        return dt.datetime.max


@dataclass
class FutureContract:
    underlying: str  # scrip master "name" — e.g. "RELIANCE", "NIFTY"
    trading_symbol: str  # e.g. "RELIANCE29SEP26FUT"
    token: str
    exch_seg: str  # always "NFO"
    expiry: str  # ISO date "2026-09-29"
    is_index: bool  # True for FUTIDX (NIFTY/BANKNIFTY/...), False for FUTSTK
    lot_size: int  # real exchange lot size, straight from Angel One's own scrip master — never guessed
    tick_size: float = 0.0  # price step in rupees from the master's tick_size column (paise there); 0.0 = the master did not say


def _tick_rupees(raw) -> float:
    """The master gives tick_size in paise ("5.000000" = Rs 0.05). Anything missing or unreadable is 0.0, never a guess."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if v <= 0:
        return 0.0
    return round(v / 100.0, 4) if v >= 1 else round(v, 4)


async def search_futures(query: str, limit: int = 25) -> list[FutureContract]:
    """
    Every live NFO futures contract (stock or index) whose underlying name
    starts with `query` (case-insensitive) — used by the Top-Bottom
    Backtesting module's futures search box. Deliberately NEVER returns
    equity/cash, ETF, mutual fund, or option contracts: this module only
    ever operates on FUTSTK/FUTIDX rows from Angel One's own live scrip
    master, so no symbol here can be anything but a real, currently listed
    futures contract.
    """
    import datetime as dt  # noqa: F401 (re-imported for _parse_expiry's annotation)

    q = query.strip().upper()
    if not q:
        return []

    rows = await _fetch_scrip_master()
    matches = [
        row
        for row in rows
        if row.get("exch_seg") == "NFO"
        and row.get("instrumenttype") in RELEVANT_NFO_TYPES
        and str(row.get("name", "")).upper().startswith(q)
    ]
    matches.sort(key=lambda r: (r["name"], _parse_expiry(r.get("expiry", ""))))
    return [
        FutureContract(
            underlying=row["name"],
            trading_symbol=row["symbol"],
            token=row["token"],
            exch_seg="NFO",
            expiry=_parse_expiry(row.get("expiry", "")).date().isoformat(),
            is_index=row.get("instrumenttype") == "FUTIDX",
            lot_size=int(float(row.get("lotsize") or 0)),
            tick_size=_tick_rupees(row.get("tick_size")),
        )
        for row in matches[:limit]
    ]


async def get_future_contracts(underlying: str) -> list[FutureContract]:
    """All live expiries for one underlying (e.g. all RELIANCE FUT contracts), nearest first."""
    return await search_futures(underlying, limit=50)


async def resolve_future_contract(underlying: str, expiry: str) -> FutureContract | None:
    """expiry: ISO date string ('2026-09-29') matching one of the live contracts' expiry exactly."""
    for c in await get_future_contracts(underlying):
        if c.underlying.upper() == underlying.strip().upper() and c.expiry == expiry:
            return c
    return None
