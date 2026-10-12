"""
The Top-Bottom System: an always-reversing, close-price trailing-stop
engine (spec sections 1-9).

Core model — NOT a fixed-SL/break-even/gap system:
  - BUY SYSTEM POINT = latest confirmed Top. SELL SYSTEM POINT = latest
    confirmed Bottom. A very small/latest reversal is a valid System
    Point — no minimum size, no 2/2 or wider pivot confirmation. Swing
    detection always uses left_bars=right_bars=1 (see swing_detection.py).
  - FLAT -> BUY when close crosses ABOVE the latest confirmed Top; initial
    stop = latest confirmed Bottom.
  - FLAT -> SELL when close crosses BELOW the latest confirmed Bottom;
    initial stop = latest confirmed Top.
  - While BUY, the stop TRAILS forward to the latest confirmed Bottom as
    newer ones confirm — it only ever moves up, never back down.
  - While SELL, the stop TRAILS forward to the latest confirmed Top as
    newer ones confirm — it only ever moves down, never back up.
  - FILLS HAPPEN AT THE LEVEL, not at the close that crossed it. An entry
    fills at its System Point; an exit fills at its stop level, and the
    reversal enters at that same level. This is what a resting stop/limit
    order actually does, and it is what the manual ADANIGREEN backtest
    (the source of truth this engine is validated against) records —
    every entry and exit price there is a structure level, never the
    breaching close.
  - When close crosses the trailing stop, that trade exits AT THE STOP
    LEVEL and the system immediately reverses into the opposite direction
    at that same level (the breached level becomes the new System Point) —
    there is no flat gap between trades once the first one has opened.
  - Only one position at a time; BUY->BUY and SELL->SELL are structurally
    impossible (the state machine only ever flips direction on exit).

No look-ahead: exactly as in swing_detection.py, only swings CONFIRMED by
bar i (confirmed_index <= i) are ever visible while processing bar i.
"""
from __future__ import annotations

import datetime as dt
import uuid

from app.strategy.top_bottom.models import Direction, InstrumentClass, Signal, Trade
from app.strategy.top_bottom.swing_detection import detect_confirmed_swings, swings_confirmed_by

# The product's one and only confirmation window — see the module
# docstring and the spec's explicit prohibition on 2/2, 3/3, 5/5 or any
# other wider/minimum-size pivot rule.
SWING_LEFT_BARS = 1
SWING_RIGHT_BARS = 1


def _is_stop_hit(direction: Direction, stop_price: float, bar_close: float) -> bool:
    """Strictly through the level — a close exactly ON the stop has not
    crossed it (matches the manual backtest this engine is validated against)."""
    return bar_close < stop_price if direction == "BUY" else bar_close > stop_price


def _open_trade(
    direction: Direction,
    system_point: float,
    initial_sl: float,
    symbol: str,
    trading_symbol: str,
    expiry: str,
    instrument_class: InstrumentClass,
    lot_size: int,
    entry_date: dt.datetime,
    entry_price: float,
    reversal_event_id: str,
) -> Trade:
    return Trade(
        direction=direction,
        symbol=symbol,
        trading_symbol=trading_symbol,
        expiry=expiry,
        instrument_class=instrument_class,
        lot_size=lot_size,
        signal_date=entry_date,
        entry_date=entry_date,
        entry_price=entry_price,
        system_point=system_point,
        initial_sl=initial_sl,
        active_stop=initial_sl,
        reversal_event_id=reversal_event_id,
        stop_history=[(entry_date, initial_sl)],
    )


