"""Which futures contract a position lives in (BankNifty monthly futures; the index itself cannot be traded).

Rule (decided with the user): the cutoff of a contract is ROLL_DAYS (8) calendar days before its expiry, and the cutoff day itself already
belongs to the NEXT contract. A weekend cutoff moves to the next Monday (exchange holidays are not known here: a holiday cutoff simply
takes effect on the first session that day-count reaches). New entries before the cutoff go to the nearer contract, entries on or after
it to the next one. A position open at the cutoff is rolled that day (see roll logic in the engine).

Hold rule (owner, 17 Nov 2026), for contracts whose cutoff is on or after HOLD_ROLL_FROM: a position open at the cutoff is NOT rolled there. It stays
in its contract, managed by that contract's System Points, until its stop / System Point closes it (the next trade then goes into the next
contract) or until the HOLD LIMIT: the open of the expiry day (NFO), or of the trading day before the tender (delivery) period (MCX). Still open
then, it is closed at that open and the same side is taken in the next contract.
"""
from __future__ import annotations

import datetime as dt

ROLL_DAYS = 8
HOLD_ROLL_FROM = dt.date(2026, 11, 18)      # contracts whose cutoff is on/after this day follow the hold rule; earlier rolls stay as they happened
# MCX delivery (tender) period: the contract's last N trading days, the expiry day included (MCX Gold Ten and Silver Micro: 3, e.g. 26-30 Nov 2026).
# An MCX symbol not listed here is held only until the day before the longer 5-day window, so a position is never carried into delivery.
MCX_TENDER_DAYS = {"GOLDTEN": 3, "SILVERMIC": 3}
MCX_TENDER_DAYS_DEFAULT = 5


def cutoff(expiry: dt.date, roll_days: int = ROLL_DAYS) -> dt.date:
    c = expiry - dt.timedelta(days=roll_days)
    while c.weekday() >= 5:            # Saturday/Sunday: first session after the weekend
        c += dt.timedelta(days=1)
    return c


def hold_rule(expiry: dt.date, roll_days: int = ROLL_DAYS) -> bool:
    """Does the hold rule apply to this contract (its cutoff is on/after HOLD_ROLL_FROM)?"""
    return cutoff(expiry, roll_days) >= HOLD_ROLL_FROM


def hold_limit(expiry: dt.date, exch_seg: str, symbol: str, roll_days: int = ROLL_DAYS) -> dt.date:
    """The day a position still held past the cutoff is closed at the open and continued in the next contract: NFO (index and stock futures)
    the expiry day itself; MCX the weekday before its tender period (the last N weekdays up to and including the expiry day). Never before the
    cutoff. Exchange holidays are not known here: an unknown holiday inside the tender period only makes the real period start a day earlier, so
    MCX symbols not listed in MCX_TENDER_DAYS use the longer 5-day window."""
    cut = cutoff(expiry, roll_days)
    if (exch_seg or "").upper() != "MCX":
        return max(expiry, cut)
    n, d = MCX_TENDER_DAYS.get((symbol or "").upper(), MCX_TENDER_DAYS_DEFAULT), expiry
    while n > 0:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return max(d, cut)


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
