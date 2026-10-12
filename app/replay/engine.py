"""
Replay / backtest engine for the BankNifty INDEX simulator.

ONE strategy implementation: `run_strategy` calls the existing, unchanged
Top-Bottom engine (strategies/top_bottom/backtest.py, incl. the 15-minute gap
protocol and the 2%-profit -> breakeven stop) on stored index candles of the
chosen interval. Replay mode and full-backtest mode both read the same
`StrategyRun`; replay only decides how much of it is REVEALED at an index.

This is an INDEX-POINT signal simulation: results are BankNifty index points.
There is no lot size, no rupee P&L and no brokerage/costs, because an index is
not tradable. Optional slippage is also in points.

No look-ahead: the engine is causal and `snapshot` exposes only candles/events
up to the replay index (tests/replay/test_engine.py checks that changing
future candles never changes a snapshot, and that a truncated run equals the
revealed part of the full run).

Gap protocol: it needs a daily series plus 15-minute candles for the gap day,
so it only runs on the 1D interval and only for days that have stored 15m
candles. Intraday intervals use the plain Top-Bottom rules.

Rules NOT implemented because they are not in the current Top-Bottom source
are listed in RULE_GAPS and shown in the UI.
"""
from __future__ import annotations

import bisect
import datetime as dt
from dataclasses import dataclass, field

from app.replay.timeframes import INTERVALS, Candle, bars15_by_date, to_candles
import copy

from app.strategy.top_bottom.gap_protocol import FirstCandle
from app.strategy.top_bottom.intraday import IntradayInitial, Minute, run_intraday
from app.strategy.top_bottom.backtest import BREAKEVEN_EVENT, run_top_bottom_backtest
from app.strategy.top_bottom.models import Signal, Trade

SYMBOL = "BANKNIFTY"
GAP_MARK_PCT = 1.0  # opens this far from the prior close are flagged on the chart
UNIT = "BankNifty Index Points"

RULE_GAPS = [
    "2% gap exit-only rule is NOT implemented (not present in the current Top-Bottom source).",
    "Entry-day 1% stop-loss is NOT implemented (not present in the current Top-Bottom source).",
    "Only the structural stop, the 15-minute gap protocol (1D with stored 15m candles) and the 2%-profit breakeven stop are simulated.",
    "Close-based stop exits fill at the stop level; the gap protocol handles gaps only where it applies.",
    "Index-point simulation only: no futures contract, lot size, rupee P&L or brokerage is modelled.",
]


@dataclass
class StrategyRun:
    interval: str
    candles: list[Candle]
    signals: list[Signal]
    trades: list[Trade]
    slippage_points: float = 0.0
    dates: list[dt.datetime] = field(default_factory=list)
    gap_cummax: list[float] = field(default_factory=list)  # largest |open-prevclose| % up to each index
    gap15_days: list[dt.date] = field(default_factory=list)  # candle days that have stored 15m data (sorted)
    mode: str = "daily"  # "daily" (close-based classic) | "intraday" (1-minute execution on daily structure)
    levels: list = field(default_factory=list)  # intraday: (day start, latest Top, latest Bottom)
    intr_notes: list = field(default_factory=list)
    data_flags: list = field(default_factory=list)
    warnings: list = field(default_factory=list)  # shown to the user: reconstructed state, missing history, ...
    session: dict = field(default_factory=dict)   # the active replay range chosen by the backend
    quality: dict = field(default_factory=dict)   # date -> 1-minute completeness of that Daily session
    blocked: list = field(default_factory=list)   # entries/reversals refused by the optional separation filter
    min_separation: float | None = None           # the filter threshold in index points (None/0 = original strategy)


def run_strategy(
    rows: list[tuple], interval: str, rows_15m: list[tuple] | None = None, slippage_points: float = 0.0,
) -> StrategyRun:
    if interval not in INTERVALS:
        raise ValueError(f"Unsupported interval {interval}")
    candles = to_candles(rows)
    dates = [c.ts for c in candles]
    intraday = bars15_by_date(rows_15m) if interval == "1D" and rows_15m else None
    signals, trades = run_top_bottom_backtest(
        SYMBOL, SYMBOL, "", "INDEX", 0, dates, [c.close for c in candles], [c.open for c in candles],
        intraday, [c.high for c in candles], [c.low for c in candles],
    )
    cummax, best = [], 0.0
    for k, c in enumerate(candles):
        if k:
            best = max(best, abs(c.open - candles[k - 1].close) / candles[k - 1].close * 100)
        cummax.append(best)
    days = sorted({d.date() for d in dates} & set(intraday)) if intraday else []
    return StrategyRun(interval, candles, signals, trades, slippage_points, dates, cummax, days)


