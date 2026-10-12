"""
Paper trading for the Top-Bottom system.

Every tick, each watched instrument's CONFIRMED candles from its fixed
`from_date` are replayed through the unmodified backtest engine
(app/strategy/top_bottom/backtest.py). The replay is deterministic, so the
paper position always equals exactly what the validated engine would hold —
there is no second copy of the trading rules here to drift out of sync.

Trades entered before the instrument was added are flagged live=False
(replay/backfill) and are excluded from the live P&L.
"""
from __future__ import annotations

import asyncio
import calendar
import datetime as dt
import json
import logging
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import BaseModel

from app.indicators.confirmed_bars import drop_incomplete_trailing_bars, drop_incomplete_trailing_daily_bar
from app.providers import angelone_scrip_master as scrip_master
from app.providers.angelone_provider import AngelOneProvider
from app.strategy.top_bottom.backtest import SWING_LEFT_BARS, SWING_RIGHT_BARS, run_top_bottom_backtest
from app.strategy.top_bottom.strategy import _build_level_lines
from app.strategy.top_bottom.swing_detection import detect_confirmed_swings

log = logging.getLogger("paper")
IST = ZoneInfo("Asia/Kolkata")
STATE_PATH = Path("data/watchlist.json")

# timeframe -> (Angel One interval, bar minutes, max days one request may span)
TIMEFRAMES: dict[str, tuple[str, int | None, int]] = {
    "1m": ("ONE_MINUTE", 1, 25),
    "5m": ("FIVE_MINUTE", 5, 90),
    "15m": ("FIFTEEN_MINUTE", 15, 180),
    "30m": ("THIRTY_MINUTE", 30, 180),
    "1H": ("ONE_HOUR", 60, 380),
    "1D": ("ONE_DAY", None, 1900),
}
DEFAULT_LOOKBACK_DAYS = {"1m": 3, "5m": 10, "15m": 20, "30m": 40, "1H": 90, "1D": 365}


class Instrument(BaseModel):
    id: str
    symbol: str
    instrument_type: str  # EQUITY | FUTURE
    timeframe: str
    quantity: int  # units traded (futures: lots x lot_size)
    lots: int | None = None
    lot_size: int | None = None
    token: str
    trading_symbol: str
    exch_seg: str
    expiry: str = ""
    from_date: str  # replay starts FLAT here
    added_at: str  # trades entered at/after this are LIVE paper trades


class PaperError(ValueError):
    pass


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST).replace(tzinfo=None)


def market_open(now: dt.datetime | None = None) -> bool:
    now = now or now_ist()
    return now.weekday() < 5 and dt.time(9, 15) <= now.time() <= dt.time(15, 30)


def _ts(d: dt.datetime) -> int:
    # IST wall-clock encoded as if UTC, so the chart axis reads in IST.
    return calendar.timegm(d.timetuple())


def _iso(d: dt.datetime | None) -> str | None:
    return d.isoformat(sep=" ", timespec="minutes") if d else None


