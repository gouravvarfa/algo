"""Backtest summary statistics and equity curve — spec sections 18/19.
Every value is derived purely from the actual closed trades; nothing here
is ever a placeholder or estimate."""
from __future__ import annotations

import datetime as dt

from app.strategy.top_bottom.models import BacktestStatistics, EquityPoint, Signal, Trade


def compute_statistics(signals: list[Signal], trades: list[Trade]) -> BacktestStatistics:
    closed = [t for t in trades if not t.is_open]
    pnl_pcts = [t.pnl_pct for t in closed if t.pnl_pct is not None]

    wins = [p for p in pnl_pcts if p > 0]
    losses = [p for p in pnl_pcts if p < 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    net_pnl = sum(pnl_pcts)

    holding_days = [t.holding_days for t in closed if t.holding_days is not None]

    return BacktestStatistics(
        total_signals=len(signals),
        buy_signals=sum(1 for s in signals if s.direction == "BUY"),
        sell_signals=sum(1 for s in signals if s.direction == "SELL"),
        total_trades=len(closed),
        buy_trades=sum(1 for t in closed if t.direction == "BUY"),
        sell_trades=sum(1 for t in closed if t.direction == "SELL"),
        winning_trades=len(wins),
        losing_trades=len(losses),
        win_rate=(len(wins) / len(closed) * 100.0) if closed else 0.0,
        gross_profit_pct=gross_profit,
        gross_loss_pct=gross_loss,
        net_pnl_pct=net_pnl,
        average_trade_pct=(net_pnl / len(pnl_pcts)) if pnl_pcts else 0.0,
        average_win_pct=(gross_profit / len(wins)) if wins else 0.0,
        average_loss_pct=-(gross_loss / len(losses)) if losses else 0.0,
        best_trade_pct=max(pnl_pcts) if pnl_pcts else 0.0,
        worst_trade_pct=min(pnl_pcts) if pnl_pcts else 0.0,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
        max_drawdown_pct=_max_drawdown(pnl_pcts),
        average_holding_days=(sum(holding_days) / len(holding_days)) if holding_days else 0.0,
        expectancy_pct=(net_pnl / len(pnl_pcts)) if pnl_pcts else 0.0,
    )


def _max_drawdown(pnl_pcts: list[float]) -> float:
    if not pnl_pcts:
        return 0.0
    equity = 100.0
    peak = equity
    max_dd = 0.0
    for pct in pnl_pcts:
        equity *= 1 + pct / 100.0
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak * 100.0
        max_dd = max(max_dd, drawdown)
    return max_dd


def build_equity_curve(trades: list[Trade], starting_capital: float = 100.0) -> list[EquityPoint]:
    closed = [t for t in trades if not t.is_open and t.exit_date is not None]
    closed.sort(key=lambda t: t.exit_date)  # type: ignore[arg-type]

    equity = starting_capital
    peak = starting_capital
    cumulative_pnl_pct = 0.0
    curve: list[EquityPoint] = []

    for n, trade in enumerate(closed, start=1):
        trade_return = (trade.pnl_pct or 0.0) / 100.0
        equity *= 1 + trade_return
        cumulative_pnl_pct = (equity - starting_capital) / starting_capital * 100.0
        peak = max(peak, equity)
        drawdown_pct = (peak - equity) / peak * 100.0 if peak > 0 else 0.0
        curve.append(
            EquityPoint(
                date=trade.exit_date,  # type: ignore[arg-type]
                trade_number=n,
                equity=equity,
                cumulative_pnl_pct=cumulative_pnl_pct,
                drawdown_pct=drawdown_pct,
            )
        )
    return curve
