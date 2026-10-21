"""
NSE F&O Bhavcopy (UDiFF format, end-of-day) -> local SQLite store for index options backtesting.

Real data only. Source: https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip
  - raw zips are cached in data/bhavcopy/raw/ and never downloaded twice
  - a day with no file (weekend, holiday, or not published yet) is recorded as "no file" - NOT as a trading day,
    and nothing is filled in for it
  - the lot size of EVERY day comes from that day's own file (NewBrdLotQty), so lot-size changes are tracked by date
Limitation (stated, not hidden): this is END-OF-DAY data (open/high/low/close/settle per contract per day). Intraday
stop-losses and entry/exit times can only be approximated from it.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import sqlite3
import time
import zipfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2] / "data" / "bhavcopy"
RAW = ROOT / "raw"
DB = ROOT / "fo_eod.db"
URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Accept": "*/*", "Referer": "https://www.nseindia.com/"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS options_eod (
  trade_date TEXT NOT NULL, symbol TEXT NOT NULL, expiry TEXT NOT NULL, strike REAL NOT NULL, opt TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, settle REAL, underlying REAL,
  oi INTEGER, change_oi INTEGER, volume INTEGER, lot_size INTEGER NOT NULL, contract TEXT,
  PRIMARY KEY (trade_date, symbol, expiry, strike, opt)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS load_log (
  trade_date TEXT PRIMARY KEY, status TEXT NOT NULL, rows INTEGER NOT NULL, rejected INTEGER NOT NULL, note TEXT, loaded_at TEXT NOT NULL
);
"""


_READY: set[str] = set()


def _conn(db: Path = DB) -> sqlite3.Connection:
    """Readers never write: the schema is created once per process, and the file uses WAL so the app can keep
    reading while a loader is writing (previously a running load made every page fail with 'database is locked')."""
    db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db, timeout=30)
    key = str(Path(db).resolve())
    if key not in _READY:
        try:
            c.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass                      # a loader holds the file right now; it stays in its current mode until next start
        c.executescript(SCHEMA)
        _READY.add(key)
    return c


def fetch_zip(day: dt.date, client: httpx.Client) -> bytes | None:
    """Cached raw file, or None when NSE has no file for that day (HTTP 404)."""
    RAW.mkdir(parents=True, exist_ok=True)
    path = RAW / f"fo_{day:%Y%m%d}.zip"
    if path.exists():
        return path.read_bytes()
    r = client.get(URL.format(d=day))
    if r.status_code == 404:
        return None
    if r.status_code != 200 or r.content[:2] != b"PK":
        raise RuntimeError(f"{day}: NSE returned HTTP {r.status_code} ({r.headers.get('content-type')}) - not stored, try again later")
    path.write_bytes(r.content)
    return r.content


def _f(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse(day: dt.date, blob: bytes, symbols: set[str]) -> tuple[list[tuple], list[str]]:
    """Index-option rows (FinInstrmTp=IDO) for `symbols`, validated. Returns (rows, rejection reasons)."""
    z = zipfile.ZipFile(io.BytesIO(blob))
    text = z.read(z.namelist()[0]).decode("utf-8")
    good, bad, seen = [], [], set()
    for r in csv.DictReader(io.StringIO(text)):
        if r["FinInstrmTp"] != "IDO" or r["TckrSymb"] not in symbols:
            continue
        key = (r["TckrSymb"], r["XpryDt"], r["StrkPric"], r["OptnTp"])
        o, h, l, c, s = (_f(r[k]) for k in ("OpnPric", "HghPric", "LwPric", "ClsPric", "SttlmPric"))
        why = None
        if r["TradDt"] != day.isoformat():
            why = f"trade date {r['TradDt']} != file date"
        elif r["OptnTp"] not in ("CE", "PE") or _f(r["StrkPric"]) is None or not r["XpryDt"]:
            why = "bad contract fields"
        elif r["XpryDt"] < r["TradDt"]:
            why = "expiry before trade date"
        elif key in seen:
            why = "duplicate contract"
        elif c is None or c < 0 or s is None or s < 0:
            why = "missing/negative close or settle"
        elif (o or 0) > 0 and not (l <= o <= h and l <= c <= h):   # untraded contracts have O/H/L = 0 and only a close
            why = "inconsistent OHLC"
        elif not r["NewBrdLotQty"].isdigit() or int(r["NewBrdLotQty"]) <= 0:
            why = "missing lot size"
        if why:
            bad.append(f"{r['FinInstrmNm']}: {why}")
            continue
        seen.add(key)
        good.append((r["TradDt"], r["TckrSymb"], r["XpryDt"], float(r["StrkPric"]), r["OptnTp"], o, h, l, c, s, _f(r["UndrlygPric"]),
                     int(float(r["OpnIntrst"] or 0)), int(float(r["ChngInOpnIntrst"] or 0)), int(float(r["TtlTradgVol"] or 0)),
                     int(r["NewBrdLotQty"]), r["FinInstrmNm"]))
    return good, bad


def load_range(start: dt.date, end: dt.date, symbols=("BANKNIFTY", "NIFTY"), delay: float = 1.0, db: Path = DB, log=print) -> dict:
    syms = {s.upper() for s in symbols}
    summary = {"loaded_days": 0, "no_file_days": [], "errors": [], "rows": 0, "rejected": 0}
    conn = _conn(db)
    with httpx.Client(headers=HEADERS, timeout=60, follow_redirects=True) as client:
        d = start
        while d <= end:
            weekend = d.weekday() >= 5   # normally closed, but special sessions happen (e.g. Budget Sunday 1 Feb 2026)
            try:
                cached = (RAW / f"fo_{d:%Y%m%d}.zip").exists()
                blob = fetch_zip(d, client)
                if not cached:
                    time.sleep(delay)
                if weekend and blob is None:
                    d += dt.timedelta(days=1)
                    continue
            except (RuntimeError, httpx.HTTPError) as exc:
                summary["errors"].append(str(exc))
                log(f"  {d}: ERROR {exc}")
                d += dt.timedelta(days=1)
                continue
            now = dt.datetime.now().isoformat(timespec="seconds")
            if blob is None:
                with conn:
                    conn.execute("INSERT OR REPLACE INTO load_log VALUES (?,?,?,?,?,?)", (d.isoformat(), "no file", 0, 0, "holiday or not published", now))
                summary["no_file_days"].append(d.isoformat())
                log(f"  {d}: no file (holiday or not published)")
            else:
                rows, bad = parse(d, blob, syms)
                with conn:
                    conn.execute("DELETE FROM options_eod WHERE trade_date=? AND symbol IN (%s)" % ",".join("?" * len(syms)), (d.isoformat(), *syms))
                    conn.executemany("INSERT INTO options_eod VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                    conn.execute("INSERT OR REPLACE INTO load_log VALUES (?,?,?,?,?,?)", (d.isoformat(), "loaded", len(rows), len(bad), "; ".join(bad[:5]), now))
                summary["loaded_days"] += 1
                summary["rows"] += len(rows)
                summary["rejected"] += len(bad)
                log(f"  {d}: {len(rows)} rows" + (f", {len(bad)} rejected" if bad else ""))
            d += dt.timedelta(days=1)
    conn.close()
    return summary


def chain_on(day: dt.date, symbol: str, expiry: str, db: Path = DB) -> list[tuple]:
    with _conn(db) as c:
        return c.execute("SELECT strike, opt, close, settle, oi, volume, lot_size, underlying FROM options_eod WHERE trade_date=? AND symbol=? AND expiry=? ORDER BY strike, opt",
                         (day.isoformat(), symbol, expiry)).fetchall()
