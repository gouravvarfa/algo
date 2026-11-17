"""Evaluate the Top-Bottom strategy across a CHAIN of monthly futures contracts (Oct -> Nov -> Dec ...).

Rules (decided with the user):
  * the cutoff of a contract is 8 calendar days before its expiry (app/live/contracts.py); on the cutoff day everything moves to the NEXT contract;
  * a position still open at the cutoff is closed in the old contract and the SAME direction is opened in the next contract as a FRESH entry that
    follows that contract's own rules (its own System Points / stop, 2% breakeven from the new entry, entry-day 1% stop, capped stop);
  * so after the cutoff a stop hit or a reversal happens entirely in the new contract; the old contract is never traded again;
  * if the strategy was flat in "waiting for a fresh System Point" mode, it keeps waiting in the new contract (only points confirmed after the exit count).
Hold rule (owner, 17 Nov 2026) for contracts whose cutoff is on/after contracts.HOLD_ROLL_FROM (the rolls above stay for earlier cutoffs):
  * from the cutoff nothing NEW is opened in the old contract; a position open at the cutoff stays there, managed by that contract's own rules;
  * its stop / System Point closes it inside the window: a reversal goes into the NEXT contract at that moment (the next contract's price at that
    minute: the exit price plus the spread between the two contracts' minute opens), 1% stop that day, then the next contract's System Points;
    a breakeven / 1% exit leaves the strategy flat in the next contract (after a 1% stop no entry that day);
  * still open at the hold limit (NFO: expiry day; MCX: the weekday before the tender period; contracts.hold_limit): closed at that day's open and
    the same side taken in the next contract at its open (1% stop that day);
  * while the position is held, the OLD contract is the active one (chart, price, System Points); no guessing: a roll waits for both prices.
Pure orchestration: data and the daily-close engine are injected, so this is tested without any network.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import pandas as pd

from app.live.contracts import cutoff, hold_limit, hold_rule
from app.ops.stats import tracked
from app.strategy.top_bottom.gap_protocol import NSE, Session
from app.strategy.top_bottom.intraday import IntradayInitial, Minute, run_intraday
from app.strategy.top_bottom.models import Trade

WAITING_EXITS: tuple = ()   # since 10 Nov 2026 no exit waits for a new System Point (the existing ones stay usable)
ROLLED = "ROLLED_TO_NEXT_CONTRACT"
REVERSING_EXITS = ("STOP_LOSS_REVERSAL", "GAP_EXIT_15M")                       # the strategy reverses at once after these
PCT_STOP_EXITS = ("CAPPED_1_PERCENT_STOP_EXIT", "ENTRY_DAY_STOP_EXIT")          # a 1% stop: nothing more is entered that day


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
    start_at: dt.datetime | None = None     # hold rule: the moment this contract took over from the previous one
    held_until: dt.datetime | None = None   # hold rule: this contract's position was held past its cutoff until this moment


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


@tracked("chain_eval.evaluate")
async def evaluate(
    *, contracts: list[ContractRef], from_dt: dt.datetime, now: dt.datetime, symbol: str,
    get_daily: Callable[[ContractRef], Awaitable[pd.DataFrame]],
    get_minutes: Callable[[ContractRef], Awaitable[list[Minute]]],
    daily_engine: Callable[[ContractRef, pd.DataFrame], list[Trade]],
    reconstruct: Callable[[list[tuple], dt.date], tuple[IntradayInitial | None, list[str]]],
    entry_day_stop_pct: float | None, struct_cap_distance: float | None, breakeven_pct: float | None = None, session: Session = NSE,
    flat_from: dt.datetime | None = None,
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
    active_c = segs[-1].contract

    take: dict | None = None              # hold rule: how the next contract takes over (moment, side, fill, reversal, no-entry day)
    out_segs: list[Segment] = []

    for k, seg in enumerate(segs):
        c = seg.contract
        last_seg = k == len(segs) - 1
        hold = (not last_seg) and seg.end is not None and hold_rule(c.expiry)
        hold_day = hold_limit(c.expiry, c.exch_seg, symbol) if hold else None
        daily = await get_daily(c)
        mins_all = await get_minutes(c)
        if take is not None:
            seg = Segment(c, take["at"].date(), seg.end, start_at=take["at"])
        out_segs.append(seg)
        upto = hold_day if hold else seg.end           # this contract's minutes / daily candles run until here (exclusive)

        if k == 0:
            before = daily[daily.index >= from_dt]
            if seg.end is not None:
                before = before[before.index.date < seg.end]
            if len(before) < 5:
                warnings.append(f"{c.trading_symbol}: not enough daily candles before its cutoff")
                return None
            trades = [t for t in daily_engine(c, before)]
            mins = [m for m in mins_all if m.ts.date() < upto] if upto else list(mins_all)
            if len(mins) >= 30:
                src = daily[daily.index >= from_dt]
                if upto is not None:
                    src = src[src.index.date < upto]
                rows = [(t.to_pydatetime(), float(r.open), float(r.high), float(r.low), float(r.close)) for t, r in src.iterrows()]
                first_day = mins[0].ts.date()
                initial, _w = reconstruct(rows, first_day)
                res = run_intraday(symbol, [r[0] for r in rows], [r[4] for r in rows], mins, initial,
                                   entry_day_stop_pct=entry_day_stop_pct, struct_cap_distance=struct_cap_distance, breakeven_pct=breakeven_pct, session=session,
                                   flat_from=flat_from, entries_from_day=seg.end if hold else None)
                older = [t for t in trades if t.exit_reason != "END_OF_BACKTEST" and t.exit_date is not None and t.exit_date.date() < first_day]
                trades = older + res.trades
                notes += res.notes
        else:
            day0 = take["at"].date() if take is not None else seg.start
            mins = [m for m in mins_all if m.ts.date() >= day0 and (upto is None or m.ts.date() < upto)]
            rows = [(t.to_pydatetime(), float(r.open), float(r.high), float(r.low), float(r.close)) for t, r in daily.iterrows()]
            dts, cls = [r[0] for r in rows], [r[4] for r in rows]
            if not mins:
                if k == 1 and take is None:
                    return None                    # the cutoff day has not produced a single minute of the next contract yet
                if take is None or take["at"].date() < now.date():
                    warnings.append(f"{c.trading_symbol}: no 1-minute candles for its window")
                trades = []
            else:
                initial = None
                if carry_dir is None and waiting_exit is not None:
                    n_before = sum(1 for d in dts if d.date() < waiting_exit.date())
                    initial = IntradayInitial(block_after=n_before - 1)
                tk = take or {}
                res = run_intraday(symbol, dts, cls, mins, initial, entry_day_stop_pct=entry_day_stop_pct,
                                   struct_cap_distance=struct_cap_distance, roll_in=tk.get("dir") or carry_dir, breakeven_pct=breakeven_pct, session=session,
                                   flat_from=flat_from, entries_from_day=seg.end if hold else None, start_at=tk.get("at"), roll_fill=tk.get("fill"),
                                   roll_reverse=bool(tk.get("reverse")), no_entry_day=tk.get("no_entry_day"))
                trades = res.trades
                notes += res.notes
                if carry_dir and not any(t.rolled_in for t in trades):
                    warnings.append(f"{c.trading_symbol}: the {carry_dir} could not be rolled in (no System Point in this contract yet), so the strategy is flat")
        for t in trades:
            t.contract = c.trading_symbol
        held_now = False

        # state to carry into the next contract
        carry_dir, waiting_exit, take = None, None, None
        if not last_seg and hold:
            nxt = segs[k + 1].contract
            lt = next((t for t in reversed(trades) if t.entry_date.date() < seg.end), None)
            open_at_cut = lt is not None and (lt.exit_reason == "END_OF_BACKTEST" or (lt.exit_date is not None and lt.exit_date.date() >= seg.end))
            if not open_at_cut:
                pass                                            # flat at the cutoff: the next contract starts on the cutoff day
            elif lt.exit_reason == "END_OF_BACKTEST":
                old_first = next((m for m in mins_all if m.ts.date() >= hold_day), None)
                nxt_first = next((m for m in await get_minutes(nxt) if m.ts.date() >= hold_day), None)
                if old_first is None or nxt_first is None:
                    held_now = True                             # still held: the hold limit has not opened yet (no guessing)
                    lt.events.append((lt.exit_date, f"HELD PAST THE CUTOFF ({seg.end}): stays in {c.trading_symbol} until its stop / System Point, "
                                                    f"at the latest until the open of {hold_day}"))
                else:
                    lt.exit_date, lt.exit_price, lt.exit_reason = old_first.ts, old_first.open, ROLLED
                    lt.events.append((old_first.ts, f"HOLD LIMIT {hold_day}: no System Point since the cutoff, closed at the open and continued in {nxt.trading_symbol}"))
                    seg.held_until = old_first.ts
                    take = {"at": nxt_first.ts, "dir": lt.direction, "fill": None, "reverse": False}
                    rolls.append({"time": old_first.ts, "direction": lt.direction, "from": c.trading_symbol, "to": nxt.trading_symbol,
                                  "old_price": old_first.open, "new_price": nxt_first.open, "kind": "ROLL"})
            else:
                T = lt.exit_date
                seg.held_until = T
                if lt.exit_reason in REVERSING_EXITS:
                    rev = "SELL" if lt.direction == "BUY" else "BUY"
                    key = T.replace(second=0, microsecond=0)
                    om = next((m for m in mins_all if m.ts.replace(second=0, microsecond=0) == key), None)
                    nm = next((m for m in await get_minutes(nxt) if m.ts >= T), None)
                    fill = None
                    if om is not None and nm is not None and nm.ts.replace(second=0, microsecond=0) == key:
                        fill = round(float(lt.exit_price) + (nm.open - om.open), 2)     # the same moment's price in the next contract (spread at that minute)
                    take = {"at": T, "dir": rev, "fill": fill, "reverse": True}
                    lt.events.append((T, f"PAST THE CUTOFF: the {rev} goes into {nxt.trading_symbol} (hold rule)"))
                    rolls.append({"time": T, "direction": rev, "from": c.trading_symbol, "to": nxt.trading_symbol,
                                  "old_price": lt.exit_price, "new_price": fill if fill is not None else (nm.open if nm else None), "kind": "REVERSE"})
                else:
                    take = {"at": T, "dir": None, "fill": None, "reverse": False,
                            "no_entry_day": T.date() if lt.exit_reason in PCT_STOP_EXITS else None}
        elif not last_seg and trades:
            last = trades[-1]
            if last.exit_reason == "END_OF_BACKTEST":
                nxt_first = next((m for m in await get_minutes(segs[k + 1].contract) if m.ts.date() >= seg.end), None)
                old_first = next((m for m in mins_all if m.ts.date() >= seg.end), None)
                if nxt_first is None or old_first is None:
                    return None                    # cannot price the roll yet: do not guess
                last.exit_date, last.exit_price, last.exit_reason = old_first.ts, old_first.open, ROLLED
                carry_dir = last.direction
                rolls.append({"time": old_first.ts, "direction": last.direction, "from": c.trading_symbol, "to": segs[k + 1].contract.trading_symbol,
                              "old_price": old_first.open, "new_price": nxt_first.open, "kind": "ROLL"})
            elif last.exit_reason in WAITING_EXITS:
                waiting_exit = last.exit_date
        chart_end = None if held_now else (take["at"].date() if take is not None else seg.end)
        if held_now:
            seg.end = None
        chart_parts.append(daily[(daily.index.date >= seg.start) & ((daily.index.date < chart_end) if chart_end else True)])
        all_trades += trades
        if held_now or last_seg:
            active_daily, active_c = daily, c
            break

    chart = pd.concat([p for p in chart_parts if len(p)]) if any(len(p) for p in chart_parts) else pd.DataFrame()
    return ChainResult(all_trades, out_segs, chart, active_c, active_daily, rolls, notes, warnings)
