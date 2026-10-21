"""Evaluate the Top-Bottom strategy across a CHAIN of monthly futures contracts (Oct -> Nov -> Dec ...).

Rules (decided with the user):
  * the cutoff of a contract is 8 calendar days before its expiry (app/live/contracts.py); on the cutoff day everything moves to the NEXT contract;
  * a position still open at the cutoff is closed in the old contract and the SAME direction is opened in the next contract as a FRESH entry that
    follows that contract's own rules (its own System Points / stop, 2% breakeven from the new entry, entry-day 1% stop, capped stop);
  * so after the cutoff a stop hit or a reversal happens entirely in the new contract; the old contract is never traded again;
  * if the strategy was flat in "waiting for a fresh System Point" mode, it keeps waiting in the new contract (only points confirmed after the exit count).
Pure orchestration: data and the daily-close engine are injected, so this is tested without any network.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import pandas as pd

from app.live.contracts import cutoff
from app.strategy.top_bottom.intraday import IntradayInitial, Minute, run_intraday
from app.strategy.top_bottom.models import Trade

WAITING_EXITS = ("BREAKEVEN_EXIT_AFTER_2_PERCENT_PROFIT", "ENTRY_DAY_STOP_EXIT", "CAPPED_1_PERCENT_STOP_EXIT")
ROLLED = "ROLLED_TO_NEXT_CONTRACT"


@dataclass(frozen=True)
class ContractRef:
    trading_symbol: str
    token: str
    exch_seg: str
    expiry: dt.date
    lot_size: int


@dataclass
class Segment:
    contract: ContractRef
    start: dt.date                 # first day this contract is traded
    end: dt.date | None            # cutoff day of this contract (exclusive); None = still the active contract


@dataclass
class ChainResult:
    trades: list[Trade]
    segments: list[Segment]
    chart: pd.DataFrame                         # composite daily candles: each contract's own real prices in its own period
    active: ContractRef
    active_daily: pd.DataFrame                  # the active contract's own confirmed daily candles (for System Points)
    rolls: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def plan(contracts: list[ContractRef], from_date: dt.date, today: dt.date) -> list[Segment]:
    """[first contract from `from_date` until its cutoff] + [each next contract from the previous cutoff], up to the contract active today.
    The active contract's segment has end=None. A contract whose cutoff is not reached yet is the active one."""
    cs = sorted(contracts, key=lambda c: c.expiry)
    segs: list[Segment] = []
    start = from_date
    for i, c in enumerate(cs):
        end = cutoff(c.expiry) if i < len(cs) - 1 else None
        if end is not None and end <= start:
            continue                       # that contract's window is over before our history begins
        if end is None or end > today:
            segs.append(Segment(c, start, None))
            break
        segs.append(Segment(c, start, end))
        start = end
    return segs


