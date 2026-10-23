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

from app.strategy.top_bottom.gap_protocol import (
    Bar15,
    is_gap_past_system_point,
    is_gap_through_stop,
    resolve_gap_entry,
    resolve_gap_exit,
)
from app.strategy.top_bottom.models import Direction, InstrumentClass, Signal, Trade
from app.ops.stats import tracked
from app.strategy.top_bottom.swing_detection import detect_confirmed_swings, swings_confirmed_by

# The product's one and only confirmation window — see the module
# docstring and the spec's explicit prohibition on 2/2, 3/3, 5/5 or any
# other wider/minimum-size pivot rule.
SWING_LEFT_BARS = 1
SWING_RIGHT_BARS = 1


BREAKEVEN_TRIGGER_PCT = 0.02
BREAKEVEN_EVENT = "2% PROFIT → BREAKEVEN SL"
NEW_SP_EVENT = "NEW SYSTEM POINT confirmed before +2% → structural trailing only"
BREAKEVEN_EXIT = "BREAKEVEN_EXIT_AFTER_2_PERCENT_PROFIT"


def _maybe_activate_breakeven(
    trade: Trade, i: int, date: dt.datetime, high: float, low: float, all_swings: list, stop_hit_first: bool,
) -> None:
    """
    2% profit -> breakeven stop. BUY: bar HIGH reaches entry*1.02; SELL: bar
    LOW reaches entry*0.98 (intrabar, on the bar's own timeframe). Fires only
    if (a) it has not fired yet, (b) no NEW system point appeared since
    entry — a swing in the trade's direction beyond the entry system point,
    confirmed by the PREVIOUS bar (a swing confirmed on this bar needs this
    bar's close, which the intrabar threshold cannot know) — and (c) the
    bar did not already breach the existing stop: OHLC cannot order the two
    events, so the stop is conservatively assumed to hit first. The stop
    only ever improves; the trailing logic's max/min keeps it from being
    undone later.
    """
    if trade.breakeven_date is not None or stop_hit_first or trade.entry_index < 0 or i <= trade.entry_index:
        return
    buy = trade.direction == "BUY"
    threshold = trade.entry_price * (1 + BREAKEVEN_TRIGGER_PCT if buy else 1 - BREAKEVEN_TRIGGER_PCT)
    if (high < threshold) if buy else (low > threshold):
        return
    kind = "TOP" if buy else "BOTTOM"
    for sw in all_swings:
        if sw.kind != kind or sw.confirmed_index <= trade.entry_index or sw.confirmed_index > i - 1:
            continue
        if (sw.price > trade.system_point) if buy else (sw.price < trade.system_point):
            return  # a new system point appeared first: existing trailing rules govern
    be = max(trade.active_stop, trade.entry_price) if buy else min(trade.active_stop, trade.entry_price)
    trade.breakeven_date = date
    trade.events.append((date, BREAKEVEN_EVENT))
    if be != trade.active_stop:
        trade.active_stop = be
        trade.stop_history.append((date, be))


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


def res_handled_gap(pre_top, pre_bottom, prev_close, day_open, day_bars) -> bool:
    """True when today is a gap-past-system-point day with a usable first
    candle but no breakout — the close-based entry must then be skipped."""
    for direction, level in (("BUY", pre_top), ("SELL", pre_bottom)):
        if level is None:
            continue
        if is_gap_past_system_point(direction, prev_close, day_open, level):
            return resolve_gap_entry(direction, day_bars).status == "NO_ENTRY"
    return False


