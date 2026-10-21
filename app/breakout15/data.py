"""15-minute index candles from Angel One for the breakout backtest, cached as
JSON per request chunk (completed chunks are never refetched). Kept apart from
the Replay store: nothing here reads or writes it."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
CACHE = Path(__file__).resolve().parents[2] / "data" / "breakout15" / "cache15"
CHUNK_DAYS = 150  # Angel One 15-minute requests are capped near 200 days
DELAY = 3.0
INDICES = {"NIFTY": "99926000", "BANKNIFTY": "99926009"}  # scrip-master index name -> expected NSE token (verified at run time)


def parse_ts(raw: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    return t.astimezone(IST).replace(tzinfo=None) if t.tzinfo else t


def chunk_ranges(start: dt.date, end: dt.date) -> list[tuple[dt.datetime, dt.datetime]]:
    out, cur = [], start
    while cur <= end:
        last = min(cur + dt.timedelta(days=CHUNK_DAYS - 1), end)
        out.append((dt.datetime.combine(cur, dt.time(9, 15)), dt.datetime.combine(last, dt.time(15, 30))))
        cur = last + dt.timedelta(days=1)
    return out


async def fetch_minutes(symbol: str, start: dt.date, end: dt.date, fetch_chunk, progress=None, today: dt.date | None = None) -> list[tuple]:
    """fetch_chunk(from_dt, to_dt) -> raw Angel rows [[ts,o,h,l,c,v],...]."""
    today = today or dt.datetime.now(IST).date()
    folder = CACHE / symbol
    folder.mkdir(parents=True, exist_ok=True)
    plan, fetched, rows = chunk_ranges(start, end), False, []
    for n, (a, b) in enumerate(plan, 1):
        f = folder / f"{a:%Y%m%d}_{b:%Y%m%d}.json"
        if f.exists() and b.date() < today:
            data = json.loads(f.read_text())
        else:
            if fetched:
                await asyncio.sleep(DELAY)
            fetched = True
            data = await fetch_chunk(a, b)
            if b.date() < today:
                f.write_text(json.dumps(data))
        rows += data
        if progress:
            progress(n, len(plan))
    seen: dict[dt.datetime, tuple] = {}
    for r in rows:
        ts = parse_ts(r[0])
        if start <= ts.date() <= end:
            seen[ts] = (ts, float(r[1]), float(r[2]), float(r[3]), float(r[4]))
    return [seen[k] for k in sorted(seen)]
