"""
Intraday execution of the Top-Bottom system (replay "Intraday 1-min" mode).

Responsibilities are split exactly as the daily strategy defines them:
  - DAILY candles own the structure. System points (latest confirmed Top /
    Bottom) come from the unchanged `detect_confirmed_swings` on daily closes
    and are only used once confirmed: on trading day D the levels known are
    those confirmed by D-1's close. Nothing from D itself is used before D
    has closed, so there is no look-ahead.
  - 1-MINUTE candles drive the playback: every minute is evaluated in order
    for system-point crossings, stop-loss hits, breakeven and exits.
  - The first 15-MINUTE candle (09:15-09:30, built from the 1-minute candles
    of that window) is used ONLY when the existing gap protocol triggers
    (gap_protocol.is_gap_through_stop / is_gap_past_system_point).

Rules reused unchanged from the daily engine (backtest.py):
  - BUY system point = latest confirmed Top, SELL = latest confirmed Bottom;
    initial stop = the opposite latest swing; no entry without that anchor.
  - The stop only ever trails forward (BUY up / SELL down) when a new opposite
    swing confirms - at the next day's open here, since swings confirm on
    daily closes.
  - ALWAYS-REVERSING: when a stop is hit the position exits and the opposite
    side is entered at the same price (the stop level becomes the new SELL/BUY
    system point), if the opposite anchor exists. After a GAP exit there is no
    automatic reverse - a fresh entry needs a genuine system-point crossing.
  - 2% profit -> breakeven stop (intrabar high/low), disabled when a new
    same-direction swing confirmed after entry.

Execution rules (decided with the user, 2026-10-10):
  - A level is crossed when the 1-minute high/low trades strictly beyond it.
  - Fill = the level, unless the candle OPENED beyond it (a gap through), in
    which case the candle's open is the fill. Signal price (the level) and
    fill price are both recorded.
  - Within ONE candle the intrabar order is unknown. Conservative rule: the
    adverse move for the open position is assumed to come first (BUY: low,
    SELL: high). A reversal made inside that candle is then exposed to the
    candle's other extreme. At most two stop-outs are processed per candle;
    any such chain is flagged `ambiguous`. A candle's high and low can each
    trigger only one thing on its assumed path (an entry made at the high
    cannot meet that same high again as a stop). A stop is always checked
    before breakeven. If a flat candle touches both system points, the nearer one to
    the candle's open is taken first (flagged ambiguous).
  - Whipsaw = a position entered and stopped out on the same trading day.
No slippage/brokerage is applied here.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from app.strategy.top_bottom.gap_protocol import (
    FirstCandle,
    is_gap_past_system_point,
    is_gap_through_stop,
)
from app.strategy.top_bottom.models import Trade
from app.ops.stats import tracked
from app.strategy.top_bottom.swing_detection import detect_confirmed_swings

BREAKEVEN_TRIGGER_PCT = 0.02
BREAKEVEN_EVENT = "2% PROFIT → BREAKEVEN SL"
WHIPSAW_EVENT = "WHIPSAW (entered and stopped out the same day)"
NEW_SP_EVENT = "NEW SYSTEM POINT confirmed before +2% \u2192 structural trailing only"
BREAKEVEN_EXIT = "BREAKEVEN_EXIT_AFTER_2_PERCENT_PROFIT"
ENTRY_DAY_STOP_EXIT = "ENTRY_DAY_STOP_EXIT"
ENTRY_DAY_STOP_PCT = 0.01   # the live engine passes this: entry-day stop = entry price -/+ 1%
CAPPED_STOP_EXIT = "CAPPED_1_PERCENT_STOP_EXIT"
STRUCT_CAP_DISTANCE = 0.02  # structural stop this far (or farther) from the entry price -> the 1% stop stays instead
FIRST_15_START, FIRST_15_END = dt.time(9, 15), dt.time(9, 30)


@dataclass(frozen=True)
class Minute:
    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float


@dataclass
class IntradayInitial:
    """State at the first replay minute, reconstructed from earlier history: an open position (with its
    stop / breakeven status) and/or the waiting state after a breakeven exit (block_after)."""
    trade: Trade | None = None
    block_after: int | None = None


@dataclass
class IntradayResult:
    trades: list[Trade]
    levels: list[tuple[dt.datetime, float | None, float | None]]  # day start, latest Top, latest Bottom
    notes: list[dict]  # plain-words gap/15-minute rule events, each with the time it becomes known
    flags: list[dict]  # data-quality flags per trading day
    blocked: list[dict] = field(default_factory=list)  # entries/reversals refused by the optional separation filter


def _f(v: float) -> str:
    return f"{v:,.2f}"


@tracked("top_bottom.run_intraday")
def run_intraday(
    symbol: str,
    daily_dates: list[dt.datetime],
    daily_closes: list[float],
    minutes: list[Minute],
    initial: IntradayInitial | None = None,
    first15: dict[dt.date, FirstCandle] | None = None,
    min_separation: float | None = None,
    entry_day_stop_pct: float | None = None,
    struct_cap_distance: float | None = None,
    roll_in: str | None = None,
    breakeven_pct: float | None = None,       # None = the index rule (2%); stocks pass 3%
) -> IntradayResult:
    """Pure function: completed daily history + 1-minute candles in, trades out.
    `daily_*` must contain only daily candles of days BEFORE or EQUAL to the
    last minute day; a day's own daily candle is never used on that day.

    entry_day_stop_pct (optional, OFF when None): on the day a position is ENTERED (including a stop-and-reverse entry) its stop is
    exactly entry -/+ this fraction (BUY below, SELL above), replacing the structural stop for that day only. If it is hit the
    position closes (ENTRY_DAY_STOP_EXIT) and the strategy stays FLAT, like after a breakeven exit: no reverse, and a new entry
    needs a System Point confirmed after the exit. From the next day the ordinary structural / trailing stop applies again.

    roll_in ("BUY" / "SELL", optional): the position of the PREVIOUS contract is being rolled into this one. On the very first minute of the first
    day in `minutes` (if still flat) a fresh entry in that direction is taken at that minute's open, with this contract's OWN System Points:
    the stop is this contract's latest opposite point, the 2% breakeven counts from this entry, and the entry-day 1% stop applies. If this
    contract has no such System Point yet, no entry is taken (no guessing).

    struct_cap_distance (optional, needs entry_day_stop_pct): from the day AFTER entry, every day, if the structural stop (latest opposite
    System Point, trailed as usual) is this fraction or MORE away from the ENTRY price, the stop stays at entry -/+ entry_day_stop_pct
    instead (fixed, never trails); once the structural stop is closer than that, it takes over. If the 1% stop is hit the position closes
    (CAPPED_1_PERCENT_STOP_EXIT) and the strategy stays FLAT until a System Point confirmed after the exit is crossed (no reverse)."""
    swings = detect_confirmed_swings(daily_dates, daily_closes, 1, 1)
    by_day: dict[dt.date, list[Minute]] = {}
    for m in minutes:
        by_day.setdefault(m.ts.date(), []).append(m)
    daily_day = [d.date() for d in daily_dates]

    trades: list[Trade] = []
    levels: list[tuple[dt.datetime, float | None, float | None]] = []
    notes: list[dict] = []
    flags: list[dict] = []
    blocked: list[dict] = []
    blocked_seen: set[tuple[str, str, str]] = set()
    open_t: Trade | None = initial.trade if initial else None
    sw_ptr, cur_top, cur_bot = 0, None, None
    rolled_done = False
    cur_top_ci = cur_bot_ci = -1  # confirmation index of the latest Top / Bottom
    # After a 2%-breakeven exit the strategy stays FLAT until a System Point confirmed AFTER that
    # exit (confirmed_index > block_after) is crossed; every point existing at the exit is consumed.
    block_after: int | None = initial.block_after if initial else None

    def new_trade(direction, level, fill, anchor, ts, day_i, gap_state=None) -> Trade:
        t = Trade(
            direction=direction, symbol=symbol, trading_symbol=symbol, expiry="", instrument_class="INDEX", lot_size=0,
            signal_date=ts, entry_date=ts, entry_price=fill, system_point=level, initial_sl=anchor, active_stop=anchor,
            reversal_event_id="", stop_history=[(ts, anchor)], entry_index=day_i, signal_price=level, gap_state=gap_state,
        )
        t.struct_stop = anchor
        if entry_day_stop_pct:
            t.entry_day_sl = round(fill * (1 - entry_day_stop_pct) if direction == "BUY" else fill * (1 + entry_day_stop_pct), 2)
            t.active_stop = t.entry_day_sl
            t.stop_history.append((ts, t.entry_day_sl))
            t.events.append((ts, f"ENTRY-DAY STOP {entry_day_stop_pct * 100:g}%: stop {t.entry_day_sl:,.2f} for today only (structural stop {anchor:,.2f} applies from the next day)"))
        return t

    def close_trade(t: Trade, ts: dt.datetime, fill: float, signal: float | None, reason: str, ambiguous: bool = False) -> None:
        t.exit_date, t.exit_price, t.exit_reason, t.exit_signal_price = ts, fill, reason, signal
        t.ambiguous = t.ambiguous or ambiguous
        if reason == "STOP_LOSS_REVERSAL" and ts.date() == t.entry_date.date():
            t.events.append((ts, WHIPSAW_EVENT))
        trades.append(t)

    for day in sorted(by_day):
        day_min = sorted(by_day[day], key=lambda m: m.ts)
        i = sum(1 for d in daily_day if d < day)  # daily candles completed before this day
        while sw_ptr < len(swings) and swings[sw_ptr].confirmed_index <= i - 1:
            sw = swings[sw_ptr]
            if sw.kind == "TOP":
                cur_top, cur_top_ci = sw.price, sw.confirmed_index
            else:
                cur_bot, cur_bot_ci = sw.price, sw.confirmed_index
            sw_ptr += 1
        top, bot = cur_top, cur_bot
        top_ok = block_after is None or cur_top_ci > block_after   # False while the Top is a consumed point
        bot_ok = block_after is None or cur_bot_ci > block_after
        first = day_min[0]
        levels.append((first.ts, top, bot))
        # OPTIONAL filter (off unless min_separation > 0). Compares ONLY the latest Top and latest Bottom that are
        # already confirmed at this day's open (the same points every decision today uses): distance = |Top - Bottom|
        # in index points. Too close -> no NEW entry or reversal is taken (exits are never blocked). It does not
        # alter, delete or re-confirm any System Point and uses no later data.
        sep = abs(top - bot) if (top is not None and bot is not None) else None
        too_close = bool(min_separation) and sep is not None and sep < min_separation

        def block(c, kind: str, side: str, price: float | None) -> None:
            key = (day.isoformat(), kind, side)
            if kind != "REVERSAL" and key in blocked_seen:   # an ENTRY attempt repeats every minute: record once per day and side
                return
            blocked_seen.add(key)
            blocked.append({"time": c.ts.isoformat(), "date": day.isoformat(), "kind": kind, "side": side, "price": price,
                            "separation": round(sep, 2), "threshold": min_separation, "top": top, "bottom": bot})
        prev_close = daily_closes[i - 1] if i > 0 else None
        day_open = first.open

        flag = {"date": day.isoformat(), "minutes": len(day_min), "time": day_min[-1].ts.isoformat(), "issues": []}
        window = [m for m in day_min if FIRST_15_START <= m.ts.time() < FIRST_15_END]
        first15_ok = len(window) == 15 and first.ts.time() == FIRST_15_START
        fc = FirstCandle(high=max(m.high for m in window), low=min(m.low for m in window), open=window[0].open) if first15_ok else None
        fc_source = "derived from the genuine 1-minute candles"
        stored = (first15 or {}).get(day)
        if stored is not None:  # prefer the genuine stored 15-minute candle for the existing gap protocol
            fc, first15_ok, fc_source = stored, True, "stored 15-minute candle"
        if len(day_min) < 375:
            flag["issues"].append(f"{375 - len(day_min)} of 375 one-minute candles missing")

        if open_t is not None and open_t.new_sp_date is None and open_t.breakeven_date is None:
            kind_sp = "TOP" if open_t.direction == "BUY" else "BOTTOM"
            for sw in swings:
                if sw.kind != kind_sp or sw.confirmed_index <= open_t.entry_index or sw.confirmed_index > i - 1:
                    continue
                if (sw.price > open_t.system_point) if open_t.direction == "BUY" else (sw.price < open_t.system_point):
                    open_t.new_sp_date, open_t.new_sp_price = sw.confirmed_date, sw.price
                    open_t.events.append((sw.confirmed_date, NEW_SP_EVENT))
                    break
        cap_rule = open_t is not None and bool(struct_cap_distance) and open_t.entry_day_sl is not None and open_t.entry_date.date() < day
        if cap_rule:
            # capped-stop rule: track the structural stop on its own, then pick the stop for today (structural, or entry +/- 1% if it is too far)
            t = open_t
            buy_t = t.direction == "BUY"
            struct = t.struct_stop if t.struct_stop is not None else t.initial_sl
            if buy_t and bot is not None and bot > struct:
                struct = bot
            elif not buy_t and top is not None and top < struct:
                struct = top
            t.struct_stop = struct
            far = abs(t.entry_price - struct) / t.entry_price >= struct_cap_distance
            eff = t.entry_day_sl if far else struct
            if t.breakeven_date is not None:                                 # the 2% breakeven stop is never loosened
                eff = max(eff, t.entry_price) if buy_t else min(eff, t.entry_price)
            if far != t.capped and t.breakeven_date is None:
                t.events.append((first.ts, (f"STOP CAPPED AT {entry_day_stop_pct * 100:g}%: the structural stop {struct:,.2f} is {abs(t.entry_price - struct) / t.entry_price * 100:.2f}% from entry "
                                            f"(>= {struct_cap_distance * 100:g}%), so the stop stays at {t.entry_day_sl:,.2f}") if far else
                                 f"STOP BACK TO STRUCTURAL: the structural stop {struct:,.2f} is now within {struct_cap_distance * 100:g}% of entry"))
            t.capped = far and t.breakeven_date is None
            if eff != t.active_stop:
                t.active_stop = eff
                t.stop_history.append((first.ts, eff))
        # an entry-day stop lives for ONE day: from the next day the structural stop (then the usual trailing) applies again
        if open_t is not None and not cap_rule and open_t.entry_day_sl is not None and open_t.entry_date.date() < day and open_t.active_stop == open_t.entry_day_sl:
            open_t.active_stop = open_t.initial_sl
            open_t.stop_history.append((first.ts, open_t.initial_sl))
        # trail the stop with swings confirmed through the previous day
        if open_t is not None and not cap_rule:
            if open_t.direction == "BUY" and bot is not None and bot > open_t.active_stop:
                open_t.active_stop = bot
                open_t.stop_history.append((first.ts, bot))
            elif open_t.direction == "SELL" and top is not None and top < open_t.active_stop:
                open_t.active_stop = top
                open_t.stop_history.append((first.ts, top))

        if roll_in and open_t is None and not rolled_done:
            rolled_done = True
            anchor_r = bot if roll_in == "BUY" else top
            level_r = top if roll_in == "BUY" else bot
            if anchor_r is not None:
                open_t = new_trade(roll_in, level_r if level_r is not None else first.open, first.open, anchor_r, first.ts, i)
                open_t.rolled_in = True
                open_t.events.append((first.ts, f"ROLL IN: {roll_in} continued in this contract at {_f(first.open)} (stop {_f(anchor_r)} from this contract's own System Point)"))
                block_after = None
            else:
                notes.append({"kind": "ROLL_NO_ANCHOR", "side": roll_in, "pct": 0.0, "time": first.ts.isoformat(), "title": "Roll skipped: no System Point",
                              "detail": f"The {roll_in} position could not be continued in this contract because it has no confirmed {'Bottom' if roll_in == 'BUY' else 'Top'} yet. The strategy stays flat."})
        # ---- does the gap protocol apply today? ------------------------------
        mode, mode_side, mode_level, mode_anchor = None, None, None, None
        gap_pct = ((day_open - prev_close) / prev_close * 100.0) if prev_close else 0.0
        if prev_close is not None:
            if open_t is not None and is_gap_through_stop(open_t.direction, day_open, open_t.active_stop):
                if first15_ok:
                    mode, mode_side = "exit", open_t.direction
                    mode_level = open_t.active_stop
                else:
                    flag["issues"].append("gap through the stop but the 09:15-09:30 candle is incomplete: gap protocol NOT applied, ordinary rules used")
            elif open_t is None and top is not None and bot is not None:
                for side, lvl, anchor, ok in (("BUY", top, bot, top_ok), ("SELL", bot, top, bot_ok)):
                    if ok and too_close and is_gap_past_system_point(side, prev_close, day_open, lvl):
                        block(first, "GAP_ENTRY", side, lvl)
                        break
                    if ok and is_gap_past_system_point(side, prev_close, day_open, lvl):
                        if first15_ok:
                            mode, mode_side, mode_level, mode_anchor = "entry", side, lvl, anchor
                        else:
                            flag["issues"].append("gap past a system point but the 09:15-09:30 candle is incomplete: gap protocol NOT applied, ordinary rules used")
                        break
        if flag["issues"]:
            flags.append(flag)
        way = "up" if gap_pct > 0 else "down"
        lv15 = f"first 15-min candle (09:15-09:30; high {_f(fc.high)}, low {_f(fc.low)}; {fc_source})" if fc else ""
        gap_resolved = False

        def breakeven(c: Minute) -> None:
            t = open_t
            if t is None or t.breakeven_date is not None:
                return
            buy = t.direction == "BUY"
            be = BREAKEVEN_TRIGGER_PCT if breakeven_pct is None else breakeven_pct
            thr = t.entry_price * (1 + be if buy else 1 - be)
            if (c.high < thr) if buy else (c.low > thr):
                return
            kind = "TOP" if buy else "BOTTOM"
            for sw in swings:
                if sw.kind != kind or sw.confirmed_index <= t.entry_index or sw.confirmed_index > i - 1:
                    continue
                if (sw.price > t.system_point) if buy else (sw.price < t.system_point):
                    return  # a new system point appeared first: existing trailing rules govern
            be = max(t.active_stop, t.entry_price) if buy else min(t.active_stop, t.entry_price)
            t.breakeven_date = c.ts
            t.events.append((c.ts, BREAKEVEN_EVENT))
            if be != t.active_stop:
                t.active_stop = be
                t.stop_history.append((c.ts, be))

        for c in day_min:
            clock = c.ts.time()
            used_high = used_low = False  # a candle's high/low can each be "used" once on its assumed path
            # ------- gap decision pending: nothing fires before 09:30 ---------
            if mode is not None and clock < FIRST_15_END:
                continue
            # ------- GAP EXIT mode: ordinary stop suspended, 15-min level rules
            if mode == "exit" and open_t is not None:
                buy = open_t.direction == "BUY"
                broke = (c.low < fc.low) if buy else (c.high > fc.high)
                if broke:
                    level = fc.low if buy else fc.high
                    fill = min(level, c.open) if buy else max(level, c.open)
                    notes.append({
                        "kind": "GAP_EXITED", "side": open_t.direction, "pct": round(gap_pct, 2), "time": c.ts.isoformat(),
                        "title": f"Gap {way} {abs(gap_pct):.2f}% - {open_t.direction} exited by 15-min rule",
                        "detail": (f"Market opened at {_f(day_open)} ({abs(gap_pct):.2f}% gap {way}), {'below' if buy else 'above'} the {open_t.direction} position's "
                                   f"structural stop {_f(mode_level)}. The ordinary stop was suspended for the day and the {lv15} was used instead. "
                                   f"At {clock.strftime('%H:%M')} price broke {'below the 15-min low' if buy else 'above the 15-min high'}, so the {open_t.direction} was "
                                   f"exited at {_f(fill)}. No reverse trade is taken automatically; a fresh entry needs a genuine system-point crossing."),
                    })
                    open_t.gap_state = "GAP_EXITED"
                    open_t.gap_first_high, open_t.gap_first_low = fc.high, fc.low
                    close_trade(open_t, c.ts, fill, level, "GAP_EXIT_15M")
                    open_t, mode, gap_resolved = None, None, True
                    continue  # the exit candle never also enters (conservative)
                breakeven(c)
                continue
            # ------- GAP ENTRY mode: wait for the 15-min level on the bypassed side
            if mode == "entry" and open_t is None:
                buy = mode_side == "BUY"
                hit = (c.high > fc.high) if buy else (c.low < fc.low)
                if hit:
                    level = fc.high if buy else fc.low
                    fill = max(level, c.open) if buy else min(level, c.open)
                    block_after = None
                    t = new_trade(mode_side, mode_level, fill, mode_anchor, c.ts, i, "GAP_ENTRY")
                    t.signal_price = level
                    t.gap_first_high, t.gap_first_low = fc.high, fc.low
                    open_t, mode, gap_resolved = t, None, True
                    used_high, used_low = buy, not buy
                    notes.append({
                        "kind": "GAP_ENTRY", "side": mode_side, "pct": round(gap_pct, 2), "time": c.ts.isoformat(),
                        "title": f"Gap {way} {abs(gap_pct):.2f}% - {mode_side} entry",
                        "detail": (f"Market opened at {_f(day_open)} ({abs(gap_pct):.2f}% gap {way}) and jumped past the {mode_side} system point {_f(mode_level)}. "
                                   f"The system waited for the {lv15}. At {clock.strftime('%H:%M')} price crossed {'above the 15-min high' if buy else 'below the 15-min low'}, "
                                   f"so {mode_side} was taken at {_f(fill)}. Structural stop kept at {_f(mode_anchor)}."),
                    })
                    # fall through: the entry candle may also touch the stop (ambiguous chain handled below)
                else:
                    # only the opposite side may still trigger normally; the bypassed side waits for the 15-min level
                    pass
            # ------- ordinary intraday processing -------------------------------
            stopouts = 0
            for _ in range(8):  # safety bound; real exit is via break
                if open_t is not None:
                    buy = open_t.direction == "BUY"
                    stop = open_t.active_stop
                    # adverse move first: BUY is exposed to the low, SELL to the high - unless that extreme already
                    # triggered something earlier on this candle's assumed path (it cannot be visited twice).
                    adverse = (c.low < stop and not used_low) if buy else (c.high > stop and not used_high)
                    if adverse:
                        gapped = stopouts == 0 and not (used_high or used_low) and ((c.open < stop) if buy else (c.open > stop))
                        fill = c.open if gapped else stop
                        if open_t.breakeven_date is not None and stop == open_t.entry_price:
                            # The 2%-profit BREAKEVEN stop was hit (not a structurally better stop): close, stay FLAT,
                            # never reverse, wait for a System Point confirmed after this exit.
                            close_trade(open_t, c.ts, fill, stop, BREAKEVEN_EXIT)
                            open_t = None
                            block_after = i - 1
                            if buy:
                                used_low = True
                            else:
                                used_high = True
                            break
                        if open_t.entry_day_sl is not None and stop == open_t.entry_day_sl and open_t.breakeven_date is None and (open_t.entry_date.date() == day or open_t.capped):
                            # the percentage stop (entry day, or capped while the structural stop is too far): close, stay FLAT, never reverse,
                            # wait for a System Point confirmed after this exit
                            close_trade(open_t, c.ts, fill, stop, ENTRY_DAY_STOP_EXIT if open_t.entry_date.date() == day else CAPPED_STOP_EXIT,
                                        ambiguous=open_t.entry_date == c.ts or stopouts > 0)
                            open_t = None
                            block_after = i - 1
                            if buy:
                                used_low = True
                            else:
                                used_high = True
                            break
                        same_candle = open_t.entry_date == c.ts or stopouts > 0
                        close_trade(open_t, c.ts, fill, stop, "STOP_LOSS_REVERSAL", ambiguous=same_candle)
                        stopouts += 1
                        if buy:
                            used_low = True
                        else:
                            used_high = True
                        rev = "SELL" if buy else "BUY"
                        anchor = top if rev == "SELL" else bot   # latest Top / Bottom, as in the daily engine
                        if anchor is None:
                            open_t = None
                            break
                        if too_close:   # filter ON and the points are too close: the stop-out stands, the REVERSAL is not taken
                            block(c, "REVERSAL", rev, stop)
                            open_t = None
                            break
                        t = new_trade(rev, stop, fill, anchor, c.ts, i)
                        t.ambiguous = t.entry_ambiguous = same_candle
                        open_t = t
                        continue
                    if (buy and not used_high) or (not buy and not used_low):
                        breakeven(c)  # only the unused favourable extreme can still count
                    break
                # flat: genuine system-point crossings only
                if mode == "entry" and not gap_resolved:
                    cand_buy = top is not None and bot is not None and (block_after is None or cur_top_ci > block_after) and mode_side != "BUY" and c.high > top and not used_high
                    cand_sell = top is not None and bot is not None and (block_after is None or cur_bot_ci > block_after) and mode_side != "SELL" and c.low < bot and not used_low
                else:
                    cand_buy = top is not None and bot is not None and (block_after is None or cur_top_ci > block_after) and c.high > top and not used_high
                    cand_sell = top is not None and bot is not None and (block_after is None or cur_bot_ci > block_after) and c.low < bot and not used_low
                if not (cand_buy or cand_sell):
                    break
                if too_close:   # a genuine crossing happened, but the opposing points are too close: no new entry
                    block(c, "ENTRY", "BUY" if cand_buy and not cand_sell else "SELL" if cand_sell and not cand_buy else "BUY/SELL", top if cand_buy else bot)
                    break
                both = cand_buy and cand_sell
                take_buy = cand_buy and (not both or abs(c.open - top) <= abs(c.open - bot))
                side = "BUY" if take_buy else "SELL"
                level, anchor = (top, bot) if take_buy else (bot, top)
                fill = max(level, c.open) if take_buy else min(level, c.open)
                t = new_trade(side, level, fill, anchor, c.ts, i)
                block_after = None  # a genuine new-System-Point entry ends the waiting state
                t.ambiguous = t.entry_ambiguous = both
                if take_buy:
                    used_high = True
                else:
                    used_low = True
                open_t = t
            # Information only (no decision changes): a fake-out leaves price back through the level we entered at.
            if open_t is not None and not open_t.carried_in and open_t.round_trip_date is None and open_t.entry_date < c.ts:
                back = (c.close < open_t.entry_price) if open_t.direction == "BUY" else (c.close > open_t.entry_price)
                if back:
                    open_t.round_trip_date = c.ts
                    open_t.events.append((c.ts, f"ROUND TRIP: 1-min close {c.close:,.2f} back through the {open_t.direction} entry level {open_t.entry_price:,.2f} "
                                                f"(position still held; stop {open_t.active_stop:,.2f})"))
            # end of ordinary processing

        # ---- end of day bookkeeping for gap modes still unresolved -------------
        if mode == "exit" and open_t is not None:
            buy = open_t.direction == "BUY"
            protective = fc.low if buy else fc.high
            open_t.gap_state = "GAP_HELD"
            open_t.gap_first_high, open_t.gap_first_low = fc.high, fc.low
            open_t.protective_sl, open_t.protective_sl_date = protective, day
            notes.append({
                "kind": "GAP_HELD", "side": open_t.direction, "pct": round(gap_pct, 2), "time": day_min[-1].ts.isoformat(),
                "title": f"Gap {way} {abs(gap_pct):.2f}% - {open_t.direction} held",
                "detail": (f"Market opened at {_f(day_open)} ({abs(gap_pct):.2f}% gap {way}), {'below' if buy else 'above'} the {open_t.direction} position's structural stop "
                           f"{_f(mode_level)}. The ordinary stop was suspended for the day and the {lv15} was used instead. Price never broke the "
                           f"{'15-min low' if buy else '15-min high'}, so the {open_t.direction} is held and today's protective stop was {_f(protective)} "
                           f"(valid for this day only; the structural stop applies again tomorrow)."),
            })
        elif mode == "entry" and open_t is None and not gap_resolved:
            notes.append({
                "kind": "GAP_NO_ENTRY", "side": mode_side, "pct": round(gap_pct, 2), "time": day_min[-1].ts.isoformat(),
                "title": f"Gap {way} {abs(gap_pct):.2f}% - no {mode_side} entry",
                "detail": (f"Market opened at {_f(day_open)} ({abs(gap_pct):.2f}% gap {way}) past the {mode_side} system point {_f(mode_level)}. "
                           f"The system waited for the {lv15} but price never crossed {'above the 15-min high' if mode_side == 'BUY' else 'below the 15-min low'}, so no entry was taken today."),
            })

    if open_t is not None:
        last = minutes[-1]
        open_t.exit_date, open_t.exit_price, open_t.exit_reason = last.ts, last.close, "END_OF_BACKTEST"
        trades.append(open_t)
    return IntradayResult(trades, levels, notes, flags, blocked)
