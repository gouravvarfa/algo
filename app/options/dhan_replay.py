"""Five years of 1-minute NIFTY option prices for the Simulator's time travel, from the Dhan expired-option download (scripts/load_dhan_options.py).

The Simulator's replay reads `candles(symbol, expiry, strike, opt, ts, open, high, low, close, volume)` (app/options/intraday.py). Dhan's rows are ATM-relative (ATM-10 .. ATM+10, the
real strike on every minute) and carry no expiry date, so this module converts them into that same table, in a separate file (data/intraday/nifty_1m_dhan.db) that the live
collector never writes. Days the Angel One database already has are not taken from here.

What the replay therefore shows for an older day: the NEAREST weekly expiry only, and prices only for the strikes within about +-500 points of the index at that minute (the
rest of the chain still lists, with the previous day's Bhavcopy OI, but without a price). The expiry of a day is the exchange calendar's next NIFTY weekly expiry: Thursday up to
28 Aug 2025, Tuesday after, an exchange holiday moves it to the previous session. It was checked against the data (the ATM straddle collapses at the close of an expiry day and a new
contract starts the next morning): 3 of 1,225 day pairs disagreed, all borderline.
"""
from __future__ import annotations

import bisect
import datetime as dt
import sqlite3
from pathlib import Path

from app.options import intraday

OPEN, LAST = "09:15", "15:29"
TUESDAY_FROM = dt.date(2025, 9, 1)


def weekly_expiries(trading_days: list[str]) -> list[dt.date]:
    """The NIFTY weekly expiry dates implied by the exchange calendar, for the span of `trading_days` (ISO strings, sorted)."""
    ts = {dt.date.fromisoformat(d) for d in trading_days}
    first, last = min(ts), max(ts)
    out, x = set(), first - dt.timedelta(days=14)
    while x <= last + dt.timedelta(days=45):
        if x.weekday() == (3 if x < TUESDAY_FROM else 1):             # Thursday, then Tuesday from 1 Sep 2025
            e = x
            if first <= x <= last:
                for _ in range(6):
                    if e in ts:
                        break
                    e -= dt.timedelta(days=1)                         # an exchange holiday: the previous session
                else:
                    e = x
            out.add(e)
        x += dt.timedelta(days=1)
    return sorted(out)


BN_WED_FROM = dt.date(2023, 9, 1)          # BANKNIFTY weeklies moved Thursday -> Wednesday (the monthly stayed on the last Thursday until Feb 2024)
BN_MONTHLY_WED_FROM = dt.date(2024, 3, 1)  # the BANKNIFTY monthly moved to the last Wednesday
BN_LAST_WEEKLY = dt.date(2024, 11, 13)     # BANKNIFTY weeklies were discontinued after this one; monthly only from here
BN_MONTHLY_THU_FROM = dt.date(2025, 1, 1)  # monthly on the last Thursday (SEBI's one-weekday rule) ...
BN_MONTHLY_TUE_FROM = dt.date(2025, 9, 1)  # ... then the last Tuesday


def _last_weekday(year: int, month: int, wd: int) -> dt.date:
    d = (dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1))
    while d.weekday() != wd:
        d -= dt.timedelta(days=1)
    return d


def _shift_to_session(x: dt.date, ts: set, first: dt.date, last: dt.date) -> dt.date:
    if not (first <= x <= last):
        return x
    e = x
    for _ in range(6):
        if e in ts:
            return e
        e -= dt.timedelta(days=1)                                     # an exchange holiday: the previous session
    return x


def banknifty_expiries(trading_days: list[str]) -> list[dt.date]:
    """The NEAREST BANKNIFTY expiry dates by the exchange calendar: weeklies (Thursday, then Wednesday from Sep 2023) until 13 Nov 2024, monthly after.
    Monthly: last Thursday until Feb 2024, last Wednesday to Dec 2024, last Thursday Jan-Aug 2025, last Tuesday from Sep 2025. Holidays move to the previous session."""
    ts = {dt.date.fromisoformat(d) for d in trading_days}
    first, last = min(ts), max(ts)
    out: set[dt.date] = set()
    y, m = (first - dt.timedelta(days=40)).year, (first - dt.timedelta(days=40)).month
    end = last + dt.timedelta(days=70)
    while dt.date(y, m, 1) <= end:
        som = dt.date(y, m, 1)
        if som < BN_MONTHLY_WED_FROM:
            monthly = _last_weekday(y, m, 3)
        elif som < BN_MONTHLY_THU_FROM:
            monthly = _last_weekday(y, m, 2)
        elif som < BN_MONTHLY_TUE_FROM:
            monthly = _last_weekday(y, m, 3)
        else:
            monthly = _last_weekday(y, m, 1)
        out.add(monthly)
        x = som
        while x.month == m:
            if x <= BN_LAST_WEEKLY:
                wd = 3 if x < BN_WED_FROM else 2
                if x.weekday() == wd and not (0 <= (monthly - x).days < 7):     # the monthly expiry week has the monthly instead
                    out.add(x)
            x += dt.timedelta(days=1)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return sorted(_shift_to_session(x, ts, first, last) for x in out)


