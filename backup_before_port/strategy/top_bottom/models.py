"""
Data model for the Top-Bottom Futures Backtesting engine — an
always-reversing, trailing-stop system (see backtest.py). Deliberately
self-contained — nothing here is imported by, or imports from, the existing
Strategy One/GFS/Advanced GFS/PRD/NRD/Value Buy code, so this module cannot
alter their behavior.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Literal

Direction = Literal["BUY", "SELL"]
InstrumentClass = Literal["STOCK", "INDEX"]
ExitReason = Literal["TRAILING_STOP_REVERSAL", "END_OF_BACKTEST"]

@dataclass(frozen=True)
class SwingPoint:
    """A CONFIRMED swing top or bottom on the close-price series — only
    materializes once `right_bars` further closes exist, so it can never be
    known earlier than its confirmation bar (see swing_detection.py). The
    product always uses left_bars=right_bars=1 — the latest/smallest valid
    reversal, no minimum swing size, no wider pivot window."""
    kind: Literal["TOP", "BOTTOM"]
    pivot_index: int
    pivot_date: dt.datetime
    price: float
    confirmed_index: int
    confirmed_date: dt.datetime

@dataclass
class Signal:
    """One entry event (either the first FLAT->BUY/SELL breakout, or a
    trailing-stop-triggered reversal into the opposite direction)."""
    direction: Direction
    signal_index: int
    signal_date: dt.datetime
    system_point: float  # the level that was crossed to trigger this entry
    entry_price: float
    reversal_event_id: str | None  # shared by the exit that caused this entry, if any

@dataclass
class Trade:
    direction: Direction
    symbol: str
    trading_symbol: str
    expiry: str
    instrument_class: InstrumentClass
    lot_size: int  # real exchange lot size (Angel One scrip master) — 0 if unknown, never guessed

    signal_date: dt.datetime
    entry_date: dt.datetime
    entry_price: float
    system_point: float  # BUY: the Top broken; SELL: the Bottom broken

    initial_sl: float  # BUY: latest Bottom at entry; SELL: latest Top at entry
    active_stop: float  # the CURRENT trailing stop (mutated as new opposite pivots confirm)

    reversal_event_id: str  # shared with the trade that preceded/followed this one via a reversal

    exit_date: dt.datetime | None = None
    exit_price: float | None = None
    exit_reason: ExitReason | None = None

    stop_history: list[tuple[dt.datetime, float]] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.exit_date is None

    @property
    def pnl_points(self) -> float | None:
        if self.exit_price is None:
            return None
        return (self.exit_price - self.entry_price) if self.direction == "BUY" else (self.entry_price - self.exit_price)

    @property
    def pnl_amount(self) -> float | None:
        """P&L in real rupees = points x the contract's actual exchange lot
        size (never a guessed/hardcoded multiplier). None when the lot
        size is unknown (0) rather than silently showing 0 as if that were
        a real, zero-profit trade."""
        pts = self.pnl_points
        if pts is None or not self.lot_size:
            return None
        return pts * self.lot_size

    @property
    def pnl_pct(self) -> float | None:
        pts = self.pnl_points
        if pts is None or self.entry_price == 0:
            return None
        return (pts / self.entry_price) * 100.0

    @property
    def holding_days(self) -> int | None:
        if self.exit_date is None:
            return None
        return (self.exit_date.date() - self.entry_date.date()).days

    @property
    def status(self) -> str:
        if self.is_open:
            return "OPEN"
        pts = self.pnl_points or 0.0
        return "WIN" if pts > 0 else ("LOSS" if pts < 0 else "BREAKEVEN")


@dataclass
class PricePoint:
    """One completed close on the line chart. The backtest returns the FULL
    price series for the requested window so the frontend can draw the
    whole history — it must never have to reconstruct a "chart" out of
    trade entry/exit points alone."""
    date: dt.datetime
    close: float


@dataclass
class LevelPoint:
    """A step in the active System Point (green) or active Stop Loss (red)
    line. These lines move as structure updates; a line moving is NOT a
    trade — only a close crossing one is."""
    date: dt.datetime
    value: float


@dataclass
class EquityPoint:
    date: dt.datetime
    trade_number: int
    equity: float
    cumulative_pnl_pct: float
    drawdown_pct: float


@dataclass
class BacktestStatistics:
    total_signals: int
    buy_signals: int
    sell_signals: int
    total_trades: int
    buy_trades: int
    sell_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    gross_profit_pct: float
    gross_loss_pct: float
    net_pnl_pct: float
    average_trade_pct: float
    average_win_pct: float
    average_loss_pct: float
    best_trade_pct: float
    worst_trade_pct: float
    profit_factor: float | None
    max_drawdown_pct: float
    average_holding_days: float
    expectancy_pct: float


@dataclass
class DataCoverage:
    requested_from: dt.datetime
    requested_to: dt.datetime
    available_from: dt.datetime | None
    available_to: dt.datetime | None
    is_complete: bool


@dataclass
class BacktestResult:
    backtest_id: str
    symbol: str
    trading_symbol: str
    expiry: str
    instrument_class: InstrumentClass
    exch_seg: str
    timeframe: str
    coverage: DataCoverage
    price_series: list[PricePoint]  # full close-price line for the requested window
    system_point_line: list[LevelPoint]  # GREEN: active System Point over time
    stop_loss_line: list[LevelPoint]  # RED: active (trailing) Stop Loss over time
    signals: list[Signal]
    trades: list[Trade]
    equity_curve: list[EquityPoint]
    statistics: BacktestStatistics
    starting_capital: float
    created_at: dt.datetime
