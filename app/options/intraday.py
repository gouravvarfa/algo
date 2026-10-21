"""Real 1-minute option data (Angel One historical candles) for intraday time travel in the Simulator.

What exists and what does not (stated, not hidden):
  * Angel One serves 1-minute candles only for contracts that are STILL LISTED in its instruments master. Expired
    contracts cannot be fetched, so intraday history starts when each live contract was listed. Older days stay EOD.
  * 1-minute candles carry OHLC + volume, NO open interest. OI shown in intraday mode is the previous trading day's
    NSE Bhavcopy OI and is labelled as such.
  * A minute with no trade has no candle. Nothing is filled: the chain shows the LAST REAL trade at or before the
    selected minute together with its age in minutes.
Storage: data/intraday/nifty_1m.db (separate file; the Bhavcopy DB is untouched).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
from pathlib import Path

from app.options import bhavcopy
from app.options.master import IST, UnderlyingInfo
from app.providers.angelone_client import AngelOneApiError, get_candle_data

DB = Path(__file__).resolve().parents[2] / "data" / "intraday" / "nifty_1m.db"
INDEX_KEY = ("", 0.0, "IDX")            # (expiry, strike, opt) used for the index rows
OPEN, CLOSE = "09:15", "15:30"
SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
  symbol TEXT NOT NULL, expiry TEXT NOT NULL, strike REAL NOT NULL, opt TEXT NOT NULL, ts TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume INTEGER NOT NULL,
  PRIMARY KEY (symbol, expiry, strike, opt, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS candles_day ON candles (symbol, ts);
CREATE TABLE IF NOT EXISTS fetch_log (
  symbol TEXT, expiry TEXT, strike REAL, opt TEXT, token TEXT, from_ts TEXT, to_ts TEXT,
  rows INTEGER, rejected INTEGER, status TEXT, note TEXT, fetched_at TEXT
);
"""


_READY: set[str] = set()


def connect(db: Path | None = None) -> sqlite3.Connection:
    db = db or DB
    db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db, timeout=30)
    key = str(Path(db).resolve())
    if key not in _READY:              # readers never write; WAL lets the app read while a fetch is writing
        try:
            c.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        c.executescript(SCHEMA)
        _READY.add(key)
    return c


def clean_rows(raw: list[list], day_filter=None) -> tuple[list[tuple], int]:
    """Angel rows [ts_iso, o, h, l, c, v] -> (ts 'YYYY-MM-DD HH:MM', o, h, l, c, v). Rejects (never repairs):
    unparsable rows, minutes outside 09:15-15:30 (the 15:30 candle is the real closing minute), non-positive prices,
    inconsistent OHLC."""
    out, bad = [], 0
    for r in raw:
        try:
            t = dt.datetime.fromisoformat(str(r[0])).astimezone(IST)
            o, h, l, c, v = float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(float(r[5] or 0))
        except (TypeError, ValueError, IndexError):
            bad += 1
            continue
        hm = t.strftime("%H:%M")
        if not (OPEN <= hm <= CLOSE) or min(o, h, l, c) <= 0 or not (l <= min(o, c) and max(o, c) <= h):
            bad += 1
            continue
        out.append((t.strftime("%Y-%m-%d %H:%M"), o, h, l, c, v))
    return out, bad


async def _fetch(session, exch: str, token: str, start: dt.datetime, end: dt.datetime, chunk_days: int = 28) -> list[list]:
    rows, a = [], start
    while a < end:
        b = min(a + dt.timedelta(days=chunk_days), end)
        rows += await get_candle_data(session, exch, token, "ONE_MINUTE", a.strftime("%Y-%m-%d %H:%M"), b.strftime("%Y-%m-%d %H:%M"))
        await asyncio.sleep(0.4)            # Angel One historical endpoint rate limit
        a = b
    return rows


