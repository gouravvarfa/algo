"""Top-level orchestration: bar arrays -> full BacktestResult. Wires
backtest.py + statistics.py together; still pure/no I/O (data fetching and
persistence live in backend/services/top_bottom_backtest_service.py)."""
from __future__ import annotations

import datetime as dt
import uuid

from app.strategy.top_bottom.backtest import run_top_bottom_backtest
from app.strategy.top_bottom.models import (
    BacktestResult,
    DataCoverage,
    InstrumentClass,
    LevelPoint,
    PricePoint,
    Trade,
)
from app.strategy.top_bottom.statistics import build_equity_curve, compute_statistics


def _build_level_lines(trades: list[Trade]) -> tuple[list[LevelPoint], list[LevelPoint]]:
    """
    The GREEN (active System Point) and RED (active Stop Loss) step lines,
    derived from the trades the engine actually produced. The stop line
    follows each trade's real stop_history, so it visibly trails; the
    system-point line holds each trade's entry trigger level for that
    trade's lifetime.
    """
    system_line: list[LevelPoint] = []
    stop_line: list[LevelPoint] = []
    for t in trades:
        end = t.exit_date or t.entry_date
        system_line.append(LevelPoint(t.entry_date, t.system_point))
        system_line.append(LevelPoint(end, t.system_point))
        for moment, value in t.stop_history:
            stop_line.append(LevelPoint(moment, value))
        stop_line.append(LevelPoint(end, t.active_stop))
    return system_line, stop_line


def run_backtest(
    symbol: str,
    trading_symbol: str,
    expiry: str,
    instrument_class: InstrumentClass,
    lot_size: int,
    exch_seg: str,
    timeframe: str,
    dates: list[dt.datetime],
    closes: list[float],
    opens: list[float],
    requested_from: dt.datetime,
    requested_to: dt.datetime,
    starting_capital: float = 100.0,
    data_available_from: dt.datetime | None = None,
    data_available_to: dt.datetime | None = None,
) -> BacktestResult:
    """
    `dates`/`closes`/`opens` must already be CLIPPED to exactly
    [requested_from, requested_to] — per explicit user direction, the
    backtest starts FLAT at from_date and never uses any bar before it for
    structure detection. No warm-up from earlier history, and no position
    can be "already open" carried in from before the selected range.

    `data_available_from`/`data_available_to` describe the FULL range the
    provider actually had on offer (before clipping) — used only for the
    honest DataCoverage report, never fed to the engine.
    """
    coverage = DataCoverage(
        requested_from=requested_from,
        requested_to=requested_to,
        available_from=data_available_from,
        available_to=data_available_to,
        is_complete=(
            data_available_from is not None
            and data_available_to is not None
            and data_available_from <= requested_from
            and data_available_to >= min(requested_to, dt.datetime.now())
        ),
    )

    signals, trades = run_top_bottom_backtest(
        symbol, trading_symbol, expiry, instrument_class, lot_size, dates, closes, opens
    )

    stats = compute_statistics(signals, trades)
    equity_curve = build_equity_curve(trades, starting_capital)

    # The COMPLETE close-price line for the requested window — the chart
    # must show all of it, never just the bars a trade happened to touch.
    price_series = [PricePoint(d, c) for d, c in zip(dates, closes)]
    system_point_line, stop_loss_line = _build_level_lines(trades)

    return BacktestResult(
        backtest_id=str(uuid.uuid4()),
        symbol=symbol,
        trading_symbol=trading_symbol,
        expiry=expiry,
        instrument_class=instrument_class,
        exch_seg=exch_seg,
        timeframe=timeframe,
        coverage=coverage,
        price_series=price_series,
        system_point_line=system_point_line,
        stop_loss_line=stop_loss_line,
        signals=signals,
        trades=trades,
        equity_curve=equity_curve,
        statistics=stats,
        starting_capital=starting_capital,
        created_at=dt.datetime.utcnow(),
    )
