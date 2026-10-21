"""Strategy state at the start of a 1-minute window, rebuilt from the Daily candles before it (moved here from the
removed Index Replay module; the live Top-Bottom engine and the contract chain use it)."""
from __future__ import annotations

import copy
import datetime as dt

from app.strategy.top_bottom.backtest import run_top_bottom_backtest
from app.strategy.top_bottom.intraday import IntradayInitial

SYMBOL = "BANKNIFTY"


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