async def evaluate(
    *, contracts: list[ContractRef], from_dt: dt.datetime, now: dt.datetime, symbol: str,
    get_daily: Callable[[ContractRef], Awaitable[pd.DataFrame]],
    get_minutes: Callable[[ContractRef], Awaitable[list[Minute]]],
    daily_engine: Callable[[ContractRef, pd.DataFrame], list[Trade]],
    reconstruct: Callable[[list[tuple], dt.date], tuple[IntradayInitial | None, list[str]]],
    entry_day_stop_pct: float | None, struct_cap_distance: float | None,
) -> ChainResult | None:
    """None = the chain does not apply (no cutoff has been passed yet, or the next contract has no minutes on its cutoff day yet):
    the caller keeps its single-contract evaluation."""
    segs = plan(contracts, from_dt.date(), now.date())
    if len(segs) < 2:
        return None

    all_trades: list[Trade] = []
    rolls: list[dict] = []
    notes: list[dict] = []
    warnings: list[str] = []
    chart_parts: list[pd.DataFrame] = []
    carry_dir: str | None = None          # direction to continue in the next contract
    waiting_exit: dt.datetime | None = None
    active_daily = pd.DataFrame()

    for k, seg in enumerate(segs):
        c = seg.contract
        daily = await get_daily(c)
        mins_all = await get_minutes(c)
        if k == len(segs) - 1:
            active_daily = daily
        day_idx = daily.index.date if len(daily) else []
        part = daily[(daily.index.date >= seg.start) & ((daily.index.date < seg.end) if seg.end else True)]
        chart_parts.append(part)

        if k == 0:
            before = daily[daily.index >= from_dt]
            if seg.end is not None:
                before = before[before.index.date < seg.end]
            if len(before) < 5:
                warnings.append(f"{c.trading_symbol}: not enough daily candles before its cutoff")
                return None
            trades = [t for t in daily_engine(c, before)]
            mins = [m for m in mins_all if m.ts.date() < seg.end] if seg.end else list(mins_all)
            if len(mins) >= 30:
                rows = [(t.to_pydatetime(), float(r.open), float(r.high), float(r.low), float(r.close)) for t, r in before.iterrows()]
                first_day = mins[0].ts.date()
                initial, _w = reconstruct(rows, first_day)
                res = run_intraday(symbol, [r[0] for r in rows], [r[4] for r in rows], mins, initial,
                                   entry_day_stop_pct=entry_day_stop_pct, struct_cap_distance=struct_cap_distance)
                older = [t for t in trades if t.exit_reason != "END_OF_BACKTEST" and t.exit_date is not None and t.exit_date.date() < first_day]
                trades = older + res.trades
                notes += res.notes
        else:
            mins = [m for m in mins_all if m.ts.date() >= seg.start and (seg.end is None or m.ts.date() < seg.end)]
            if not mins:
                if k == 1:
                    return None                    # the cutoff day has not produced a single minute of the next contract yet
                warnings.append(f"{c.trading_symbol}: no 1-minute candles for its window")
                trades = []
            else:
                rows = [(t.to_pydatetime(), float(r.open), float(r.high), float(r.low), float(r.close)) for t, r in daily.iterrows()]
                dts, cls = [r[0] for r in rows], [r[4] for r in rows]
                initial = None
                if carry_dir is None and waiting_exit is not None:
                    n_before = sum(1 for d in dts if d.date() < waiting_exit.date())
                    initial = IntradayInitial(block_after=n_before - 1)
                res = run_intraday(symbol, dts, cls, mins, initial, entry_day_stop_pct=entry_day_stop_pct,
                                   struct_cap_distance=struct_cap_distance, roll_in=carry_dir)
                trades = res.trades
                notes += res.notes
                if carry_dir and not any(t.rolled_in for t in trades):
                    warnings.append(f"{c.trading_symbol}: the {carry_dir} could not be rolled in (no System Point in this contract yet), so the strategy is flat")
        for t in trades:
            t.contract = c.trading_symbol

        # state to carry into the next contract
        carry_dir, waiting_exit = None, None
        if k < len(segs) - 1 and trades:
            last = trades[-1]
            if last.exit_reason == "END_OF_BACKTEST":
                nxt_first = next((m for m in await get_minutes(segs[k + 1].contract) if m.ts.date() >= seg.end), None)
                old_first = next((m for m in mins_all if m.ts.date() >= seg.end), None)
                if nxt_first is None or old_first is None:
                    return None                    # cannot price the roll yet: do not guess
                last.exit_date, last.exit_price, last.exit_reason = old_first.ts, old_first.open, ROLLED
                carry_dir = last.direction
                rolls.append({"time": old_first.ts, "direction": last.direction, "from": c.trading_symbol, "to": segs[k + 1].contract.trading_symbol,
                              "old_price": old_first.open, "new_price": nxt_first.open})
            elif last.exit_reason in WAITING_EXITS:
                waiting_exit = last.exit_date
        all_trades += trades

    chart = pd.concat([p for p in chart_parts if len(p)]) if any(len(p) for p in chart_parts) else pd.DataFrame()
    return ChainResult(all_trades, segs, chart, segs[-1].contract, active_daily, rolls, notes, warnings)
