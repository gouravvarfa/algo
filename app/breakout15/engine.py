"""15-minute opening-candle CALL breakout backtest (index points only).

Pure functions over real 15-minute index candles (bar START timestamps, IST,
naive). Breakout and stop are detected on those candles, so a breakout time is
the start of the candle that crossed (no finer timestamp is invented); a candle
that breaks out AND touches the stop is flagged AMBIGUOUS. Missing candles are
never filled: no 09:15 candle means DATA_INCOMPLETE.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

OPEN = dt.time(9, 15)
FIRST_END = dt.time(9, 30)
SECOND_LAST_START = dt.time(15, 0)
LAST_START = dt.time(15, 15)
EXPECTED_BARS = 25  # 09:15 .. 15:15 starts

STOP_LOSS, MARKET_CLOSE, NO_ENTRY, DATA_INCOMPLETE = "STOP_LOSS", "MARKET_CLOSE", "NO_ENTRY", "DATA_INCOMPLETE"
DATA_ENDED = "DATA_ENDED_BEFORE_CLOSE"

Bar = tuple  # (ts: datetime, o, h, l, c)


@dataclass
class DayResult:
    date: dt.date
    first_open: float | None = None
    first_high: float | None = None
    first_low: float | None = None
    first_close: float | None = None
    first_range_pct: float | None = None
    first_direction: str | None = None
    breakout_time: dt.datetime | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    second_last_time: dt.datetime | None = None
    second_last_close: float | None = None
    second_last_change_pct: float | None = None
    exit_time: dt.datetime | None = None
    exit_price: float | None = None
    exit_reason: str = NO_ENTRY
    pnl_points: float | None = None
    pnl_pct: float | None = None
    flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        def t(v):
            return v.isoformat(sep=" ", timespec="minutes") if v else None
        d = {k: v for k, v in self.__dict__.items() if k != "flags"}
        d["date"] = self.date.isoformat()
        for k in ("breakout_time", "second_last_time", "exit_time"):
            d[k] = t(d[k])
        d["flags"] = list(self.flags)
        d["quality"] = "; ".join(self.flags) if self.flags else "OK"
        return d


def _direction(o: float, c: float) -> str:
    return "Green" if c > o else "Red" if c < o else "Doji"


def run_day(day: dt.date, bars: list[Bar]) -> DayResult:
    """`bars` are real 15-minute candles keyed by START time (09:15 ... 15:15)."""
    r = DayResult(day)
    by_start = {b[0].time(): b for b in sorted(bars, key=lambda b: b[0])}
    first = by_start.get(OPEN)
    if first is None:
        r.exit_reason = DATA_INCOMPLETE
        r.flags.append("first 15-minute candle (09:15) missing")
        return r

    _, o, h, l, c = first
    r.first_open, r.first_high, r.first_low, r.first_close = o, h, l, c
    r.first_range_pct = (h - l) / o * 100.0 if o else None
    r.first_direction = _direction(o, c)
    r.stop_loss = l

    after = [b for t, b in sorted(by_start.items()) if t >= FIRST_END]  # never anything at/before the first candle
    missing = EXPECTED_BARS - 1 - len(after)

    sl = by_start.get(SECOND_LAST_START)
    if sl is not None:
        r.second_last_time = sl[0]
        r.second_last_close = sl[4]
        r.second_last_change_pct = (sl[4] - sl[1]) / sl[1] * 100.0 if sl[1] else None
    else:
        r.flags.append("second-last candle (15:00) missing")

    entry_i = next((i for i, b in enumerate(after) if b[2] > h), None)
    if entry_i is None:
        r.exit_reason = NO_ENTRY
        if missing:
            r.flags.append(f"{missing} candle(s) missing after 09:30; a breakout cannot be ruled out")
        return r

    eb = after[entry_i]
    r.breakout_time = eb[0]
    r.entry_price = eb[1] if eb[1] > h else h
    if eb[1] > h:
        r.flags.append("gap entry at bar open")
    r.flags.append("breakout time = start of the 15-minute candle that crossed the High")

    exit_bar = exit_price = reason = None
    if eb[3] <= l:  # the breakout candle also trades through the stop: order inside the candle is unknowable
        exit_bar, exit_price, reason = eb, l, STOP_LOSS
        r.flags.append("AMBIGUOUS: breakout and stop loss inside the same 15-minute candle; stop assumed first")
    else:
        for b in after[entry_i + 1:]:
            if b[3] <= l:
                exit_bar, exit_price, reason = b, (b[1] if b[1] < l else l), STOP_LOSS
                if b[1] < l:
                    r.flags.append("stop gapped: filled at candle open")
                break
    if reason is None:
        last = after[-1]
        exit_bar, exit_price = last, last[4]
        if last[0].time() == LAST_START and not missing:
            reason = MARKET_CLOSE
        else:
            reason = DATA_ENDED
            r.flags.append(f"data incomplete: last candle {last[0]:%H:%M}, {missing} candle(s) missing; not a verified market close")

    r.exit_time, r.exit_price, r.exit_reason = exit_bar[0], exit_price, reason
    r.pnl_points = exit_price - r.entry_price
    r.pnl_pct = r.pnl_points / r.entry_price * 100.0
    return r


def summarize(rows: list[DayResult]) -> dict:
    trades = [r for r in rows if r.entry_price is not None]
    done = [r for r in trades if r.pnl_points is not None]
    pts = [r.pnl_points for r in done]
    equity = peak = dd = 0.0
    for p in pts:
        equity += p
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    wins = [p for p in pts if p > 0]
    return {
        "trading_days": len(rows),
        "call_entries": len(trades),
        "no_entry_days": sum(r.exit_reason == NO_ENTRY for r in rows),
        "stop_loss_exits": sum(r.exit_reason == STOP_LOSS for r in rows),
        "market_close_exits": sum(r.exit_reason == MARKET_CLOSE for r in rows),
        "incomplete_days": sum(r.exit_reason in (DATA_INCOMPLETE, DATA_ENDED) for r in rows),
        "ambiguous_days": sum(any(f.startswith("AMBIGUOUS") for f in r.flags) for r in rows),
        "winning_trades": len(wins),
        "losing_trades": sum(p < 0 for p in pts),
        "win_rate": round(len(wins) / len(pts) * 100.0, 1) if pts else None,
        "net_points": round(sum(pts), 2),
        "avg_points": round(sum(pts) / len(pts), 2) if pts else None,
        "max_drawdown_points": round(dd, 2),
    }


def run_backtest(bars: list[Bar]) -> tuple[list[DayResult], dict]:
    by_day: dict[dt.date, list[Bar]] = {}
    for b in bars:
        by_day.setdefault(b[0].date(), []).append(b)
    rows = [run_day(d, v) for d, v in sorted(by_day.items())]
    return rows, summarize(rows)
