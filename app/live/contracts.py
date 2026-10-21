"""Which futures contract a position lives in (BankNifty monthly futures; the index itself cannot be traded).

Rule (decided with the user): the cutoff of a contract is ROLL_DAYS (8) calendar days before its expiry, and the cutoff day itself already
belongs to the NEXT contract. A weekend cutoff moves to the next Monday (exchange holidays are not known here: a holiday cutoff simply
takes effect on the first session that day-count reaches). New entries before the cutoff go to the nearer contract, entries on or after
it to the next one. A position open at the cutoff is rolled that day (see roll logic in the engine).
"""
from __future__ import annotations

import datetime as dt

ROLL_DAYS = 8


def cutoff(expiry: dt.date, roll_days: int = ROLL_DAYS) -> dt.date:
    c = expiry - dt.timedelta(days=roll_days)
    while c.weekday() >= 5:            # Saturday/Sunday: first session after the weekend
        c += dt.timedelta(days=1)
    return c


def active_expiry(on: dt.date, expiries: list[dt.date], roll_days: int = ROLL_DAYS) -> dt.date | None:
    """Contract for a NEW entry on `on`: the nearest contract that has not reached its cutoff (and has not expired)."""
    for e in sorted(expiries):
        if on <= e and on < cutoff(e, roll_days):
            return e
    return None


def chain(expiries: list[dt.date], start: dt.date, roll_days: int = ROLL_DAYS) -> list[tuple[dt.date, dt.date]]:
    """[(expiry, active_from)] from `start`: the first contract is the one active on `start`, each next one starts on the previous cutoff."""
    ex = sorted(expiries)
    first = active_expiry(start, ex, roll_days)
    if first is None:
        return []
    out, cur_from = [(first, start)], None
    for e in ex:
        if e <= first:
            continue
        prev = out[-1][0]
        out.append((e, cutoff(prev, roll_days)))
    return out