async def ingest(session, info: UnderlyingInfo, days_back: int = 30, expiry_horizon_days: int = 45, pad_points: float = 300.0,
                 db: Path | None = None, log=print) -> dict:
    """Fetch the index and every listed option of `info` that expires within `expiry_horizon_days`, for strikes that the
    index actually came near during the window (index low/high +/- pad_points). Re-running is safe (upsert by minute)."""
    now = dt.datetime.now(IST).replace(tzinfo=None, second=0, microsecond=0)
    start = (now - dt.timedelta(days=days_back)).replace(hour=9, minute=15)
    c = connect(db)
    summary = {"index_rows": 0, "contracts": 0, "contracts_with_data": 0, "rows": 0, "rejected": 0, "errors": []}
    stamp = lambda: dt.datetime.now().isoformat(timespec="seconds")

    raw = await _fetch(session, "NSE", info.spot_token, start, now)
    rows, bad = clean_rows(raw)
    with c:
        c.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)", [(info.name, *INDEX_KEY, *r) for r in rows])
        c.execute("INSERT INTO fetch_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (info.name, "", 0, "IDX", info.spot_token, str(start), str(now), len(rows), bad, "ok", "", stamp()))
    summary["index_rows"], summary["rejected"] = len(rows), bad
    if not rows:
        raise RuntimeError("no index candles returned; cannot decide which strikes to fetch")
    lo, hi = min(r[3] for r in rows) - pad_points, max(r[2] for r in rows) + pad_points
    log(f"index: {len(rows)} minutes, range {lo + pad_points:.0f}-{hi - pad_points:.0f}")

    horizon = now.date() + dt.timedelta(days=expiry_horizon_days)
    todo = [k for k in info.contracts if now.date() <= k.expiry <= horizon and lo <= k.strike <= hi]
    summary["contracts"] = len(todo)
    log(f"contracts to fetch: {len(todo)} ({len({k.expiry for k in todo})} expiries)")
    for i, k in enumerate(todo, 1):
        try:
            raw = await _fetch(session, "NFO", k.token, start, now)
        except AngelOneApiError as exc:
            summary["errors"].append(f"{k.symbol}: {exc}")
            with c:
                c.execute("INSERT INTO fetch_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (info.name, k.expiry.isoformat(), k.strike, k.option_type, k.token, str(start), str(now), 0, 0, "error", str(exc)[:200], stamp()))
            continue
        rows, bad = clean_rows(raw)
        with c:
            c.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)",
                          [(info.name, k.expiry.isoformat(), k.strike, k.option_type, *r) for r in rows])
            c.execute("INSERT INTO fetch_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (info.name, k.expiry.isoformat(), k.strike, k.option_type, k.token, str(start), str(now), len(rows), bad, "ok", "", stamp()))
        summary["rows"] += len(rows)
        summary["rejected"] += bad
        summary["contracts_with_data"] += bool(rows)
        if i % 25 == 0:
            log(f"  {i}/{len(todo)} contracts, {summary['rows']} option minutes so far")
    return summary


# ---------------------------------------------------------------- reads
def days(c: sqlite3.Connection, symbol: str) -> list[dict]:
    """Trading days that have index minutes AND option minutes, with coverage counts."""
    idx = dict(c.execute("SELECT substr(ts,1,10), COUNT(*) FROM candles WHERE symbol=? AND opt='IDX' GROUP BY 1", (symbol,)).fetchall())
    opt = dict(c.execute("SELECT substr(ts,1,10), COUNT(DISTINCT expiry||strike||opt) FROM candles WHERE symbol=? AND opt!='IDX' GROUP BY 1", (symbol,)).fetchall())
    return [{"date": d, "index_minutes": idx[d], "contracts": opt.get(d, 0)} for d in sorted(idx) if opt.get(d)]


def minutes(c: sqlite3.Connection, symbol: str, day: str) -> list[str]:
    return [r[0][11:] for r in c.execute("SELECT ts FROM candles WHERE symbol=? AND opt='IDX' AND ts BETWEEN ? AND ? ORDER BY ts",
                                          (symbol, day + " 00:00", day + " 23:59"))]


def spot_at(c: sqlite3.Connection, symbol: str, day: str, hm: str) -> tuple[float, str] | None:
    r = c.execute("SELECT close, ts FROM candles WHERE symbol=? AND opt='IDX' AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 1",
                  (symbol, day + " 00:00", f"{day} {hm}")).fetchone()
    return (r[0], r[1][11:]) if r else None


def chain_at(c: sqlite3.Connection, symbol: str, day: str, hm: str, expiry: str) -> dict:
    """{(strike, opt): {ltp, ts, volume_to_now, day_high, day_low}} using only candles at or before the minute."""
    out = {}
    q = """SELECT strike, opt, ts, close, volume, high, low FROM candles
           WHERE symbol=? AND expiry=? AND opt!='IDX' AND ts BETWEEN ? AND ? ORDER BY ts"""
    for k, o, ts, cl, vol, h, l in c.execute(q, (symbol, expiry, day + " 00:00", f"{day} {hm}")):
        r = out.setdefault((k, o), {"volume": 0, "high": h, "low": l})
        r.update(ltp=cl, ts=ts[11:])
        r["volume"] += vol
        r["high"], r["low"] = max(r["high"], h), min(r["low"], l)
    return out


def expiries_on(c: sqlite3.Connection, symbol: str, day: str) -> list[str]:
    return [r[0] for r in c.execute("SELECT DISTINCT expiry FROM candles WHERE symbol=? AND opt!='IDX' AND ts BETWEEN ? AND ? AND expiry>=? ORDER BY 1",
                                    (symbol, day + " 00:00", day + " 23:59", day))]


def price_at(c: sqlite3.Connection, symbol: str, expiry: str, strike: float, opt: str, day: str, hm: str) -> tuple[float, str] | None:
    r = c.execute("SELECT close, ts FROM candles WHERE symbol=? AND expiry=? AND strike=? AND opt=? AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 1",
                  (symbol, expiry, strike, opt, day + " 00:00", f"{day} {hm}")).fetchone()
    return (r[0], r[1][11:]) if r else None


def crosscheck(c: sqlite3.Connection, symbol: str, bhav_db: Path = bhavcopy.DB) -> dict:
    """Compare each contract-day's LAST 1-minute close with the NSE Bhavcopy close for that day (reported, never 'fixed')."""
    b = sqlite3.connect(bhav_db)
    rows = c.execute("""SELECT expiry, strike, opt, substr(ts,1,10) d, close FROM candles c1 WHERE symbol=? AND opt!='IDX'
                        AND ts = (SELECT MAX(ts) FROM candles c2 WHERE c2.symbol=c1.symbol AND c2.expiry=c1.expiry AND c2.strike=c1.strike
                                  AND c2.opt=c1.opt AND substr(c2.ts,1,10)=substr(c1.ts,1,10))""", (symbol,)).fetchall()
    n = same = 0
    worst = []
    for e, k, o, d, cl in rows:
        r = b.execute("SELECT close FROM options_eod WHERE symbol=? AND trade_date=? AND expiry=? AND strike=? AND opt=?", (symbol, d, e, k, o)).fetchone()
        if not r:
            continue
        n += 1
        diff = abs(r[0] - cl)
        same += diff <= max(0.05, 0.01 * r[0])
        worst.append((diff, d, e, k, o, cl, r[0]))
    worst.sort(reverse=True)
    return {"compared": n, "within_1pct": same, "worst": worst[:5]}
