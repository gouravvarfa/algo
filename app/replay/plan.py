"""
Backend-chosen replay session: the user never types dates.

  - Daily-close engine : the whole stored 1D range.
  - Hybrid / intraday  : ONE continuous block of stored 1-minute days. By default the most recent block; an older
                         block can be chosen explicitly (`block` index) and is always replayed on its own.

What makes a block "continuous": there is no exchange holiday calendar in this app, so the stored DAILY candles are the
evidence of which days were trading sessions. Two 1-minute days belong to the same block when no stored Daily session
lies between them (weekends and holidays have no Daily candle, so they never split a block). A Daily session WITHOUT
1-minute data between them splits the block: a position cannot be carried across a session whose intraday path is
unknown. Where the Daily data does not cover the dates, the older rule applies (a gap of more than 5 calendar days).
Disconnected blocks are never joined.

Everything before a block's first day is warm-up that stays internal: Daily candles give the System Points, and the
position / stop / breakeven / waiting state at the first replay candle is reconstructed from them (APPROXIMATE warm-up:
Daily-close semantics, because no continuous 1-minute history exists before a block's own first day).
"""
from __future__ import annotations

import datetime as dt

from app.replay.store import ReplayStore

SYMBOL = "BANKNIFTY"
MAX_BLOCK_GAP_DAYS = 5


class PlanError(ValueError):
    pass


def split_blocks(minute_days: list[str], daily_days: list[str]) -> list[dict]:
    """[{days: [...], gap_before: reason-or-None}] in chronological order."""
    daily = sorted(daily_days)
    out: list[dict] = []
    for d in minute_days:
        if not out:
            out.append({"days": [d], "gap_before": None})
            continue
        prev = out[-1]["days"][-1]
        covered = bool(daily) and daily[0] <= prev and daily[-1] >= d
        if covered:
            between = [x for x in daily if prev < x < d]
            reason = (f"{len(between)} stored Daily session(s) between {prev} and {d} have no 1-minute data "
                      f"({between[0]}{'...' if len(between) > 1 else ''})") if between else None
        else:
            gap = (dt.date.fromisoformat(d) - dt.date.fromisoformat(prev)).days
            reason = f"{gap} calendar days without 1-minute data between {prev} and {d} (no Daily data there to verify sessions)" if gap > MAX_BLOCK_GAP_DAYS else None
        if reason:
            out.append({"days": [d], "gap_before": reason})
        else:
            out[-1]["days"].append(d)
    return out


def _range(first: str, last: str) -> tuple[dt.datetime, dt.datetime]:
    return (dt.datetime.combine(dt.date.fromisoformat(first), dt.time()),
            dt.datetime.combine(dt.date.fromisoformat(last), dt.time(23, 59)))


def plan_session(store: ReplayStore, mode: str, interval: str | None = None, block: int | None = None) -> dict:
    warnings: list[str] = []
    if mode in ("intraday", "hybrid"):
        iv = "1m"
        days = store.distinct_days(SYMBOL, iv)
        if not days:
            raise PlanError("No 1m candles stored. Download or import 1m data first (see data coverage).")
        daily_days = store.distinct_days(SYMBOL, "1D")
        if not daily_days:
            raise PlanError("Intraday mode needs 1D candles for the System Points. Download 1D data covering the 1m dates and the weeks before them.")
        raw = split_blocks(days, daily_days)
        chosen = len(raw) - 1 if block is None else block
        if not 0 <= chosen < len(raw):
            raise PlanError(f"Unknown 1-minute data block {block}: {len(raw)} block(s) are stored.")
        blocks = []
        for i, b in enumerate(raw):
            s, e = _range(b["days"][0], b["days"][-1])
            blocks.append({"index": i, "from_date": b["days"][0], "to_date": b["days"][-1], "sessions": len(b["days"]),
                           "minute_candles": store.count(SYMBOL, iv, s, e), "selected": i == chosen,
                           "separated_from_previous_by": b["gap_before"]})
        first, last = raw[chosen]["days"][0], raw[chosen]["days"][-1]
        if len(raw) > 1:
            others = "; ".join(f"{b['from_date']}..{b['to_date']} ({b['sessions']} sessions)" for b in blocks if not b["selected"])
            warnings.append(f"{len(raw)} separate blocks of 1m data are stored; this replay uses {first}..{last} "
                            f"({'the most recent block' if chosen == len(raw) - 1 else 'the block you selected'}). Not included: {others}. "
                            "Blocks are never joined (a position cannot be carried across sessions without 1-minute data); pick another block in 'Data block' to replay it on its own.")
        before = sum(1 for d in daily_days if d < first)
        if before < 3:
            raise PlanError(f"Not enough 1D history before {first} to form System Points ({before} Daily candles). Download 1D data from earlier dates.")
        later = blocks[chosen + 1] if chosen + 1 < len(blocks) else None
        daily_after = [d for d in daily_days if d > last and (later is None or d < later["from_date"])]
        end = {
            "last_minute_day": last,
            "last_daily_session": max((d for d in daily_days if d <= last), default=None),
            "last_stored_daily": daily_days[-1],
            "daily_sessions_after_without_1m": len(daily_after),
            "first_daily_after": daily_after[0] if daily_after else None,
            "later_block": {"from_date": later["from_date"], "to_date": later["to_date"]} if later else None,
            "provider_checked": False,  # nothing here asks Angel One; only the stored database is consulted
        }
        extra = {"blocks": blocks, "selected_block": chosen, "end": end,
                 "warmup": "Approximate warm-up: the state before the block's first day comes from Daily candles (Daily-close semantics), "
                           "because no continuous 1-minute history exists before it."}
    elif mode == "daily":
        iv = interval or "1D"
        days = store.distinct_days(SYMBOL, iv)
        if not days:
            raise PlanError(f"No {iv} candles stored. Download or import data first (see data coverage).")
        first, last = days[0], days[-1]
        warnings.append(f"Daily-close engine starts FLAT at the first stored candle ({first}); there is no earlier history to reconstruct a position from.")
        extra = {"blocks": [], "selected_block": None, "warmup": None,
                 "end": {"last_minute_day": None, "last_daily_session": last, "last_stored_daily": last, "daily_sessions_after_without_1m": 0,
                         "first_daily_after": None, "later_block": None, "provider_checked": False}}
    else:
        raise PlanError(f"Unknown engine '{mode}'")
    start, end_ts = _range(first, last)
    minute_candles = store.count(SYMBOL, iv, start, end_ts)
    base = {"mode": mode, "from_date": first, "to_date": last, "warnings": warnings, **extra}
    if mode == "hybrid":   # what is shown/stepped is the Daily candle; the 1-minute data is processed inside each session
        return {**base, "interval": "1D", "candles": store.count(SYMBOL, "1D", start, end_ts), "minute_candles": minute_candles}
    return {**base, "interval": iv, "candles": minute_candles}
