"""Candle intervals for the BankNifty INDEX replay. Only natively fetched or
imported intervals are ever offered; nothing is synthesised from another
interval. Candles carry their START timestamp in IST (1D: midnight)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from app.strategy.top_bottom.gap_protocol import Bar15

# interval -> (Angel One interval name, minutes per candle or None for daily,
#              max days per request observed in the 2026-10-10 probe, used with a safety margin)
INTERVALS: dict[str, tuple[str, int | None, int]] = {
    "1m": ("ONE_MINUTE", 1, 25),
    "5m": ("FIVE_MINUTE", 5, 90),
    "15m": ("FIFTEEN_MINUTE", 15, 180),
    "30m": ("THIRTY_MINUTE", 30, 180),
    "1H": ("ONE_HOUR", 60, 360),
    "1D": ("ONE_DAY", None, 1800),
}
SESSION_OPEN_MIN = 9 * 60 + 15
SESSION_CLOSE_MIN = 15 * 60 + 30


@dataclass(frozen=True)
class Candle:
    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def expected_starts(interval: str) -> list[int]:
    """Minutes-since-midnight at which this interval's candles start in a full session."""
    n = INTERVALS[interval][1]
    if n is None:
        return []
    return list(range(SESSION_OPEN_MIN, SESSION_CLOSE_MIN, n))


def on_grid(interval: str, ts: dt.datetime) -> bool:
    n = INTERVALS[interval][1]
    if n is None:
        return ts.hour == 0 and ts.minute == 0 and ts.second == 0
    m = ts.hour * 60 + ts.minute
    return ts.second == 0 and SESSION_OPEN_MIN <= m < SESSION_CLOSE_MIN and (m - SESSION_OPEN_MIN) % n == 0


def to_candles(rows: list[tuple]) -> list[Candle]:
    return [Candle(*r) for r in rows]


def bars15_by_date(rows_15m: list[tuple]) -> dict[dt.date, list[Bar15]]:
    out: dict[dt.date, list[Bar15]] = {}
    for ts, o, h, lo, c, _v in rows_15m:
        out.setdefault(ts.date(), []).append(Bar15(ts, o, h, lo, c))
    return out