def expiries_for(symbol: str, trading_days: list[str]) -> list[dt.date]:
    return banknifty_expiries(trading_days) if symbol == "BANKNIFTY" else weekly_expiries(trading_days)


def flag_for(symbol: str, day: str) -> str:
    """Which Dhan series holds the NEAREST expiry on `day`: NIFTY always the weekly; BANKNIFTY the weekly while weeklies existed, the monthly after (the download switched on 21 Nov 2024)."""
    return "MONTH" if symbol == "BANKNIFTY" and day >= "2024-11-21" else "WEEK"


def next_expiry(day: str, expiries: list[dt.date]) -> dt.date | None:
    i = bisect.bisect_left(expiries, dt.date.fromisoformat(day))
    return expiries[i] if i < len(expiries) else None


META = """
CREATE TABLE IF NOT EXISTS replay_days (
  symbol TEXT NOT NULL, date TEXT NOT NULL, expiry TEXT NOT NULL, index_minutes INTEGER NOT NULL, contracts INTEGER NOT NULL,
  PRIMARY KEY (symbol, date)
);
"""


def build(src: Path, dst: Path, symbol: str = "NIFTY", log=print, only_after: str | None = None) -> dict:
    """Convert the Dhan download `src` into the replay database `dst` (created if missing; re-running replaces the days it converts)."""
    s = sqlite3.connect(src, timeout=60)
    s.execute("CREATE INDEX IF NOT EXISTS candles_ts ON candles(ts)")
    s.commit()
    days = [r[0] for r in s.execute("SELECT DISTINCT substr(ts,1,10) FROM candles WHERE symbol=? AND code=1 ORDER BY 1", (symbol,))]
    expiries = expiries_for(symbol, days)
    d = intraday.connect(dst)
    d.executescript(META)
    done = rows_total = 0
    for day in days:
        if only_after and day <= only_after:
            continue
        e = next_expiry(day, expiries)
        if e is None:
            continue
        lo, hi = f"{day} {OPEN}", f"{day} {LAST}"
        flag = flag_for(symbol, day)
        opts = s.execute("SELECT opt, ts, strike, open, high, low, close, volume FROM candles WHERE symbol=? AND flag=? AND code=1 AND ts>=? AND ts<=?", (symbol, flag, lo, hi)).fetchall()
        idx = s.execute("SELECT ts, spot FROM candles WHERE symbol=? AND flag=? AND code=1 AND opt='CE' AND rel=0 AND ts>=? AND ts<=? AND spot IS NOT NULL ORDER BY ts", (symbol, flag, lo, hi)).fetchall()
        if not opts or not idx:
            continue
        d.execute("DELETE FROM candles WHERE symbol=? AND ts>=? AND ts<=?", (symbol, day + " 00:00", day + " 23:59"))
        d.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)",
                      [(symbol, e.isoformat(), float(k), o, ts, op, hh, ll, cl, int(v or 0)) for o, ts, k, op, hh, ll, cl, v in opts if cl is not None and op is not None])
        d.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)", [(symbol, *intraday.INDEX_KEY, ts, sp, sp, sp, sp, 0) for ts, sp in idx])
        contracts = len({(k, o) for o, _, k, *_ in opts})
        d.execute("INSERT OR REPLACE INTO replay_days VALUES (?,?,?,?,?)", (symbol, day, e.isoformat(), len(idx), contracts))
        d.commit()
        done += 1
        rows_total += len(opts) + len(idx)
        if done % 100 == 0:
            log(f"{done} days converted, {rows_total:,} rows, at {day}")
    d.commit()
    return {"days": done, "rows": rows_total, "from": days[0] if days else None, "to": days[-1] if days else None}


def days(c: sqlite3.Connection, symbol: str) -> list[dict]:
    """Days the replay file holds, in the same shape as intraday.days()."""
    try:
        return [{"date": r[0], "index_minutes": r[1], "contracts": r[2]} for r in c.execute("SELECT date, index_minutes, contracts FROM replay_days WHERE symbol=? ORDER BY date", (symbol,))]
    except sqlite3.OperationalError:
        return []