RULE_GAPS_INTRADAY = RULE_GAPS[:2] + [
    "Intraday mode: daily structure from confirmed daily swings (known from the previous close); 1-minute candles trigger entries/stops. A level is crossed when the 1-minute high/low trades strictly beyond it.",
    "Fill = the level, or the candle's open when it opened beyond the level (gap through). Signal price and fill price are both shown. No slippage or brokerage is applied.",
    "One candle cannot show the order of its high and low: the adverse move for the open position is assumed first and such trades are flagged AMBIGUOUS (max two stop-outs per candle).",
    "Intraday mode will NOT match the daily-close mode trade-for-trade: daily mode decides on closes, intraday mode on touches.",
    "Index-point simulation only: no futures contract, lot size, rupee P&L or brokerage is modelled.",
]


def reconstruct_state(daily_rows: list[tuple], first_day: dt.date) -> tuple[IntradayInitial | None, list[str]]:
    """Strategy state at the open of `first_day`, rebuilt from the Daily candles BEFORE it (no future data):
    runs the Daily-close engine over that history and hands over an open position (stop, breakeven status)
    or the waiting state after a breakeven exit. Always returns a user-visible warning describing what was assumed."""
    prior = [r for r in daily_rows if r[0].date() < first_day]
    if len(prior) < 3:
        return None, [f"Only {len(prior)} Daily candle(s) exist before {first_day}: not enough history to reconstruct earlier positions, so the run starts FLAT."]
    _, trs = run_top_bottom_backtest(
        SYMBOL, SYMBOL, "", "INDEX", 0, [r[0] for r in prior], [r[4] for r in prior], [r[1] for r in prior], None,
        [r[2] for r in prior], [r[3] for r in prior],
    )
    last = trs[-1] if trs else None
    basis = f"reconstructed from {len(prior)} Daily candles before {first_day} (close-based Daily semantics; the exact intraday fills of earlier days are not reproduced)"
    if last is not None and last.exit_reason == "END_OF_BACKTEST":
        t = copy.copy(last)
        t.exit_date = t.exit_price = t.exit_reason = None
        t.events, t.stop_history = list(last.events), list(last.stop_history)
        t.carried_in = True
        if t.entry_index < 0:
            t.entry_index = len(prior) - 1
        return IntradayInitial(trade=t), [
            f"Approximate warm-up. Position carried in: {t.direction} from {t.entry_date.date()} @ {t.entry_price:,.2f}, stop {t.active_stop:,.2f}"
            + (", breakeven active" if t.breakeven_date else "") + f" - {basis}."]
    if last is not None and last.exit_reason == "BREAKEVEN_EXIT_AFTER_2_PERCENT_PROFIT":
        idx = next(i for i, r in enumerate(prior) if r[0] == last.exit_date)
        return IntradayInitial(block_after=idx), [
            f"Approximate warm-up. Waiting state carried in: flat after a 2% breakeven exit on {last.exit_date.date()}; every earlier System Point is consumed - {basis}."]
    return None, [f"Approximate warm-up. No open position before {first_day} - {basis}."]


def run_intraday_strategy(daily_rows: list[tuple], minute_rows: list[tuple], slippage_points: float = 0.0, seed: bool = True) -> StrategyRun:
    """Intraday mode: structure from stored 1D candles, execution on stored 1-minute candles. The state before the
    first replay minute (open position, active stop, breakeven, waiting) is reconstructed from earlier Daily history."""
    candles = to_candles(minute_rows)
    mins = [Minute(c.ts, c.open, c.high, c.low, c.close) for c in candles]
    initial, warnings = reconstruct_state(daily_rows, minute_rows[0][0].date()) if seed else (None, [])
    res = run_intraday(SYMBOL, [r[0] for r in daily_rows], [r[4] for r in daily_rows], mins, initial)
    cummax, best = [], 0.0
    for k, c in enumerate(candles):
        if k and c.ts.date() != candles[k - 1].ts.date():
            best = max(best, abs(c.open - candles[k - 1].close) / candles[k - 1].close * 100)
        cummax.append(best)
    run = StrategyRun("1m", candles, [], res.trades, slippage_points, [c.ts for c in candles], cummax, [])
    run.mode, run.levels, run.intr_notes, run.data_flags = "intraday", res.levels, res.notes, res.flags
    run.warnings = warnings
    return run


