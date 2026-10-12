"""
SQLite persistence for the BankNifty INDEX replay: candles per (instrument,
interval), data-source tracking and replay session state. Index data lives
only here, keyed by explicit instrument type, and the store refuses anything
that is not the BankNifty index, so futures candles can never be mixed in.

Deployment note: on an ephemeral disk (e.g. Render without a persistent
disk) this file is lost on restart. Point REPLAY_DB_PATH at a persistent volume.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading
from pathlib import Path

from app.replay.csv_import import incomplete_days
from app.replay.timeframes import INTERVALS

_DEFAULT = Path(__file__).resolve().parents[2] / "data" / "replay" / "replay_index.db"

# The only instrument this module serves.
INDEX_INSTRUMENTS = {"BANKNIFTY": {"instrument_type": "INDEX", "token": "99926009", "exch_seg": "NSE", "name": "Nifty Bank"}}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL, interval TEXT NOT NULL, ts TEXT NOT NULL,
    o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL, v REAL NOT NULL,
    PRIMARY KEY (symbol, interval, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS datasets (
    symbol TEXT NOT NULL, interval TEXT NOT NULL, instrument_type TEXT NOT NULL,
    sources TEXT NOT NULL, updated_at TEXT NOT NULL, last_report TEXT NOT NULL,
    PRIMARY KEY (symbol, interval)
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, config TEXT NOT NULL, position INTEGER NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""
ISO = "%Y-%m-%dT%H:%M:%S"


class InstrumentError(ValueError):
    pass


class ReplayStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.environ.get("REPLAY_DB_PATH") or _DEFAULT)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    @staticmethod
    def _check(symbol: str, interval: str) -> None:
        if symbol not in INDEX_INSTRUMENTS or "FUT" in symbol:
            raise InstrumentError(f"{symbol} is not a supported index instrument")
        if interval not in INTERVALS:
            raise InstrumentError(f"unsupported interval {interval}")

    def add_candles(self, symbol: str, interval: str, rows: list[tuple], source: str, report: dict) -> None:
        """Merge rows (INSERT OR REPLACE by timestamp, so ranges accumulate and
        re-downloads are idempotent). `source` is IMPORT or ANGELONE."""
        self._check(symbol, interval)
        with self._lock, self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)",
                [(symbol, interval, r[0].strftime(ISO), *r[1:]) for r in rows],
            )
            old = c.execute("SELECT sources FROM datasets WHERE symbol=? AND interval=?", (symbol, interval)).fetchone()
            sources = sorted(set(json.loads(old[0])) | {source}) if old else [source]
            c.execute(
                "INSERT OR REPLACE INTO datasets VALUES (?,?,?,?,?,?)",
                (symbol, interval, INDEX_INSTRUMENTS[symbol]["instrument_type"], json.dumps(sources),
                 dt.datetime.now().strftime(ISO), json.dumps(report)),
            )

    def coverage(self) -> list[dict]:
        """Per (symbol, interval): range, count, sources and incomplete days,
        computed from what is actually stored."""
        out = []
        with self._conn() as c:
            sets = c.execute("SELECT symbol,interval,instrument_type,sources,updated_at FROM datasets ORDER BY interval").fetchall()
            for sym, iv, typ, src, upd in sets:
                first, last, n = c.execute(
                    "SELECT MIN(ts),MAX(ts),COUNT(*) FROM candles WHERE symbol=? AND interval=?", (sym, iv)).fetchone()
                per = INTERVALS[iv][1]
                incomplete = None
                if per is not None:
                    full = len(range(9 * 60 + 15, 15 * 60 + 30, per))
                    rows = c.execute(
                        "SELECT substr(ts,1,10), COUNT(*) FROM candles WHERE symbol=? AND interval=? GROUP BY 1 HAVING COUNT(*)<?",
                        (sym, iv, full)).fetchall()
                    incomplete = [{"date": d, "candles": k, "expected": full} for d, k in rows[:200]]
                out.append({
                    "symbol": sym, "instrument_type": typ, "interval": iv, "sources": json.loads(src),
                    "first_ts": first, "last_ts": last, "candles": n, "updated_at": upd,
                    "incomplete_days": len(incomplete) if incomplete is not None else None,
                    "incomplete_days_detail": incomplete,
                })
        return out

    def intervals_available(self, symbol: str) -> list[str]:
        return [d["interval"] for d in self.coverage() if d["symbol"] == symbol and d["candles"]]

    def count(self, symbol: str, interval: str, start: dt.datetime | None = None, end: dt.datetime | None = None) -> int:
        q, args = "SELECT COUNT(*) FROM candles WHERE symbol=? AND interval=?", [symbol, interval]
        if start:
            q += " AND ts>=?"
            args.append(start.strftime(ISO))
        if end:
            q += " AND ts<=?"
            args.append(end.strftime(ISO))
        with self._conn() as c:
            return c.execute(q, args).fetchone()[0]

    def distinct_days(self, symbol: str, interval: str) -> list[str]:
        self._check(symbol, interval)
        with self._conn() as c:
            rows = c.execute("SELECT DISTINCT substr(ts,1,10) FROM candles WHERE symbol=? AND interval=? ORDER BY 1", (symbol, interval)).fetchall()
        return [r[0] for r in rows]

    def day_stats(self, symbol: str, interval: str, start: dt.datetime, end: dt.datetime) -> list[tuple]:
        """(day, candle count, first ts, last ts, candles with inconsistent OHLC) per calendar day."""
        self._check(symbol, interval)
        with self._conn() as c:
            return c.execute(
                "SELECT substr(ts,1,10), COUNT(*), MIN(ts), MAX(ts), "
                "SUM(CASE WHEN h < o OR h < c OR l > o OR l > c OR l > h OR o <= 0 OR h <= 0 OR l <= 0 OR c <= 0 THEN 1 ELSE 0 END) "
                "FROM candles WHERE symbol=? AND interval=? AND ts>=? AND ts<=? GROUP BY 1 ORDER BY 1",
                (symbol, interval, start.strftime(ISO), end.strftime(ISO))).fetchall()

    def candles(self, symbol: str, interval: str, start: dt.datetime | None = None, end: dt.datetime | None = None) -> list[tuple]:
        self._check(symbol, interval)
        q, args = "SELECT ts,o,h,l,c,v FROM candles WHERE symbol=? AND interval=?", [symbol, interval]
        if start:
            q += " AND ts>=?"
            args.append(start.strftime(ISO))
        if end:
            q += " AND ts<=?"
            args.append(end.strftime(ISO))
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY ts", args).fetchall()
        return [(dt.datetime.strptime(r[0], ISO), *r[1:]) for r in rows]

    def save_session(self, sid: str, config: dict, position: int) -> None:
        now = dt.datetime.now().strftime(ISO)
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "config=excluded.config, position=excluded.position, updated_at=excluded.updated_at",
                (sid, json.dumps(config), position, now, now),
            )

    def load_session(self, sid: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT config,position FROM sessions WHERE id=?", (sid,)).fetchone()
        return {"config": json.loads(row[0]), "position": row[1]} if row else None

    def latest_session_id(self) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT id FROM sessions ORDER BY updated_at DESC LIMIT 1").fetchone()
        return row[0] if row else None


__all__ = ["ReplayStore", "InstrumentError", "INDEX_INSTRUMENTS", "incomplete_days"]
