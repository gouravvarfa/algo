"""
Confirmed swing Top/Bottom detection on the CLOSE-PRICE series only (never
high/low) — per the Top-Bottom System spec, section 7/8.

No look-ahead bias: a bar at index i is only a candidate pivot once
`right_bars` further closes exist beyond it, and it only ever gets emitted
at that later confirmation index — never backdated to look as if it were
known at the pivot bar itself. Both the historical backtest and any future
live-mode caller must use this exact same function so their behavior can
never diverge.
"""
from __future__ import annotations

from app.strategy.top_bottom.models import SwingPoint


def detect_confirmed_swings(
    dates: list, closes: list[float], left_bars: int = 2, right_bars: int = 2
) -> list[SwingPoint]:
    """
    Returns every confirmed swing Top/Bottom in `closes`, in the order they
    become CONFIRMED (i.e. sorted by confirmed_index, not pivot_index).

    A bar at index i is a swing TOP if close[i] > close[i-left_bars..i-1]
    and close[i] > close[i+1..i+right_bars] (strict on both sides — an
    exact tie does not confirm a new pivot, avoiding ambiguous double-tops
    being reported as two separate breakouts). Symmetric rule for BOTTOM.
    """
    n = len(closes)
    swings: list[SwingPoint] = []
    if left_bars < 1 or right_bars < 1:
        raise ValueError("left_bars and right_bars must each be >= 1")

    for i in range(left_bars, n - right_bars):
        left_window = closes[i - left_bars : i]
        right_window = closes[i + 1 : i + 1 + right_bars]
        pivot = closes[i]

        if pivot > max(left_window) and pivot > max(right_window):
            confirmed_index = i + right_bars
            swings.append(
                SwingPoint(
                    kind="TOP",
                    pivot_index=i,
                    pivot_date=dates[i],
                    price=pivot,
                    confirmed_index=confirmed_index,
                    confirmed_date=dates[confirmed_index],
                )
            )
        elif pivot < min(left_window) and pivot < min(right_window):
            confirmed_index = i + right_bars
            swings.append(
                SwingPoint(
                    kind="BOTTOM",
                    pivot_index=i,
                    pivot_date=dates[i],
                    price=pivot,
                    confirmed_index=confirmed_index,
                    confirmed_date=dates[confirmed_index],
                )
            )

    swings.sort(key=lambda s: (s.confirmed_index, s.pivot_index))
    return swings


def swings_confirmed_by(swings: list[SwingPoint], as_of_index: int) -> list[SwingPoint]:
    """The subset of `swings` that are already confirmed as of bar `as_of_index`
    (i.e. confirmed_index <= as_of_index) — this is the ONLY view of swing
    history the event-loop backtester (backtest.py) is allowed to use at
    that bar, which is what actually enforces no-look-ahead in practice."""
    return [s for s in swings if s.confirmed_index <= as_of_index]
