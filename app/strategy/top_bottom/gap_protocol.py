"""
Gap handling for the Top-Bottom System — the 15-minute gap protocol.

Pure functions, no I/O, shared by the historical backtest (backtest.py) and
any live/signal caller so the two can never diverge. Signal-only: nothing
here places a broker order.

Rules (all mirrored for SELL):
  - A BUY system point is never shifted by a gap. A gap-DOWN open simply
    leaves the point where it was; entry still needs price to cross it.
  - GAP-UP past the system point: wait for the 09:15-09:30 candle to
    COMPLETE, freeze its HIGH/LOW, and enter only when price trades above
    that HIGH. The original structural stop is preserved.
  - Existing BUY and the open gaps BELOW its structural stop: the ordinary
    stop is suspended for that day (GAP_EXIT_PENDING). Once the first
    candle completes: trading below its LOW exits the position; if it never
    does, the first candle's LOW is that day's protective stop.
  - Nothing fires off an incomplete candle, and the first candle's levels
    are frozen once complete (pass the stored `FirstCandle` back in via
    `frozen=` to guarantee a live caller never recomputes them).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal, Sequence

Direction = Literal["BUY", "SELL"]
GapStatus = Literal["GAP_EXIT_PENDING", "GAP_EXITED", "GAP_HELD", "NO_FIRST_CANDLE"]

CANDLE_MINUTES = 15
FIRST_CANDLE_START = dt.time(9, 15)
SECOND_CANDLE_START = dt.time(9, 30)
SESSION_END = dt.time(15, 30)


@dataclass(frozen=True)
class Bar15:
    """One 15-minute candle; `start` is its opening timestamp (IST)."""
    start: dt.datetime
    open: float
    high: float
    low: float
    close: float

    @property
    def end(self) -> dt.datetime:
        return self.start + dt.timedelta(minutes=CANDLE_MINUTES)


@dataclass(frozen=True)
class FirstCandle:
    """Frozen HIGH/LOW of the completed 09:15-09:30 candle."""
    high: float
    low: float
    open: float


@dataclass(frozen=True)
class GapExitResult:
    status: GapStatus
    first_candle: FirstCandle | None = None
    exit_level: float | None = None  # fill price when status == GAP_EXITED
    exit_time: dt.datetime | None = None
    protective_sl: float | None = None  # the day's protective stop once the candle is complete


@dataclass(frozen=True)
class GapEntryResult:
    status: Literal["TRIGGERED", "WAITING", "NO_ENTRY", "NO_FIRST_CANDLE"]
    first_candle: FirstCandle | None = None
    entry_level: float | None = None
    entry_time: dt.datetime | None = None


def _complete(bars: Sequence[Bar15], as_of: dt.datetime | None) -> list[Bar15]:
    done = [b for b in bars if as_of is None or b.end <= as_of]
    return sorted(done, key=lambda b: b.start)


def first_candle(bars: Sequence[Bar15], as_of: dt.datetime | None = None) -> FirstCandle | None:
    """The 09:15 candle, only once it is complete (as_of >= 09:30)."""
    for b in _complete(bars, as_of):
        if b.start.time() == FIRST_CANDLE_START:
            return FirstCandle(high=b.high, low=b.low, open=b.open)
    return None


def _later(bars: Sequence[Bar15], as_of: dt.datetime | None) -> list[Bar15]:
    return [b for b in _complete(bars, as_of) if b.start.time() >= SECOND_CANDLE_START]


def is_gap_through_stop(direction: Direction, day_open: float, stop: float) -> bool:
    """Open strictly beyond the structural stop (BUY: below, SELL: above)."""
    return day_open < stop if direction == "BUY" else day_open > stop


def is_gap_past_system_point(direction: Direction, prev_close: float, day_open: float, system_point: float) -> bool:
    """The system point was bypassed by the gap itself: price closed on the
    near side yesterday and opened beyond it today. A gap the other way
    (BUY point, open below it) is NOT this case — the point simply stays."""
    if direction == "BUY":
        return prev_close <= system_point < day_open
    return prev_close >= system_point > day_open


def resolve_gap_exit(
    direction: Direction,
    bars: Sequence[Bar15],
    as_of: dt.datetime | None = None,
    frozen: FirstCandle | None = None,
) -> GapExitResult:
    """
    Existing position, open gapped through its structural stop. `as_of=None`
    means the day is fully known (backtest); otherwise only candles complete
    at `as_of` are used (live). Exit fills at the first-candle level (or the
    candle's open if it gapped past that level); no reverse entry here.
    """
    fc = frozen or first_candle(bars, as_of)
    if fc is None:
        return GapExitResult("GAP_EXIT_PENDING" if as_of is not None else "NO_FIRST_CANDLE")
    protective = fc.low if direction == "BUY" else fc.high
    for b in _later(bars, as_of):
        if direction == "BUY" and b.low < fc.low:
            return GapExitResult("GAP_EXITED", fc, min(fc.low, b.open), b.start, protective)
        if direction == "SELL" and b.high > fc.high:
            return GapExitResult("GAP_EXITED", fc, max(fc.high, b.open), b.start, protective)
    day_over = as_of is None or as_of.time() >= SESSION_END
    return GapExitResult("GAP_HELD" if day_over else "GAP_EXIT_PENDING", fc, None, None, protective)


def resolve_gap_entry(
    direction: Direction,
    bars: Sequence[Bar15],
    as_of: dt.datetime | None = None,
    frozen: FirstCandle | None = None,
) -> GapEntryResult:
    """Fresh entry after a gap past the system point: BUY above the first
    candle HIGH, SELL below its LOW (first completed later candle that does)."""
    fc = frozen or first_candle(bars, as_of)
    if fc is None:
        return GapEntryResult("NO_FIRST_CANDLE")
    for b in _later(bars, as_of):
        if direction == "BUY" and b.high > fc.high:
            return GapEntryResult("TRIGGERED", fc, max(fc.high, b.open), b.start)
        if direction == "SELL" and b.low < fc.low:
            return GapEntryResult("TRIGGERED", fc, min(fc.low, b.open), b.start)
    day_over = as_of is None or as_of.time() >= SESSION_END
    return GapEntryResult("NO_ENTRY" if day_over else "WAITING", fc)