def run_hybrid_strategy(
    daily_rows: list[tuple], minute_rows: list[tuple], slippage_points: float = 0.0,
    first15: dict[dt.date, FirstCandle] | None = None, min_separation: float | None = None,
) -> StrategyRun:
    """Daily candles are what the user SEES and steps through; stored 1-minute candles are processed inside each
    Daily session (hidden) for crossings, entries, stops, whipsaws and the gap rule. One replay step = one Daily candle,
    committed only after that session's minutes have been processed (the whole run is computed before playback)."""
    mday = sorted({r[0].date() for r in minute_rows})
    first_day, last_day = mday[0], mday[-1]
    session = [r for r in daily_rows if first_day <= r[0].date() <= last_day]
    have_daily = {r[0].date() for r in session}
    usable = [r for r in minute_rows if r[0].date() in have_daily]
    candles = to_candles(session)
    mins = [Minute(r[0], r[1], r[2], r[3], r[4]) for r in usable]
    initial, warnings = reconstruct_state(daily_rows, first_day)
    res = run_intraday(SYMBOL, [r[0] for r in daily_rows], [r[4] for r in daily_rows], mins, initial, first15, min_separation or None)
    flags = list(res.flags)
    have_min = {r[0].date() for r in usable}
    for c in candles:  # a Daily session without 1-minute data cannot have its intraday events processed
        if c.ts.date() not in have_min:
            flags.append({"date": c.ts.date().isoformat(), "minutes": 0, "time": dt.datetime.combine(c.ts.date(), dt.time(15, 29)).isoformat(),
                          "issues": ["no 1-minute data stored for this Daily session: its intraday touches (stops, entries, whipsaws) were NOT processed"]})
    dropped = sorted(d for d in mday if d not in have_daily)
    if dropped:
        warnings.append(f"1-minute data exists for {len(dropped)} day(s) without a stored Daily candle ({dropped[0]}...): those sessions are skipped, never aggregated into a Daily candle.")
    if flags:
        warnings.append(f"{len(flags)} Daily session(s) have incomplete 1-minute data (see the event log); whipsaw detection is not guaranteed for them.")
    cummax, best = [], 0.0
    for k, c in enumerate(candles):
        if k:
            best = max(best, abs(c.open - candles[k - 1].close) / candles[k - 1].close * 100)
        cummax.append(best)
    run = StrategyRun("1D", candles, [], res.trades, slippage_points, [c.ts for c in candles], cummax, [])
    run.mode, run.levels, run.intr_notes, run.data_flags = "hybrid", res.levels, res.notes, flags
    run.blocked, run.min_separation = res.blocked, (min_separation or None)
    run.warnings = warnings
    if run.min_separation:
        run.warnings.insert(0, f"FILTERED RUN: Minimum System Point Separation = {run.min_separation:g} index points is ON. Entries and reversals are blocked while the latest Top and Bottom are closer than that, so these results are NOT the original strategy results.")
    return run


