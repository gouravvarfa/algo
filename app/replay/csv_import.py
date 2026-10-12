"""
Validation + CSV import of BankNifty INDEX candles.

CSV columns (header required, any order):
  timestamp,instrument,interval,open,high,low,close,volume
- instrument: must be BANKNIFTY (the index). Anything that looks like a
  futures/option contract (e.g. BANKNIFTY27OCT26FUT) is rejected, so futures
  data can never enter the index replay.
- interval: 1m, 5m, 15m, 30m, 1H or 1D (one interval per file).
- timestamp: ISO 8601 with offset (converted to IST) or naive IST; 1D accepts
  a plain date. Candle START time, on the interval's 09:15 grid.
- volume may be 0 (index data has no traded volume).

Invalid rows are rejected into a report; missing candles are REPORTED, never
filled. Shared by the CSV importer and the Angel One downloader.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
from dataclasses import dataclass, field

from app.replay.timeframes import INTERVALS, expected_starts, on_grid

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
REQUIRED = ["timestamp", "instrument", "interval", "open", "high", "low", "close", "volume"]
SYMBOL = "BANKNIFTY"
MAX_REPORTED = 500
_DERIVATIVE = re.compile(r"(FUT|CE|PE)$|\d{2}[A-Z]{3}\d{2}")


@dataclass
class ImportResult:
    ok: bool
    meta: dict | None = None
    rows: list[tuple] = field(default_factory=list)
    report: dict = field(default_factory=dict)


def _parse_ts(raw: str) -> dt.datetime:
    raw = raw.strip().replace("Z", "+00:00")
    t = dt.datetime.fromisoformat(raw)
    return t.astimezone(IST).replace(tzinfo=None) if t.tzinfo else t


def parse_csv_text(text: str) -> tuple[list[dict], list[str]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    reader.fieldnames = header
    missing = [c for c in REQUIRED if c not in header]
    return ([{k: (v or "").strip() for k, v in row.items() if k} for row in reader], missing)


def incomplete_days(rows: list[tuple], interval: str) -> dict[str, list[str]]:
    """Per trading day present, the candle start times (HH:MM) missing from a
    full session. Daily candles: calendar gaps longer than 5 days are flagged
    (no holiday calendar, so shorter gaps are normal weekends/holidays)."""
    out: dict[str, list[str]] = {}
    if INTERVALS[interval][1] is None:
        days = sorted({r[0].date() for r in rows})
        for a, b in zip(days, days[1:]):
            if (b - a).days > 5:
                out[b.isoformat()] = [f"gap of {(b - a).days} days after {a.isoformat()}"]
        return out
    exp = expected_starts(interval)
    by_day: dict[dt.date, set[int]] = {}
    for r in rows:
        by_day.setdefault(r[0].date(), set()).add(r[0].hour * 60 + r[0].minute)
    for day, have in sorted(by_day.items()):
        miss = [f"{m // 60:02d}:{m % 60:02d}" for m in exp if m not in have]
        if miss:
            out[day.isoformat()] = miss
    return out


def validate_rows(raw_rows: list[dict], missing_columns: list[str] | None = None, interval: str | None = None) -> ImportResult:
    rep: dict = {"total_rows": len(raw_rows), "rejected": [], "warnings": [], "rejected_count": 0}
    if missing_columns:
        rep["fatal"] = f"Missing required columns: {', '.join(missing_columns)}"
        return ImportResult(False, None, [], rep)

    def reject(line: int, reason: str) -> None:
        rep["rejected_count"] += 1
        if len(rep["rejected"]) < MAX_REPORTED:
            rep["rejected"].append({"line": line, "reason": reason})

    file_interval = interval
    seen: set[dt.datetime] = set()
    good: list[tuple] = []
    out_of_order, prev = 0, None

    for n, row in enumerate(raw_rows, start=2):  # line 1 is the header
        sym = row["instrument"].upper().replace(" ", "")
        if sym != SYMBOL:
            why = "futures/option contracts are never accepted in the index replay" if _DERIVATIVE.search(sym) else "only the BANKNIFTY index is supported"
            reject(n, f"instrument '{row['instrument']}': {why}")
            continue
        iv = row["interval"]
        if iv not in INTERVALS:
            reject(n, f"unsupported interval '{iv}' (use {', '.join(INTERVALS)})")
            continue
        if file_interval is None:
            file_interval = iv
        elif iv != file_interval:
            reject(n, f"interval {iv} differs from the file's interval {file_interval} (one interval per file)")
            continue
        try:
            ts = _parse_ts(row["timestamp"])
            o, h, lo, c, v = (float(row[k]) for k in ("open", "high", "low", "close", "volume"))
        except (ValueError, KeyError) as exc:
            reject(n, f"unparseable value: {exc}")
            continue
        if not on_grid(file_interval, ts):
            reject(n, f"timestamp {ts} is not on the {file_interval} candle grid (09:15-15:30 IST session)")
            continue
        if min(o, h, lo, c) <= 0 or v < 0 or h < max(o, c, lo) or lo > min(o, c, h):
            reject(n, "inconsistent OHLC (need low <= open,close <= high, prices > 0, volume >= 0)")
            continue
        if ts in seen:
            reject(n, "duplicate timestamp (first occurrence kept)")
            continue
        if prev is not None and ts < prev:
            out_of_order += 1
        prev = ts
        seen.add(ts)
        good.append((ts, o, h, lo, c, v))

    good.sort(key=lambda r: r[0])
    if out_of_order:
        rep["warnings"].append(f"{out_of_order} rows were out of order and have been sorted")
    if not good or file_interval is None:
        rep["fatal"] = "No valid rows" + (f" - first problems: {'; '.join(r['reason'] for r in rep['rejected'][:3])}" if rep["rejected"] else "")
        return ImportResult(False, None, [], rep)

    gaps = incomplete_days(good, file_interval)
    rep.update({
        "accepted": len(good), "first_ts": good[0][0].isoformat(), "last_ts": good[-1][0].isoformat(),
        "incomplete_days": len(gaps), "incomplete_days_detail": dict(list(gaps.items())[:200]),
        "missing_candles_total": sum(len(v) for v in gaps.values()) if INTERVALS[file_interval][1] else 0,
        "note": "Missing candles are reported, never filled. Holidays/short sessions also show as incomplete days.",
    })
    return ImportResult(True, {"symbol": SYMBOL, "interval": file_interval, "instrument_type": "INDEX"}, good, rep)


def validate_csv(text: str) -> ImportResult:
    rows, missing = parse_csv_text(text)
    return validate_rows(rows, missing)
