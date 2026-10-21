"""Local store of every candle the live engine has seen (data/paper_trading/candles.db).

Why: Angel One serves only the last ~25 days of 1-minute candles, and a futures contract disappears from its master when it expires.
Keeping what we have already fetched lets the engine re-evaluate a whole multi-contract history (Oct leg -> Nov leg -> Dec leg) later,
exactly as it was seen. Only REAL candles from Angel One are stored; a stored candle is overwritten by a later fetch of the same bar.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pandas as pd

DB = Path("data/paper_trading/candles.db")
SCHEMA = """CREATE TABLE IF NOT EXISTS candles (
  token TEXT NOT NULL, interval TEXT NOT NULL, ts TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL,
  PRIMARY KEY (token, interval, ts)) WITHOUT ROWID"""
_READY: set[str] = set()


def _conn(db: Path | None = None) -> sqlite3.Connection:
    db = Path(db or DB)
    db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db, timeout=30)
    key = str(db.resolve())
    if key not in _READY:
        try:
            c.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        c.execute(SCHEMA)
        _READY.add(key)
    return c


def put(token: str, interval: str, df: pd.DataFrame, db: Path | None = None) -> int:
    if df is None or df.empty:
        return 0
    rows = [(str(token), interval, ts.strftime("%Y-%m-%d %H:%M:%S"), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume) if "volume" in df else 0.0)
            for ts, r in df.iterrows() if min(r.open, r.high, r.low, r.close) > 0]
    with _conn(db) as c:
        c.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def get(token: str, interval: str, since: dt.datetime | None = None, db: Path | None = None) -> pd.DataFrame:
    q, a = "SELECT ts, open, high, low, close, volume FROM candles WHERE token=? AND interval=?", [str(token), interval]
    if since is not None:
        q += " AND ts >= ?"
        a.append(since.strftime("%Y-%m-%d %H:%M:%S"))
    with _conn(db) as c:
        rows = c.execute(q + " ORDER BY ts", a).fetchall()
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    if df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")[["open", "high", "low", "close", "volume"]].astype(float)


def merge(stored: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    """Union by timestamp; the fresh fetch wins for bars present in both."""
    if stored.empty:
        return fresh
    if fresh.empty:
        return stored
    out = pd.concat([stored[~stored.index.isin(fresh.index)], fresh]).sort_index()
    return out
