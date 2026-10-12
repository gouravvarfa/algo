"""
Chunked, rate-limited, resumable BankNifty INDEX candle download from Angel
One, with per-chunk disk caching.

Read-only probe findings (2026-10-10) behind the design:
  - Index: name BANKNIFTY, token 99926009, symbol "Nifty Bank", NSE (AMXIDX).
  - Per-request caps (observed): ~30 days for 1m, ~100 for 5m, ~200 for 15m/30m,
    ~2000 for 1D -> chunk sizes in timeframes.INTERVALS carry a safety margin.
  - Sampled windows returned data back to 2020 (1m), 2021 (5m-1H), 2016 (1D);
    continuity over those years is only proven by completing a download and
    reading the resulting missing-candle report.
Nothing here runs unless the user explicitly starts a download.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import Awaitable, Callable

from app.replay.csv_import import SYMBOL, ImportResult, validate_rows
from app.replay.timeframes import INTERVALS

CHUNK_DELAY_SECONDS = 3.0
INDEX_TOKEN = "99926009"

FetchChunk = Callable[[str, dt.datetime, dt.datetime], Awaitable[list[list]]]  # (angel interval, from, to) -> rows


class FetchRefused(ValueError):
    pass


def chunks(interval: str, start: dt.date, end: dt.date) -> list[tuple[dt.datetime, dt.datetime]]:
    if interval not in INTERVALS:
        raise FetchRefused(f"unsupported interval {interval}")
    if start > end:
        raise FetchRefused("from must not be after to")
    span = INTERVALS[interval][2]
    daily = INTERVALS[interval][1] is None
    out, cur = [], start
    while cur <= end:
        last = min(cur + dt.timedelta(days=span - 1), end)
        a = dt.datetime.combine(cur, dt.time(0, 0) if daily else dt.time(9, 15))
        b = dt.datetime.combine(last, dt.time(0, 0) if daily else dt.time(15, 30))
        out.append((a, b))
        cur = last + dt.timedelta(days=1)
    return out


async def fetch_index(
    interval: str, start: dt.date, end: dt.date, fetch_chunk: FetchChunk, cache_dir: Path,
    delay: float = CHUNK_DELAY_SECONDS, progress: Callable[[int, int], None] | None = None,
    today: dt.date | None = None,
) -> ImportResult:
    """Completed chunks (ending before today) are cached on disk and never
    refetched, so an interrupted download resumes where it stopped."""
    today = today or dt.date.today()
    folder = Path(cache_dir) / SYMBOL / interval
    folder.mkdir(parents=True, exist_ok=True)
    plan = chunks(interval, start, end)
    angel_iv = INTERVALS[interval][0]
    rows: list[dict] = []
    fetched_any = False
    for n, (a, b) in enumerate(plan, start=1):
        f = folder / f"{a:%Y%m%d}_{b:%Y%m%d}.json"
        if f.exists() and b.date() < today:
            data = json.loads(f.read_text(encoding="utf-8"))
        else:
            if fetched_any:
                await asyncio.sleep(delay)
            fetched_any = True
            data = await fetch_chunk(angel_iv, a, b)
            if b.date() < today:
                f.write_text(json.dumps(data), encoding="utf-8")
        for r in data:
            rows.append({
                "timestamp": r[0], "instrument": SYMBOL, "interval": interval,
                "open": str(r[1]), "high": str(r[2]), "low": str(r[3]), "close": str(r[4]), "volume": str(r[5]),
            })
        if progress:
            progress(n, len(plan))
    result = validate_rows(rows, interval=interval)
    result.report["source"] = "ANGELONE"
    result.report["requested"] = [start.isoformat(), end.isoformat()]
    return result