def build_event_log(run: "StrategyRun", idx: int) -> list[dict]:
    """Chronological, exact-timestamp event log of everything the hidden 1-minute engine did on the Daily sessions
    revealed so far (<= idx). Types: SYSTEM_POINT_CROSSED, BUY_ENTRY, SELL_ENTRY, STOP_EXIT, REVERSAL_ENTRY, ROUND_TRIP,
    WHIPSAW_STOP_OUT, BREAKEVEN_ACTIVATED, BREAKEVEN_EXIT_AFTER_2_PERCENT_PROFIT, GAP_PROTOCOL, AMBIGUOUS,
    INTRADAY_DATA_INCOMPLETE (plus NEW_SYSTEM_POINT as information)."""
    out: list[dict] = []
    f = lambda v: f"{v:,.2f}"

    cur_trade: dict = {"n": None, "side": None}

    def add(when: dt.datetime, typ: str, text: str, price: float | None = None) -> None:
        out.append({"time": when.isoformat(), "date": when.date().isoformat(), "type": typ, "text": text, "price": price,
                    "trade": cur_trade["n"], "side": cur_trade["side"]})

    prev = None
    for number, t in enumerate(run.trades, start=1):
        cur_trade.update(n=number, side=t.direction)
        eb = _bar(run.dates, t.entry_date)
        if eb > idx:
            break
        side = t.direction
        if not t.carried_in:
            reversal = prev is not None and prev.exit_reason == "STOP_LOSS_REVERSAL" and prev.exit_date == t.entry_date
            if reversal:
                add(t.entry_date, "REVERSAL_ENTRY", f"{side} entered at {f(t.entry_price)} on the reversal of the stopped-out {prev.direction} (stop {f(t.system_point)}); stop {f(t.initial_sl)}", t.entry_price)
            elif t.gap_state == "GAP_ENTRY":
                add(t.entry_date, f"{side}_ENTRY", f"{side} entered at {f(t.entry_price)} after the gap rule (15-min level {f(t.signal_price or t.entry_price)}); stop {f(t.initial_sl)}", t.entry_price)
            else:
                add(t.entry_date, "SYSTEM_POINT_CROSSED", f"Price crossed the {side} System Point {f(t.system_point)}"
                    + (f" (filled at the candle open {f(t.entry_price)})" if t.entry_price != t.system_point else ""), t.system_point)
                add(t.entry_date, f"{side}_ENTRY", f"{side} entered at {f(t.entry_price)}; stop {f(t.initial_sl)}", t.entry_price)
            if t.entry_ambiguous:
                add(t.entry_date, "AMBIGUOUS", "One 1-minute candle could not show the order of its high and low; the adverse sequence was assumed (flagged, not guessed).")
        for when, label in t.events:
            if _bar(run.dates, when) > idx:
                continue
            if "BREAKEVEN" in label and label.startswith("2%"):
                add(when, "BREAKEVEN_ACTIVATED", f"+2% reached: stop moved to breakeven {f(t.entry_price)}")
            elif label.startswith("ROUND TRIP"):
                add(when, "ROUND_TRIP", label.replace("ROUND TRIP: ", "") + " - not an exit")
            elif label.startswith("NEW SYSTEM POINT"):
                add(when, "NEW_SYSTEM_POINT", f"New confirmed {side}-direction System Point {f(t.new_sp_price or 0)}: structural trailing only")
        done = t.exit_date is not None and t.exit_reason != "END_OF_BACKTEST" and _bar(run.dates, t.exit_date) <= idx
        if done:
            if t.exit_reason == "STOP_LOSS_REVERSAL":
                add(t.exit_date, "STOP_EXIT", f"{side} stopped out at {f(t.exit_price)} (stop {f(t.exit_signal_price or t.exit_price)})", t.exit_price)
                if t.exit_date.date() == t.entry_date.date():
                    add(t.exit_date, "WHIPSAW_STOP_OUT", f"{side} entered and stopped out on the same Daily session")
            elif t.exit_reason == "BREAKEVEN_EXIT_AFTER_2_PERCENT_PROFIT":
                add(t.exit_date, "BREAKEVEN_EXIT_AFTER_2_PERCENT_PROFIT", f"{side} closed at the breakeven stop {f(t.exit_price)}; flat, waiting for a NEW System Point (no reversal)", t.exit_price)
            if t.ambiguous and not t.entry_ambiguous:
                add(t.exit_date, "AMBIGUOUS", "The exit candle also reached the other extreme; order unknowable, adverse sequence assumed.")
        prev = t
    cur_trade.update(n=None, side=None)
    for n in run.intr_notes:
        cur_trade["side"] = n.get("side")
        when = dt.datetime.fromisoformat(n["time"])
        if _bar(run.dates, when) <= idx:
            add(when, "GAP_PROTOCOL", n["title"] + ": " + n["detail"])
    cur_trade.update(n=None, side=None)
    cur_trade.update(n=None, side=None)
    for b in run.blocked:
        when = dt.datetime.fromisoformat(b["time"])
        if _bar(run.dates, when) <= idx:
            cur_trade["side"] = b["side"]
            add(when, "BLOCKED_SYSTEM_POINTS_TOO_CLOSE", f"BLOCKED: SYSTEM_POINTS_TOO_CLOSE - {b['kind'].replace('_', ' ').lower()} {b['side']} not taken: Top {b['top']:,.2f} and Bottom {b['bottom']:,.2f} are {b['separation']:,.2f} points apart (minimum {b['threshold']:g})", b["price"])
    cur_trade.update(n=None, side=None)
    for fl in run.data_flags:
        when = dt.datetime.fromisoformat(fl["time"])
        if _bar(run.dates, when) <= idx:
            for issue in fl["issues"]:
                add(when, "INTRADAY_DATA_INCOMPLETE", f"{fl['date']}: {issue}")
    out.sort(key=lambda e: e["time"])
    return out


