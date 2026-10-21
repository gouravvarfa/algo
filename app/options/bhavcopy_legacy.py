"""NSE F&O Bhavcopy in the OLD (pre-UDiFF) format, 2001 -> 5 Jul 2024, into the SAME options_eod table.

Source files (real NSE data, cached under data/bhavcopy/raw_legacy/):
  options/futures : content/historical/DERIVATIVES/YYYY/MON/foDDMONYYYYbhav.csv.zip
  index close     : content/indices/ind_close_all_DDMMYYYY.csv
The old format has no lot size and no underlying value, so:
  * underlying = the index's official closing value from NSE's daily index file (never the futures price). NSE's
    archive has no such file before mid-2013; for those days the index's daily close from Angel One's historical
    candles is used (table index_close, source='angelone'), after the two sources were checked to agree on the
    overlap years. A day with neither is NOT loaded (recorded in load_log with the reason)
  * lot size   = DERIVED from that day's own turnover: futures VAL_INLAKH*1e5 / (CONTRACTS * typical price), checked
    across every traded futures month; it must agree (within 1.5%) on one multiple of 5, else the day is NOT loaded.
    Every legacy day is labelled in load_log ("legacy ... lot derived") so results using it can say so.
Days already loaded from the UDiFF files are never overwritten.
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

from app.options import bhavcopy

RAW = bhavcopy.ROOT / "raw_legacy"
FO_URL = "https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{y}/{mon}/fo{d:%d}{mon}{y}bhav.csv.zip"
IX_URL = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{d:%d%m%Y}.csv"
UDIFF_START = dt.date(2024, 7, 8)
INDEX_NAMES = {
    "NIFTY": {"nifty 50", "cnx nifty", "s&p cnx nifty", "nifty"},
    "BANKNIFTY": {"nifty bank", "cnx bank", "cnx bank index", "bank nifty", "nifty bank index"},
}


def _get(client: httpx.Client, url: str, path: Path, binary: bool) -> bytes | None:
    if path.exists():
        return path.read_bytes()
    r = client.get(url)
    if r.status_code == 404:
        return None
    if r.status_code != 200 or (binary and r.content[:2] != b"PK") or (not binary and not r.content[:5].lower().startswith(b"index")):
        raise RuntimeError(f"{url}: HTTP {r.status_code} ({r.headers.get('content-type')})")
    path.write_bytes(r.content)
    return r.content


def index_closes(text: str) -> dict[str, float]:
    out = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 6:
            continue
        name = row[0].strip().lower()
        for sym, names in INDEX_NAMES.items():
            if name in names:
                try:
                    out[sym] = float(row[5])
                except ValueError:
                    pass
    return out


def derive_lot(fut_rows: list[dict]) -> tuple[int | None, str]:
    """Lot size from futures turnover. Returns (lot, note). Requires every traded month to agree on one multiple of 5."""
    cands = []
    for r in fut_rows:
        try:
            n, val = float(r["CONTRACTS"]), float(r["VAL_INLAKH"]) * 1e5
            px = (float(r["OPEN"]) + float(r["HIGH"]) + float(r["LOW"]) + float(r["CLOSE"])) / 4
        except (KeyError, ValueError):
            continue
        if n >= 10 and px > 0 and val > 0:
            cands.append(val / (n * px))
    if not cands:
        return None, "no traded futures to derive the lot size"
    lot = int(round(cands[0] / 5.0) * 5) or None
    if not lot or any(abs(c - lot) / lot > 0.015 for c in cands):
        return None, f"futures turnover does not give one consistent lot size ({[round(c, 1) for c in cands]})"
    return lot, f"lot {lot} derived from {len(cands)} futures month(s)"


def parse(day: dt.date, blob: bytes, closes: dict[str, float], symbols: set[str]) -> tuple[list[tuple], list[str], list[str]]:
    """Returns (rows for options_eod, rejections, day-level notes). A symbol whose lot or underlying is unknown is dropped."""
    z = zipfile.ZipFile(io.BytesIO(blob))
    data = list(csv.DictReader(io.StringIO(z.read(z.namelist()[0]).decode("utf-8", "replace"))))
    notes, good, bad = [], [], []
    for sym in sorted(symbols):
        und = closes.get(sym)
        lot, lnote = derive_lot([r for r in data if r.get("INSTRUMENT") == "FUTIDX" and r.get("SYMBOL") == sym])
        opts = [r for r in data if r.get("INSTRUMENT") == "OPTIDX" and r.get("SYMBOL") == sym]
        if not opts:
            continue
        if und is None or lot is None:
            notes.append(f"{sym}: NOT loaded - " + ("index close missing" if und is None else lnote))
            continue
        notes.append(f"{sym}: {lnote}; underlying from NSE index close")
        seen = set()
        for r in opts:
            try:
                exp = dt.datetime.strptime(r["EXPIRY_DT"].strip(), "%d-%b-%Y").date()
                k = float(r["STRIKE_PR"])
                o, h, l, c, s = (float(r[x]) for x in ("OPEN", "HIGH", "LOW", "CLOSE", "SETTLE_PR"))
                oi, coi, n = int(float(r["OPEN_INT"] or 0)), int(float(r["CHG_IN_OI"] or 0)), int(float(r["CONTRACTS"] or 0))
            except (KeyError, ValueError):
                bad.append(f"{sym} unparsable row")
                continue
            opt = r["OPTION_TYP"].strip()
            key = (exp, k, opt)
            why = None
            if opt not in ("CE", "PE"):
                why = "bad option type"
            elif exp < day:
                why = "expiry before trade date"
            elif key in seen:
                why = "duplicate contract"
            elif c < 0 or s < 0:
                why = "negative close/settle"
            elif o > 0 and not (l <= o <= h and l <= c <= h):
                why = "inconsistent OHLC"
            if why:
                bad.append(f"{sym} {exp} {k} {opt}: {why}")
                continue
            seen.add(key)
            good.append((day.isoformat(), sym, exp.isoformat(), k, opt, o, h, l, c, s, und, oi, coi, n, lot,
                         f"{sym}{exp:%d%b%y}{int(k) if k == int(k) else k}{opt}".upper()))
    return good, bad, notes


INDEX_CLOSE_SCHEMA = """CREATE TABLE IF NOT EXISTS index_close (
  trade_date TEXT NOT NULL, symbol TEXT NOT NULL, close REAL NOT NULL, source TEXT NOT NULL, PRIMARY KEY (trade_date, symbol, source))"""


def fallback_closes(conn: sqlite3.Connection) -> dict:
    conn.execute(INDEX_CLOSE_SCHEMA)
    return {(d, s): c for d, s, c in conn.execute("SELECT trade_date, symbol, close FROM index_close WHERE source='angelone'")}


def load_range(start: dt.date, end: dt.date, symbols=("NIFTY", "BANKNIFTY"), newest_first: bool = True, delay: float = 0.6,
               db: Path = bhavcopy.DB, log=print) -> dict:
    """Loads legacy days in [start, min(end, 2024-07-07)], newest first by default. Commits per day."""
    RAW.mkdir(parents=True, exist_ok=True)
    end = min(end, UDIFF_START - dt.timedelta(days=1))
    syms = {s.upper() for s in symbols}
    days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    if newest_first:
        days.reverse()
    summary = {"loaded_days": 0, "no_file_days": 0, "partial_days": [], "errors": [], "rows": 0, "rejected": 0, "skipped_existing": 0}
    conn = sqlite3.connect(db, timeout=120)
    conn.executescript(bhavcopy.SCHEMA)
    fb = fallback_closes(conn)
    with httpx.Client(headers=bhavcopy.HEADERS, timeout=60, follow_redirects=True) as client:
        for d in days:
            st = conn.execute("SELECT status, note FROM load_log WHERE trade_date=?", (d.isoformat(),)).fetchone()
            if st and st[0] == "loaded" and not (st[1] or "").startswith("legacy"):
                summary["skipped_existing"] += 1          # UDiFF day: never overwrite
                continue
            if st and st[0] == "loaded" and (st[1] or "").startswith("legacy") and "NOT loaded" not in (st[1] or ""):
                summary["skipped_existing"] += 1          # already loaded from a legacy file
                continue
            mon = d.strftime("%b").upper()
            path = RAW / f"fo{d:%Y%m%d}.zip"
            try:
                cached = path.exists()
                blob = _get(client, FO_URL.format(y=d.year, mon=mon, d=d), path, True)
                if not cached:
                    time.sleep(delay)
                if blob is None:
                    if d.weekday() < 5:
                        with conn:
                            conn.execute("INSERT OR REPLACE INTO load_log VALUES (?,?,?,?,?,?)", (d.isoformat(), "no file", 0, 0, "legacy: holiday or not published", dt.datetime.now().isoformat(timespec="seconds")))
                        summary["no_file_days"] += 1
                    continue
                ipath = RAW / f"ix{d:%Y%m%d}.csv"
                icached = ipath.exists()
                itext = _get(client, IX_URL.format(d=d), ipath, False)
                if not icached:
                    time.sleep(delay)
                closes = index_closes(itext.decode("utf-8", "replace")) if itext else {}
                src = {s: "NSE index file" for s in closes}
                for s in syms:
                    if s not in closes and (d.isoformat(), s) in fb:
                        closes[s], src[s] = fb[(d.isoformat(), s)], "Angel One daily index close"
            except (RuntimeError, httpx.HTTPError) as exc:
                summary["errors"].append(f"{d}: {exc}")
                log(f"  {d}: ERROR {exc}")
                continue
            rows, bad, notes = parse(d, blob, closes, syms)
            notes = [n.replace("underlying from NSE index close", "underlying: " + src.get(n.split(":")[0], "?")) for n in notes]
            note = "legacy format; " + " | ".join(notes) + (f" | rejected: {'; '.join(bad[:3])}" if bad else "")
            with conn:
                conn.execute("DELETE FROM options_eod WHERE trade_date=? AND symbol IN (%s)" % ",".join("?" * len(syms)), (d.isoformat(), *syms))
                conn.executemany("INSERT INTO options_eod VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                conn.execute("INSERT OR REPLACE INTO load_log VALUES (?,?,?,?,?,?)",
                             (d.isoformat(), "loaded" if rows else "no data", len(rows), len(bad), note[:1000], dt.datetime.now().isoformat(timespec="seconds")))
            summary["loaded_days"] += bool(rows)
            summary["rows"] += len(rows)
            summary["rejected"] += len(bad)
            if any("NOT loaded" in n for n in notes):
                summary["partial_days"].append(d.isoformat())
            log(f"  {d}: {len(rows)} rows" + (f", {len(bad)} rejected" if bad else "") + ("" if all("NOT" not in n for n in notes) else " | " + "; ".join(n for n in notes if "NOT" in n)))
    conn.close()
    return summary