class PaperEngine:
    def __init__(self, provider: AngelOneProvider) -> None:
        self.provider = provider
        self.instruments: dict[str, Instrument] = {}
        self.snapshots: dict[str, dict] = {}
        self.errors: dict[str, str] = {}
        self.last_tick: dt.datetime | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._load()

    # ---- persistence -----------------------------------------------------
    def _load(self) -> None:
        if STATE_PATH.exists():
            for raw in json.loads(STATE_PATH.read_text()):
                inst = Instrument.model_validate(raw)
                self.instruments[inst.id] = inst

    def _save(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps([i.model_dump() for i in self.instruments.values()], indent=2))

    # ---- watchlist -------------------------------------------------------
    async def add(self, symbol: str, instrument_type: str, timeframe: str, size: int, lookback_days: int | None) -> Instrument:
        symbol = symbol.strip().upper()
        instrument_type = instrument_type.upper()
        if timeframe not in TIMEFRAMES:
            raise PaperError(f"Timeframe must be one of {list(TIMEFRAMES)}")
        if size <= 0:
            raise PaperError("Quantity / lots must be at least 1")
        max_days = TIMEFRAMES[timeframe][2]
        lookback = min(lookback_days or DEFAULT_LOOKBACK_DAYS[timeframe], max_days - 30)
        now = now_ist()

        if instrument_type == "EQUITY":
            match = await self.provider.resolve_equity(symbol)
            if match is None:
                raise PaperError(f"{symbol}: NSE equity not found")
            fields = dict(quantity=size, token=match.token, trading_symbol=match.trading_symbol, exch_seg=match.exch_seg)
        elif instrument_type == "FUTURE":
            today = now.date().isoformat()
            contracts = [c for c in await scrip_master.get_future_contracts(symbol) if c.underlying == symbol and c.expiry >= today]
            if not contracts:
                raise PaperError(f"{symbol}: no live futures contract found")
            c = min(contracts, key=lambda x: x.expiry)
            fields = dict(
                quantity=size * c.lot_size, lots=size, lot_size=c.lot_size, token=c.token,
                trading_symbol=c.trading_symbol, exch_seg=c.exch_seg, expiry=c.expiry,
            )
        else:
            raise PaperError("instrument_type must be EQUITY or FUTURE")

        inst = Instrument(
            id=uuid.uuid4().hex[:10], symbol=symbol, instrument_type=instrument_type, timeframe=timeframe,
            from_date=(now - dt.timedelta(days=lookback)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
            added_at=now.isoformat(), **fields,
        )
        self.instruments[inst.id] = inst
        self._save()
        await self.refresh_one(inst)
        return inst

    def remove(self, instrument_id: str) -> None:
        self.instruments.pop(instrument_id, None)
        self.snapshots.pop(instrument_id, None)
        self.errors.pop(instrument_id, None)
        self._save()

    # ---- evaluation ------------------------------------------------------
    async def _build_snapshot(self, inst: Instrument) -> dict:
        interval, minutes, max_days = TIMEFRAMES[inst.timeframe]
        now = now_ist()
        from_dt = dt.datetime.fromisoformat(inst.from_date)
        added_at = dt.datetime.fromisoformat(inst.added_at)
        days_back = (now - from_dt).days + 2
        if days_back > max_days:
            raise PaperError(
                f"Running longer than Angel One serves {inst.timeframe} history ({max_days} days) — remove and re-add this instrument."
            )

        raw = await self.provider.get_intraday_ohlc(inst.exch_seg, inst.token, interval, days_back, include_partial=True)
        raw = raw[raw.index >= from_dt]
        if raw.empty:
            raise PaperError("No candle data returned by Angel One")
        confirmed = drop_incomplete_trailing_daily_bar(raw, now) if minutes is None else drop_incomplete_trailing_bars(raw, minutes, now)
        if len(confirmed) < 5:
            raise PaperError("Not enough confirmed candles yet")

        dates = [t.to_pydatetime() for t in confirmed.index]
        closes = [float(c) for c in confirmed["close"]]
        signals, trades = run_top_bottom_backtest(
            inst.symbol, inst.trading_symbol, inst.expiry, "STOCK", inst.quantity, dates, closes, list(confirmed["open"]),
        )
        ltp = float(raw["close"].iloc[-1])

        swings = detect_confirmed_swings(dates, closes, SWING_LEFT_BARS, SWING_RIGHT_BARS)
        tops = [s for s in swings if s.kind == "TOP"]
        bottoms = [s for s in swings if s.kind == "BOTTOM"]

        entry_signals = {(s.signal_date, s.direction): s for s in signals}
        rows, position = [], None
        for n, t in enumerate(trades, 1):
            is_open = t.exit_reason == "END_OF_BACKTEST"
            sig = entry_signals.get((t.entry_date, t.direction))
            exit_price = ltp if is_open else t.exit_price
            pts = (exit_price - t.entry_price) if t.direction == "BUY" else (t.entry_price - exit_price)
            row = {
                "n": n,
                "direction": t.direction,
                "live": t.entry_date >= added_at,
                "status": "OPEN" if is_open else ("WIN" if pts > 0 else "LOSS" if pts < 0 else "BREAKEVEN"),
                "entry_date": _iso(t.entry_date),
                "entry_price": round(t.entry_price, 2),
                "trigger_close": round(closes[sig.signal_index], 2) if sig else None,
                "initial_sl": round(t.initial_sl, 2),
                "active_stop": round(t.active_stop, 2),
                "exit_date": None if is_open else _iso(t.exit_date),
                "exit_price": None if is_open else round(t.exit_price, 2),
                "pnl_points": round(pts, 2),
                "pnl_amount": round(pts * inst.quantity, 2),
                "pnl_pct": round(pts / t.entry_price * 100.0, 2) if t.entry_price else 0.0,
            }
            rows.append(row)
            if is_open:
                position = row

        live_closed = [r for r in rows if r["live"] and r["status"] != "OPEN"]
        wins = [r for r in live_closed if r["pnl_amount"] > 0]
        stats = {
            "live_trades": len(live_closed),
            "live_wins": len(wins),
            "live_win_rate": round(len(wins) / len(live_closed) * 100.0, 1) if live_closed else None,
            "live_realized": round(sum(r["pnl_amount"] for r in live_closed), 2),
            "unrealized": position["pnl_amount"] if position else 0.0,
            "replay_trades": len([r for r in rows if not r["live"] and r["status"] != "OPEN"]),
            "replay_realized": round(sum(r["pnl_amount"] for r in rows if not r["live"] and r["status"] != "OPEN"), 2),
        }

        system_line, stop_line = _build_level_lines(trades)
        markers = []
        for t in trades:
            markers.append({
                "time": _ts(t.entry_date), "position": "belowBar" if t.direction == "BUY" else "aboveBar",
                "shape": "arrowUp" if t.direction == "BUY" else "arrowDown",
                "color": "#22c55e" if t.direction == "BUY" else "#ef4444",
                "text": f"{t.direction} {t.entry_price:.2f}",
            })

        def _line(points):
            out: dict[int, float] = {}
            for p in points:
                out[_ts(p.date)] = round(p.value, 2)  # one value per bar; the latest wins
            return [{"time": k, "value": v} for k, v in sorted(out.items())]

        return {
            "instrument": inst.model_dump(),
            "ltp": round(ltp, 2),
            "last_confirmed_bar": _iso(dates[-1]),
            "updated_at": _iso(now),
            "latest_top": round(tops[-1].price, 2) if tops else None,
            "latest_bottom": round(bottoms[-1].price, 2) if bottoms else None,
            "position": position,
            "stats": stats,
            "trades": rows[::-1],
            "candles": [
                {"time": _ts(t.to_pydatetime()), "open": r.open, "high": r.high, "low": r.low, "close": r.close}
                for t, r in raw.iterrows()
            ],
            "added_marker": _ts(added_at),
            "system_line": _line(system_line),
            "stop_line": _line(stop_line),
            "markers": sorted(markers, key=lambda m: m["time"]),
        }

    async def refresh_one(self, inst: Instrument) -> None:
        try:
            self.snapshots[inst.id] = await self._build_snapshot(inst)
            self.errors.pop(inst.id, None)
        except Exception as exc:  # one bad symbol must never stop the others
            log.exception("refresh failed for %s", inst.symbol)
            self.errors[inst.id] = str(exc) or exc.__class__.__name__

    async def refresh_all(self) -> None:
        async with self._lock:
            for inst in list(self.instruments.values()):
                await self.refresh_one(inst)
                await asyncio.sleep(0.4)  # stay under Angel One's candle-API rate limit
            self.last_tick = now_ist()

    async def _loop(self) -> None:
        while True:
            try:
                await self.refresh_all()
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(20 if market_open() else 300)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    # ---- views -----------------------------------------------------------
    def overview(self) -> dict:
        items = []
        for inst in self.instruments.values():
            snap = self.snapshots.get(inst.id)
            items.append({
                "instrument": inst.model_dump(),
                "error": self.errors.get(inst.id),
                "ltp": snap and snap["ltp"],
                "position": snap and snap["position"],
                "stats": snap and snap["stats"],
            })
        stats = [i["stats"] for i in items if i["stats"]]
        trades = sum(s["live_trades"] for s in stats)
        wins = sum(s["live_wins"] for s in stats)
        return {
            "market_open": market_open(),
            "last_tick": _iso(self.last_tick),
            "items": items,
            "totals": {
                "live_realized": round(sum(s["live_realized"] for s in stats), 2),
                "unrealized": round(sum(s["unrealized"] for s in stats), 2),
                "live_trades": trades,
                "live_win_rate": round(wins / trades * 100.0, 1) if trades else None,
                "open_positions": len([i for i in items if i["position"]]),
            },
        }