def run_top_bottom_backtest(
    symbol: str,
    trading_symbol: str,
    expiry: str,
    instrument_class: InstrumentClass,
    lot_size: int,
    dates: list[dt.datetime],
    closes: list[float],
    opens: list[float],
) -> tuple[list[Signal], list[Trade]]:
    """
    Pure function: bar arrays in, signals + trades out. `opens` is accepted
    for call-site symmetry with the data-fetch layer but is unused — this
    system's entries, exits and trailing stop are all CLOSE-price events
    (spec section 11: close-only execution, never high/low/open).
    """
    del opens
    if len(dates) != len(closes):
        raise ValueError("dates and closes must be the same length")

    all_swings = detect_confirmed_swings(dates, closes, SWING_LEFT_BARS, SWING_RIGHT_BARS)

    signals: list[Signal] = []
    trades: list[Trade] = []
    open_trade: Trade | None = None

    for i in range(len(closes)):
        date = dates[i]
        close = closes[i]

        confirmed = swings_confirmed_by(all_swings, i)
        tops = [s for s in confirmed if s.kind == "TOP"]
        bottoms = [s for s in confirmed if s.kind == "BOTTOM"]
        latest_top = tops[-1].price if tops else None
        latest_bottom = bottoms[-1].price if bottoms else None

        if open_trade is None:
            # FLAT: watch for a breakout above the latest Top or a
            # breakdown below the latest Bottom — whichever happens first.
            if latest_top is not None and close > latest_top:
                event_id = str(uuid.uuid4())
                if latest_bottom is None:
                    # No confirmed Bottom yet to anchor an initial stop —
                    # the System has nothing valid to protect the trade
                    # with, so this breakout is skipped rather than
                    # entered with a fabricated stop.
                    continue
                # Filled AT the System Point, not at the close that crossed
                # it — a stop/limit order rests at the level, so that level
                # is the fill. (Verified against the manual ADANIGREEN
                # backtest: every entry and exit there is a structure level.)
                signals.append(Signal("BUY", i, date, latest_top, latest_top, None))
                open_trade = _open_trade(
                    "BUY", latest_top, latest_bottom, symbol, trading_symbol, expiry,
                    instrument_class, lot_size, date, latest_top, event_id,
                )
            elif latest_bottom is not None and close < latest_bottom:
                if latest_top is None:
                    continue
                signals.append(Signal("SELL", i, date, latest_bottom, latest_bottom, None))
                open_trade = _open_trade(
                    "SELL", latest_bottom, latest_top, symbol, trading_symbol, expiry,
                    instrument_class, lot_size, date, latest_bottom, str(uuid.uuid4()),
                )
            continue

        # IN A POSITION: trail the stop forward, then check for a breach.
        if open_trade.direction == "BUY" and latest_bottom is not None:
            new_stop = max(open_trade.active_stop, latest_bottom)
            if new_stop != open_trade.active_stop:
                open_trade.active_stop = new_stop
                open_trade.stop_history.append((date, new_stop))
        elif open_trade.direction == "SELL" and latest_top is not None:
            new_stop = min(open_trade.active_stop, latest_top)
            if new_stop != open_trade.active_stop:
                open_trade.active_stop = new_stop
                open_trade.stop_history.append((date, new_stop))

        if _is_stop_hit(open_trade.direction, open_trade.active_stop, close):
            # Exit fills AT the stop level (the resting stop order), not at
            # the close that breached it — and the reversal enters at that
            # very same level, so exit price == next entry price exactly.
            stop_level = open_trade.active_stop
            event_id = open_trade.reversal_event_id
            open_trade.exit_date = date
            open_trade.exit_price = stop_level
            open_trade.exit_reason = "TRAILING_STOP_REVERSAL"
            trades.append(open_trade)

            reverse_direction: Direction = "SELL" if open_trade.direction == "BUY" else "BUY"
            new_system_point = stop_level  # the breached level becomes the new System Point
            new_initial_sl = latest_top if reverse_direction == "SELL" else latest_bottom
            if new_initial_sl is None:
                # No opposite-side confirmed level exists yet to anchor the
                # reversed trade's stop — stay flat rather than fabricate one.
                open_trade = None
                continue

            signals.append(Signal(reverse_direction, i, date, new_system_point, stop_level, event_id))
            open_trade = _open_trade(
                reverse_direction, new_system_point, new_initial_sl, symbol, trading_symbol,
                expiry, instrument_class, lot_size, date, stop_level, event_id,
            )

    if open_trade is not None:
        open_trade.exit_date = dates[-1]
        open_trade.exit_price = closes[-1]
        open_trade.exit_reason = "END_OF_BACKTEST"
        trades.append(open_trade)

    return signals, trades
