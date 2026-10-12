"""
1-minute data completeness of every Daily session used by the hybrid replay. Read-only: nothing is filled or repaired.

Expected candles: the regular NSE session 09:15-15:29 IST = 375 one-minute candles.

Which days are trading sessions: this app has NO exchange holiday calendar, so the stored Daily candle is the evidence.
  - weekday with a Daily candle           -> a regular session: Complete (375/375, right boundaries, valid OHLC) or Incomplete
  - 1-minute data but no Daily candle     -> Not Verified (cannot confirm it was a regular session)
  - weekend date                          -> Not Verified (special sessions have no known expected count here)
  - weekday with neither                  -> listed separately as "no session in stored data": a holiday OR missing data;
                                             the two cannot be told apart without an exchange calendar.
Duplicate timestamps cannot exist in the store (unique key per instrument/interval/timestamp); duplicates met during an
import are rejected and counted in that import's validation report.
"""
from __future__ import annotations

import datetime as dt

from app.replay.store import ReplayStore

SYMBOL = "BANKNIFTY"
EXPECTED = 375
OPEN_MIN, LAST_MIN = 9 * 60 + 15, 15 * 60 + 29
CALENDAR_NOTE = ("No exchange holiday calendar is configured: trading sessions are identified from the stored Daily candles. "
                 "Shortened or special sessions therefore show as Incomplete, and weekdays with no data at all cannot be classified as holiday vs missing.")


def _ranges(missing: list[int]) -> list[str]:
    out, start, prev = [], None, None
    for m in missing:
        if start is None:
            start = prev = m
        elif m == prev + 1:
            prev = m
        else:
            out.append((start, prev))
            start = prev = m
    if start is not None:
        out.append((start, prev))
    f = lambda x: f"{x // 60:02d}:{x % 60:02d}"
    return [f(a) if a == b else f"{f(a)}-{f(b)}" for a, b in out]


def session_quality(store: ReplayStore, first: str, last: str) -> dict:
    start = dt.datetime.combine(dt.date.fromisoformat(first), dt.time())
    end = dt.datetime.combine(dt.date.fromisoformat(last), dt.time(23, 59))
    stats = {d: (n, lo, hi, bad) for d, n, lo, hi, bad in store.day_stats(SYMBOL, "1m", start, end)}
    daily = {d for d in store.distinct_days(SYMBOL, "1D") if first <= d <= last}
    sessions: list[dict] = []
    for day in sorted(daily | set(stats)):
        n, lo, hi, bad = stats.get(day, (0, None, None, 0))
        weekday = dt.date.fromisoformat(day).weekday()
        entry = {"date": day, "expected": EXPECTED, "actual": n, "missing": max(0, EXPECTED - n),
                 "coverage_pct": round(min(n, EXPECTED) / EXPECTED * 100, 2), "first": lo[11:16] if lo else None, "last": hi[11:16] if hi else None,
                 "invalid_ohlc": bad, "duplicates": 0, "missing_ranges": [], "note": ""}
        if day not in daily:
            entry.update(status="Not Verified", expected=None, missing=None, coverage_pct=None,
                         note="1-minute data without a stored Daily candle: cannot confirm a regular trading session")
        elif weekday >= 5:
            entry.update(status="Not Verified", expected=None, missing=None, coverage_pct=None,
                         note="weekend date (special session): the expected candle count is not known")
        else:
            problems = []
            if n == 0:
                problems.append("no 1-minute candles stored")
            else:
                if n < EXPECTED:
                    have = {t[0].hour * 60 + t[0].minute for t in store.candles(
                        SYMBOL, "1m", dt.datetime.combine(dt.date.fromisoformat(day), dt.time()),
                        dt.datetime.combine(dt.date.fromisoformat(day), dt.time(23, 59)))}
                    entry["missing_ranges"] = _ranges([m for m in range(OPEN_MIN, LAST_MIN + 1) if m not in have])
                    problems.append(f"{EXPECTED - n} of {EXPECTED} minutes missing")
                if lo and lo[11:16] != "09:15":
                    problems.append(f"session starts at {lo[11:16]} instead of 09:15")
                if hi and hi[11:16] != "15:29":
                    problems.append(f"session ends at {hi[11:16]} instead of 15:29")
                if bad:
                    problems.append(f"{bad} candle(s) with inconsistent OHLC")
            entry.update(status="Incomplete" if problems else "Complete", note="; ".join(problems))
        sessions.append(entry)
    seen = daily | set(stats)
    nothing, d = [], dt.date.fromisoformat(first)
    while d <= dt.date.fromisoformat(last):
        if d.weekday() < 5 and d.isoformat() not in seen:
            nothing.append(d.isoformat())
        d += dt.timedelta(days=1)
    regular = [s for s in sessions if s["status"] in ("Complete", "Incomplete")]
    summary = {
        "sessions": len(sessions),
        "complete": sum(s["status"] == "Complete" for s in sessions),
        "incomplete": sum(s["status"] == "Incomplete" for s in sessions),
        "not_verified": sum(s["status"] == "Not Verified" for s in sessions),
        "expected_minutes": sum(s["expected"] for s in regular), "actual_minutes": sum(min(s["actual"], EXPECTED) for s in regular),
        "coverage_pct": round(sum(min(s["actual"], EXPECTED) for s in regular) / (len(regular) * EXPECTED) * 100, 2) if regular else None,
        "weekdays_without_any_data": len(nothing),
    }
    return {"from_date": first, "to_date": last, "summary": summary, "sessions": sessions, "weekdays_without_any_data": nothing,
            "calendar_note": CALENDAR_NOTE,
            "warning": ("Sessions marked Incomplete or Not Verified may have missed intraday entries, stop-loss hits, round trips or whipsaws."
                        if summary["incomplete"] or summary["not_verified"] else None)}