@tracked("top_bottom.daily_backtest")
def run_top_bottom_backtest(
    symbol: str,
    trading_symbol: str,
    expiry: str,
    instrument_class: InstrumentClass,
    lot_size: int,
    dates: list[dt.datetime],
    closes: list[float],
    opens: list[float],
    intraday: dict[dt.date, list[Bar15]] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> tuple[list[Signal], list[Trade]]:
    """
    Pure function: bar arrays in, signals + trades out. Entries, exits and
    the trailing stop are CLOSE-price events (spec section 11), with ONE
    exception: when `intraday` (date -> 15-minute bars) is supplied, a day
    whose OPEN gaps past a system point / through a structural stop is
    handled by the 15-minute gap protocol (gap_protocol.py). Days with no
    intraday data, and every non-gap day, behave exactly as before; with
    `intraday=None` the engine is unchanged.

    `highs`/`lows` (optional) enable the 2%-profit -> breakeven stop rule
    (see _maybe_activate_breakeven); without them it never fires.
    """
    if len(dates) != len(closes):
        raise ValueError("dates and closes must be the same length")

    all_swings = detect_confirmed_swings(dates, closes, SWING_LEFT_BARS, SWING_RIGHT_BARS)

    signals: list[Signal] = []
    trades: list[Trade] = []
    open_trade: Trade | None = None
    sw_ptr = 0
    cur_top: float | None = None
    cur_bottom: float | None = None
    cur_top_ci = cur_bot_ci = -1  # confirmation index of the latest Top / Bottom
    # After a 2%-breakeven exit the strategy stays FLAT until a System Point confirmed
    # AFTER that exit (index > block_after) is crossed; every earlier point is consumed.
    block_after: int | None = None

    for i in range(len(closes)):
        date = dates[i]
        close = closes[i]

        # Latest confirmed Top/Bottom as of bar i, advanced incrementally
        # (same result as filtering swings_confirmed_by(all_swings, i), but
        # O(1) per bar so minute-level series stay fast).
        while sw_ptr < len(all_swings) and all_swings[sw_ptr].confirmed_index <= i:
            sw = all_swings[sw_ptr]
            if sw.kind == "TOP":
                cur_top, cur_top_ci = sw.price, sw.confirmed_index
            else:
                cur_bottom, cur_bot_ci = sw.price, sw.confirmed_index
            if open_trade is not None and open_trade.new_sp_date is None and open_trade.breakeven_date is None:
                same_dir = (sw.kind == "TOP") if open_trade.direction == "BUY" else (sw.kind == "BOTTOM")
                beyond = (sw.price > open_trade.system_point) if open_trade.direction == "BUY" else (sw.price < open_trade.system_point)
                if same_dir and beyond and (open_trade.entry_index < 0 or sw.confirmed_index > open_trade.entry_index):
                    open_trade.new_sp_date, open_trade.new_sp_price = sw.confirmed_date, sw.price
                    open_trade.events.append((sw.confirmed_date, NEW_SP_EVENT))
            sw_ptr += 1
        if open_trade is not None:
            block_after = None

        if open_trade is not None and open_trade.entry_index < 0:
            open_trade.entry_index = i - 1  # opened on the previous bar

        day_bars = intraday.get(date.date()) if intraday and i > 0 else None
        if day_bars:
            pre = swings_confirmed_by(all_swings, i - 1)
            pre_top = next((x.price for x in reversed(pre) if x.kind == "TOP"), None)
            pre_bottom = next((x.price for x in reversed(pre) if x.kind == "BOTTOM"), None)
            pre_top_ci = next((x.confirmed_index for x in reversed(pre) if x.kind == "TOP"), -1)
            pre_bot_ci = next((x.confirmed_index for x in reversed(pre) if x.kind == "BOTTOM"), -1)
            if block_after is not None:  # consumed points are not eligible for a gap entry either
                if pre_top_ci <= block_after:
                    pre_top = None
                if pre_bot_ci <= block_after:
                    pre_bottom = None

            if open_trade is not None:
                direction = open_trade.direction
                if is_gap_through_stop(direction, opens[i], open_trade.active_stop):
                    res = resolve_gap_exit(direction, day_bars)
                    if res.status == "GAP_EXITED":
                        open_trade.exit_date = res.exit_time
                        open_trade.exit_price = res.exit_level
                        open_trade.exit_reason = "GAP_EXIT_15M"
                        open_trade.gap_state = "GAP_EXITED"
                        open_trade.gap_first_high = res.first_candle.high
                        open_trade.gap_first_low = res.first_candle.low
                        trades.append(open_trade)
                        # No automatic reverse: stay flat; a fresh opposite
                        # entry needs the normal system trigger from the
                        # next bar on.
                        open_trade = None
                        continue
                    if res.status == "GAP_HELD":
                        open_trade.gap_state = "GAP_HELD"
                        open_trade.gap_first_high = res.first_candle.high
                        open_trade.gap_first_low = res.first_candle.low
                        open_trade.protective_sl = res.protective_sl
                        open_trade.protective_sl_date = date.date()
                        if highs is not None and lows is not None:
                            _maybe_activate_breakeven(open_trade, i, date, highs[i], lows[i], all_swings, False)
                        # Structural stop is suspended for today only; it
                        # still trails and is evaluated again from tomorrow.
                        if direction == "BUY":
                            if cur_bottom is not None and max(open_trade.active_stop, cur_bottom) != open_trade.active_stop:
                                open_trade.active_stop = cur_bottom
                                open_trade.stop_history.append((date, cur_bottom))
                        else:
                            if cur_top is not None and min(open_trade.active_stop, cur_top) != open_trade.active_stop:
                                open_trade.active_stop = cur_top
                                open_trade.stop_history.append((date, cur_top))
                        continue
                    # NO_FIRST_CANDLE: incomplete intraday data -> fall back
                    # to the ordinary close-based rules below.
            elif pre_top is not None and pre_bottom is not None:
                for direction, level, anchor in (("BUY", pre_top, pre_bottom), ("SELL", pre_bottom, pre_top)):
                    if level is None or not is_gap_past_system_point(direction, closes[i - 1], opens[i], level):
                        continue
                    res = resolve_gap_entry(direction, day_bars)
                    if res.status == "NO_FIRST_CANDLE":
                        break
                    if res.status == "TRIGGERED":
                        signals.append(Signal(direction, i, res.entry_time, level, res.entry_level, None))
                        open_trade = _open_trade(
                            direction, level, anchor, symbol, trading_symbol, expiry,
                            instrument_class, lot_size, res.entry_time, res.entry_level, str(uuid.uuid4()),
                        )
                        open_trade.gap_state = "GAP_ENTRY"
                        open_trade.gap_first_high = res.first_candle.high
                        open_trade.gap_first_low = res.first_candle.low
                    # Gap day handled (entered, or no breakout today): the
                    # close-based entry must not fire off the same gap.
                    break
                if open_trade is not None and open_trade.gap_state == "GAP_ENTRY" and open_trade.entry_date.date() == date.date():
                    continue
                if res_handled_gap(pre_top, pre_bottom, closes[i - 1], opens[i], day_bars):
                    continue

        latest_top, latest_bottom = cur_top, cur_bottom

        if open_trade is None:
            # FLAT: watch for a breakout above the latest Top or a
            # breakdown below the latest Bottom — whichever happens first.
            top_ok = block_after is None or cur_top_ci > block_after
            bot_ok = block_after is None or cur_bot_ci > block_after
            if latest_top is not None and top_ok and close > latest_top:
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
            elif latest_bottom is not None and bot_ok and close < latest_bottom:
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

        if highs is not None and lows is not None:
            _maybe_activate_breakeven(
                open_trade, i, date, highs[i], lows[i], all_swings,
                _is_stop_hit(open_trade.direction, open_trade.active_stop, close),
            )

        if highs is not None and lows is not None and not _is_stop_hit(open_trade.direction, open_trade.active_stop, close):
            # Information only (no decision changes): the bar's wick went through the stop but the CLOSE held.
            # The close-based daily engine ignores it by design; Intraday (1-min) mode processes the touch.
            ext = lows[i] if open_trade.direction == "BUY" else highs[i]
            if (ext < open_trade.active_stop) if open_trade.direction == "BUY" else (ext > open_trade.active_stop):
                open_trade.events.append((date, f"WICK THROUGH STOP {open_trade.active_stop:,.2f} (close held; Intraday mode processes the touch)"))

        if _is_stop_hit(open_trade.direction, open_trade.active_stop, close):
            # Exit fills AT the stop level (the resting stop order), not at
            # the close that breached it — and the reversal enters at that
            # very same level, so exit price == next entry price exactly.
            stop_level = open_trade.active_stop
            event_id = open_trade.reversal_event_id
            open_trade.exit_date = date
            open_trade.exit_price = stop_level
            if open_trade.breakeven_date is not None and stop_level == open_trade.entry_price:
                # The 2%-profit BREAKEVEN stop (not a structurally better one) was hit: close, stay
                # FLAT, never reverse, and wait for a System Point confirmed after this exit.
                open_trade.exit_reason = BREAKEVEN_EXIT
                trades.append(open_trade)
                open_trade = None
                block_after = i
                continue
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