def _points(t: Trade, exit_price: float, slip: float) -> float:
    gross = (exit_price - t.entry_price) if t.direction == "BUY" else (t.entry_price - exit_price)
    return gross - 2 * slip


def _bar(dates: list[dt.datetime], moment: dt.datetime) -> int:
    return bisect.bisect_right(dates, moment) - 1


def snapshot(run: StrategyRun, idx: int, window: int = 400) -> dict:
    """Everything the UI may know at replay candle `idx` (inclusive)."""
    n = len(run.candles)
    idx = max(0, min(idx, n - 1))
    cur = run.candles[idx]
    slip = run.slippage_points

    closed_pts: list[float] = []
    last_trade = None  # (trade, finished) of the most recent revealed trade
    open_trade, rows = None, []
    markers: list[dict] = []
    gap_notes: list[dict] = []
    stop_line: list[dict] = []
    system_line: list[dict] = []
    gap_levels: list[dict] = []
    events: list[dict] = []

    for number, t in enumerate(run.trades, start=1):
        eb = _bar(run.dates, t.entry_date)
        if eb > idx:
            continue
        markers.append({"time": t.entry_date.isoformat(), "kind": "ENTRY", "side": t.direction, "price": t.entry_price})
        for when, level in t.stop_history:
            if _bar(run.dates, when) <= idx:
                stop_line.append({"time": when.isoformat(), "value": level})
        system_line.append({"time": t.entry_date.isoformat(), "value": t.system_point})
        for when, label in t.events:
            if _bar(run.dates, when) <= idx:
                events.append({"time": when.isoformat(), "label": label})
        gap_bar = {
            "GAP_ENTRY": eb,
            "GAP_EXITED": _bar(run.dates, t.exit_date) if t.exit_date else None,
            "GAP_HELD": _bar(run.dates, dt.datetime.combine(t.protective_sl_date, dt.time())) if t.protective_sl_date else None,
        }.get(t.gap_state)
        gap_known = gap_bar is not None and gap_bar <= idx  # a gap fact is visible only from its own bar
        if gap_known and run.mode == "daily":
            gday = {"GAP_ENTRY": eb, "GAP_EXITED": gap_bar, "GAP_HELD": gap_bar}[t.gap_state]
            note = _gap_note(run, t, gday)
            if note:
                gap_notes.append(note)
        if gap_known and run.mode == "daily" and t.gap_first_high is not None:
            day = {"GAP_ENTRY": t.entry_date, "GAP_EXITED": t.exit_date,
                   "GAP_HELD": dt.datetime.combine(t.protective_sl_date or t.entry_date.date(), dt.time())}[t.gap_state]
            gap_levels.append({"date": day.date().isoformat(), "high": t.gap_first_high, "low": t.gap_first_low, "state": t.gap_state})
        finished = t.exit_date is not None and t.exit_reason != "END_OF_BACKTEST" and _bar(run.dates, t.exit_date) <= idx
        last_trade = (t, finished)
        if finished:
            pts = _points(t, t.exit_price, slip)
            closed_pts.append(pts)
            markers.append({"time": t.exit_date.isoformat(), "kind": "EXIT", "side": t.direction, "price": t.exit_price})
            if t.gap_state == "GAP_EXITED":
                events.append({"time": t.exit_date.isoformat(), "label": "GAP EXIT (15m)"})
            stop = t.stop_history[-1][1] if t.stop_history else t.initial_sl
            rows.append(_row(number, t, stop, t.exit_price, pts, t.exit_reason, t.gap_state, t.breakeven_date is not None))
        else:
            stops = [v for w, v in t.stop_history if _bar(run.dates, w) <= idx]
            stop = stops[-1] if stops else t.initial_sl
            pts = _points(t, cur.close, slip)
            be = t.breakeven_date is not None and _bar(run.dates, t.breakeven_date) <= idx
            open_trade = {"side": t.direction, "entry_price": t.entry_price, "stop": stop, "price": cur.close,
                          "unrealized_points": round(pts, 2), "breakeven_active": be}
            rows.append(_row(number, t, stop, None, pts, "OPEN", t.gap_state if gap_known else None, be))

    wins = [p for p in closed_pts if p > 0]
    losses = [-p for p in closed_pts if p < 0]
    equity = peak = dd = 0.0
    for p in closed_pts:
        equity += p
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    realized = sum(closed_pts)
    unreal = open_trade["unrealized_points"] if open_trade else 0.0
    stats = {
        "trades_closed": len(closed_pts), "winning": len(wins), "losing": len(losses),
        "win_rate_pct": round(len(wins) / len(closed_pts) * 100, 2) if closed_pts else None,
        "realized_points": round(realized, 2), "unrealized_points": round(unreal, 2), "total_points": round(realized + unreal, 2),
        "profit_factor": round(sum(wins) / sum(losses), 4) if losses else None,
        "avg_win_points": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss_points": round(-sum(losses) / len(losses), 2) if losses else None,
        "max_drawdown_points": round(dd, 2), "largest_gap_pct": round(run.gap_cummax[idx], 3),
    }
    # Every day whose OPEN gapped >= 1% from the previous close (known from that day's open on).
    gap_days = [
        {"time": run.candles[k].ts.isoformat(), "pct": round(_pct(run.candles[k].open, run.candles[k - 1].close), 2)}
        for k in range(max(1, idx - window + 1), idx + 1)
        if abs(_pct(run.candles[k].open, run.candles[k - 1].close)) >= GAP_MARK_PCT
        and (run.interval == "1D" or run.candles[k].ts.date() != run.candles[k - 1].ts.date())
    ]
    waiting = None
    if open_trade is None and last_trade and last_trade[1] and last_trade[0].exit_reason == "BREAKEVEN_EXIT_AFTER_2_PERCENT_PROFIT":
        waiting = {"since": last_trade[0].exit_date.isoformat(),
                   "text": "Flat after the 2% breakeven exit - no reversal. Waiting for a NEW System Point and its entry trigger."}
    intr = run.mode in ("intraday", "hybrid")
    if intr:
        gap_notes = [n for n in run.intr_notes if _bar(run.dates, dt.datetime.fromisoformat(n["time"])) <= idx]
    levels_line = [{"time": ts.isoformat(), "top": tp, "bottom": bt} for ts, tp, bt in run.levels if _bar(run.dates, ts) <= idx] if intr else []
    data_flags = [f for f in run.data_flags if _bar(run.dates, dt.datetime.fromisoformat(f["time"])) <= idx] if intr else []
    known_gap_days = bisect.bisect_right(run.gap15_days, cur.ts.date())  # only days up to this candle
    start = max(0, idx - window + 1)
    return {
        "index": idx, "total": n, "at_end": idx == n - 1, "time": cur.ts.isoformat(), "close": cur.close,
        "instrument": {"symbol": SYMBOL, "instrument_type": "INDEX"}, "interval": run.interval, "unit": UNIT,
        "position": open_trade["side"] if open_trade else "FLAT", "open_trade": open_trade, "stats": stats,
        "candles": [{"time": c.ts.isoformat(), "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
                    for c in run.candles[start: idx + 1]],
        "window_start": start, "trades": rows, "markers": markers, "stop_line": stop_line, "system_line": system_line,
        "gap_levels": gap_levels, "events": events, "gap_notes": gap_notes, "gap_days": gap_days, "data_quality": run.quality.get(cur.ts.date().isoformat()),
        "filter": {"min_separation": run.min_separation, "blocked": sum(1 for b in run.blocked if _bar(run.dates, dt.datetime.fromisoformat(b["time"])) <= idx)}, "mode": run.mode, "event_log": build_event_log(run, idx) if intr else [], "waiting": waiting, "warnings": run.warnings, "session": run.session,
        "starts_flat_note": None,  # superseded by `warnings`: earlier state is reconstructed from Daily history and reported there
        "gap_protocol": {"active": (bool(gap_notes) or len(levels_line) > len(data_flags)) if intr else known_gap_days > 0,
                         "days_with_15m_data": (len(levels_line) - len(data_flags)) if intr else known_gap_days},
        "execution": {"slippage_points": slip}, "rule_gaps": RULE_GAPS_INTRADAY if intr else RULE_GAPS,
    }


def _row(number: int, t: Trade, stop: float, exit_price: float | None, pts: float, reason: str | None,
         gap_state: str | None, breakeven: bool) -> dict:
    done = exit_price is not None  # while a trade is open, nothing about its exit is known yet
    return {
        "carried_in": t.carried_in, "signal": t.signal_price, "exit_signal": t.exit_signal_price if done else None,
        "ambiguous": t.ambiguous if done else t.entry_ambiguous,
        "whipsaw": done and any(e[1].startswith("WHIPSAW") for e in t.events),
        "number": number, "time": t.entry_date.isoformat(), "instrument": SYMBOL, "side": t.direction,
        "entry": t.entry_price, "stop": stop, "exit": exit_price,
        "exit_time": t.exit_date.isoformat() if exit_price is not None and t.exit_date else None,
        "points": round(pts, 2), "reason": reason, "breakeven": bool(breakeven), "gap_state": gap_state,
    }


def _pct(a: float, b: float) -> float:
    return (a - b) / b * 100.0 if b else 0.0


def _gap_note(run: "StrategyRun", t: Trade, idx_of: int) -> dict | None:
    """Plain-words explanation of one gap-protocol event of trade `t`, whose
    gap day is candle `idx_of`. Uses only that day's open and the prior close."""
    if idx_of < 1 or t.gap_first_high is None:
        return None
    c, prev = run.candles[idx_of], run.candles[idx_of - 1]
    gap = _pct(c.open, prev.close)
    way = "up" if gap > 0 else "down"
    fh, fl = t.gap_first_high, t.gap_first_low
    f = lambda v: f"{v:,.2f}"
    levels = f"first 15-min candle (09:15-09:30; high {f(fh)}, low {f(fl)})"
    if t.gap_state == "GAP_ENTRY":
        buy = t.direction == "BUY"
        return {
            "kind": "GAP_ENTRY", "side": t.direction, "pct": round(gap, 2), "time": t.entry_date.isoformat(),
            "title": f"Gap {way} {abs(gap):.2f}% - {t.direction} entry",
            "detail": (f"Market opened at {f(c.open)} ({abs(gap):.2f}% gap {way}) and jumped past the {'BUY' if buy else 'SELL'} system point {f(t.system_point)}. "
                       f"The system waited for the {levels}. {t.direction} was taken when price crossed {'above the 15-min high' if buy else 'below the 15-min low'} "
                       f"at {f(t.entry_price)}. Structural stop kept at {f(t.initial_sl)}."),
        }
    stops = [v for w, v in t.stop_history if _bar(run.dates, w) < idx_of]
    before = stops[-1] if stops else t.initial_sl
    buy = t.direction == "BUY"
    head = (f"Market opened at {f(c.open)} ({abs(gap):.2f}% gap {way}), {'below' if buy else 'above'} the {t.direction} position's structural stop {f(before)}. "
            f"The ordinary stop was suspended for the day and the {levels} was used instead. ")
    if t.gap_state == "GAP_EXITED":
        return {
            "kind": "GAP_EXITED", "side": t.direction, "pct": round(gap, 2), "time": t.exit_date.isoformat(),
            "title": f"Gap {way} {abs(gap):.2f}% - {t.direction} exited by 15-min rule",
            "detail": head + f"Price then broke {'below the 15-min low' if buy else 'above the 15-min high'}, so the {t.direction} was exited at {f(t.exit_price)}. "
                             "No reverse trade is taken automatically; a fresh entry needs the normal system trigger.",
        }
    if t.gap_state == "GAP_HELD":
        return {
            "kind": "GAP_HELD", "side": t.direction, "pct": round(gap, 2), "time": c.ts.isoformat(),
            "title": f"Gap {way} {abs(gap):.2f}% - {t.direction} held",
            "detail": head + f"Price did not break the {'15-min low' if buy else '15-min high'}, so the {t.direction} is held and today's protective stop is the 15-min "
                             f"{'low' if buy else 'high'} {f(t.protective_sl)} (valid for this day only).",
        }
    return None


def backtest_report(run: StrategyRun) -> dict:
    """Full-range report: the same snapshot at the final candle (identical
    rules and assumptions as replay)."""
    return snapshot(run, len(run.candles) - 1, window=len(run.candles))


__all__ = ["StrategyRun", "run_strategy", "run_intraday_strategy", "run_hybrid_strategy", "build_event_log", "RULE_GAPS_INTRADAY", "snapshot", "backtest_report", "RULE_GAPS", "BREAKEVEN_EVENT", "UNIT"]
