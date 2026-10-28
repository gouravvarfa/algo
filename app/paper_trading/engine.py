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
from app.live import candle_store
from app.ops import flags as ops_flags, incidents as ops_incidents
from app.ops.stats import tracked
from app.live import chain_eval
from app.live.chain_eval import ContractRef
from app.live.contracts import cutoff as contract_cutoff
from app.live.ticks import TickFeed, merge_minutes
from app.providers import angelone_scrip_master as scrip_master
from app.providers.angelone_provider import AngelOneProvider
from app.strategy.top_bottom.gap_protocol import Bar15
from app.strategy.top_bottom.intraday import ENTRY_DAY_STOP_PCT, Minute, STRUCT_CAP_DISTANCE, run_intraday
from app.strategy.top_bottom.backtest import SWING_LEFT_BARS, SWING_RIGHT_BARS, run_top_bottom_backtest
from app.strategy.top_bottom.strategy import _build_level_lines
from app.strategy.top_bottom.swing_detection import detect_confirmed_swings

log = logging.getLogger("paper")
IST = ZoneInfo("Asia/Kolkata")
STATE_PATH = Path("data/watchlist.json")
# The Oct contract's history was approved as shown on 2026-10-21 (1-minute rules from this day, daily-close rules before it); the contract rule must not
# rewrite it, so its intraday window is pinned here instead of sliding with the REST window.
FIRST_CONTRACT_MINUTES_FROM = dt.date(2026, 10, 12)
CONTRACTS_PATH = Path("data/paper_trading/contracts.json")      # every futures contract seen per symbol (expired ones disappear from Angel One's master)
JOURNAL_PATH = Path("data/paper_trading/journal.jsonl")         # append-only log of live events
JOURNAL_SEEN = Path("data/paper_trading/journal_seen.json")     # last known state per live trade (no duplicates after restart)

# timeframe -> (Angel One interval, bar minutes, max days one request may span)
TIMEFRAMES: dict[str, tuple[str, int | None, int]] = {
    "1m": ("ONE_MINUTE", 1, 25),
    "5m": ("FIVE_MINUTE", 5, 90),
    "15m": ("FIFTEEN_MINUTE", 15, 180),
    "30m": ("THIRTY_MINUTE", 30, 180),
    "1H": ("ONE_HOUR", 60, 380),
    "1D": ("ONE_DAY", None, 1900),
}
LIVE_MINUTES_DAYS = 12          # calendar days of 1-minute candles used for the live intraday rules
LIVE_MINUTES_REFRESH_SECONDS = 30
CANDLE_CALL_GAP = 0.7                 # seconds between any two Angel One candle requests
LIVE_MINUTES_REFRESH_WITH_FEED = 120   # the WebSocket supplies the recent minutes, so REST is only needed for history
DAILY_RAW_CACHE_WITH_FEED = 120
CANDLE_SHARE_SECONDS = 15              # identical candle requests made by different users within this time share one Angel One call
ALL = object()                        # "every owner" (internal callers and tests)
LEGACY_OWNER_PATH = Path("data/paper_trading/legacy_owner.txt")   # the account that adopted the pre-platform watchlist and journal
STOCK_BREAKEVEN_PCT = 0.03          # stocks arm the breakeven stop at +3% profit; the indexes (BankNifty / Nifty) keep +2%


def breakeven_pct_for(symbol: str) -> float | None:
    """None = the strategy's own index rule (2%). Every other symbol is a stock future: 3%."""
    return None if symbol in INDEX_TOKENS else STOCK_BREAKEVEN_PCT


STOCK_STRUCT_CAP = 0.03             # stocks: a structural stop 3%+ away from the entry keeps the entry-day 1% stop (the indexes use 2%)


def struct_cap_for(symbol: str) -> float:
    return STRUCT_CAP_DISTANCE if symbol in INDEX_TOKENS else STOCK_STRUCT_CAP


INDEX_TOKENS = {"BANKNIFTY": ("NSE", "99926009"), "NIFTY": ("NSE", "99926000")}
LIVE_RATE_LIMIT_COOLDOWN_SECONDS = 90   # after an Angel One 'Too many requests', do not ask for 1-minute data again for this long
LIVE_REUSE_MAX_SECONDS = 20 * 60        # a previous good intraday result may be shown (clearly labelled) for at most this long
GAP_15M_REFRESH_SECONDS = 600  # 15m bars for the daily gap protocol are re-fetched at most this often
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
    owner: str | None = None  # platform user id; None = the single-user / open-mode watchlist


class PaperError(ValueError):
    pass


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST).replace(tzinfo=None)


def market_open(now: dt.datetime | None = None) -> bool:
    now = now or now_ist()
    return now.weekday() < 5 and dt.time(9, 15) <= now.time() <= dt.time(15, 30)


def trim_partial_first_day(mins: list, lenient: bool = False) -> list:
    """The REST window starts N days back at the current time of day, so its FIRST day is usually only a partial session. A
    partial first day hides the morning crossings that happened before the window began (the engine then 'enters' on the
    window's first minute, at a wrong price and time, and the result changes every time the window moves). Drop leading minutes
    until the first complete session (it must start by 09:20); earlier history is rebuilt from the daily candles instead.
    `lenient` (stock futures): a thinly traded contract may print its first trade of a day after 09:20 even in a complete session, so only the
    first (partial) day is dropped and the next day is accepted as it is."""
    if not mins:
        return mins
    first_day = mins[0].ts.date()
    if mins[0].ts.time() <= dt.time(9, 20):
        return mins
    rest = [m for m in mins if m.ts.date() > first_day]
    if lenient:
        return rest
    return rest if rest and rest[0].ts.time() <= dt.time(9, 20) else []


def _candles_with_tick(raw: pd.DataFrame, tick_ltp: float | None) -> list[dict]:
    """Chart candles from Angel One; the forming (last) candle's close/high/low follow the live tick so the chart is current."""
    out = [{"time": _ts(t.to_pydatetime()), "open": r.open, "high": r.high, "low": r.low, "close": r.close} for t, r in raw.iterrows()]
    if out and tick_ltp is not None:
        last = out[-1]
        last["close"], last["high"], last["low"] = tick_ltp, max(last["high"], tick_ltp), min(last["low"], tick_ltp)
    return out


def _ts(d: dt.datetime) -> int:
    # IST wall-clock encoded as if UTC, so the chart axis reads in IST.
    return calendar.timegm(d.timetuple())


def _iso(d: dt.datetime | None) -> str | None:
    return d.isoformat(sep=" ", timespec="minutes") if d else None


class PaperEngine:
    def __init__(self, provider) -> None:       # AngelOneProvider or UpstoxProvider: the same methods, the data source is chosen in app/main.py
        self.provider = provider
        self.index_tokens = getattr(provider, "index_tokens", INDEX_TOKENS)      # index name -> (exchange, token) in THIS data source
        self.instruments: dict[str, Instrument] = {}
        self._foreign: list[dict] = []           # saved instruments of ANOTHER data source (Angel One tokens while running on Upstox): kept in the file, never used here
        self.snapshots: dict[str, dict] = {}
        self.errors: dict[str, str] = {}
        self.faults: dict[str, dict] = {}        # instrument id -> {message, since}: an instrument whose evaluation keeps failing (isolated; the others go on)
        self.last_tick: dt.datetime | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._bars15: dict[str, tuple[dt.datetime, dict]] = {}  # instrument id -> (fetched at, date -> [Bar15])
        self._minutes: dict[str, tuple[dt.datetime, list]] = {}  # instrument id -> (fetched at, 1-minute candles)
        make_feed = getattr(provider, "make_feed", None)
        self.feed = make_feed() if make_feed else TickFeed(provider.get_session, provider.reset_session)
        self._raw_cache: dict[str, tuple[dt.datetime, pd.DataFrame]] = {}
        self._prev_close: dict[str, tuple] = {}
        self._live_good: dict[str, tuple[dt.datetime, list, str]] = {}  # instrument id -> (evaluated at, trades, note) of the last SUCCESSFUL intraday run
        self._cooldown_until: dict = {}
        self._chain_cache: dict = {}
        self._load()

    # ---- persistence -----------------------------------------------------
    def _load(self) -> None:
        if STATE_PATH.exists():
            seen: dict[tuple, str] = {}
            dropped = 0
            accepts = getattr(self.provider, "accepts_token", None)
            for raw in json.loads(STATE_PATH.read_text()):
                if accepts and not accepts(str(raw.get("token", ""))):
                    self._foreign.append(raw)
                    continue
                inst = Instrument.model_validate(raw)
                key = (inst.owner, inst.symbol, inst.instrument_type, inst.timeframe)
                if key in seen:                  # the same strategy added twice for one person (two opens of the page at once): keep the first
                    dropped += 1
                    continue
                seen[key] = inst.id
                self.instruments[inst.id] = inst
            if dropped:
                log.warning("removed %d duplicate instrument(s) from the watchlist", dropped)
                self._save()

    def _save(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps([i.model_dump() for i in self.instruments.values()] + self._foreign, indent=2))

    # ---- watchlist -------------------------------------------------------
    async def add(self, symbol: str, instrument_type: str, timeframe: str, size: int, lookback_days: int | None, owner: str | None = None) -> Instrument:
        symbol = symbol.strip().upper()
        instrument_type = instrument_type.upper()
        if timeframe not in TIMEFRAMES:
            raise PaperError(f"Timeframe must be one of {list(TIMEFRAMES)}")
        if size <= 0:
            raise PaperError("Quantity / lots must be at least 1")
        for existing in self.instruments.values():       # adding the same strategy again for the same person returns the one they already have
            if existing.owner == owner and existing.symbol == symbol and existing.instrument_type == instrument_type and existing.timeframe == timeframe:
                return existing
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
            contracts = [c for c in await self._future_contracts(symbol) if c.underlying == symbol and c.expiry >= today]
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
            added_at=now.isoformat(), owner=owner, **fields,
        )
        self.instruments[inst.id] = inst
        self._save()
        self.feed.subscribe(inst.exch_seg, inst.token)
        await self.refresh_one(inst)
        return inst

    def remove(self, instrument_id: str, owner=ALL) -> bool:
        if owner is not ALL and (self.instruments.get(instrument_id) is None or self.instruments[instrument_id].owner != owner):
            return False                              # not yours (or not there): nothing is removed
        gone = self.instruments.pop(instrument_id, None)
        if gone is not None and not any(i.token == gone.token for i in self.instruments.values()):
            self.feed.unsubscribe_all_for(gone.token)
        self._raw_cache.pop(instrument_id, None)
        self.snapshots.pop(instrument_id, None)
        self._bars15.pop(instrument_id, None)
        self._minutes.pop(instrument_id, None)
        self._live_good.pop(instrument_id, None)
        self._cooldown_until.pop(instrument_id, None)
        self.errors.pop(instrument_id, None)
        self.faults.pop(instrument_id, None)
        self._save()
        return True

    def owned(self, instrument_id: str, owner) -> Instrument | None:
        inst = self.instruments.get(instrument_id)
        return inst if inst is not None and (owner is ALL or inst.owner == owner) else None

    def adopt_orphans(self, owner_id: str) -> int:
        """The watchlist and journal that existed before user accounts belong to the first account (the owner). Idempotent."""
        n = 0
        for inst in self.instruments.values():
            if inst.owner is None:
                inst.owner, n = owner_id, n + 1
        if n:
            self._save()
        try:
            if not LEGACY_OWNER_PATH.exists():
                LEGACY_OWNER_PATH.parent.mkdir(parents=True, exist_ok=True)
                LEGACY_OWNER_PATH.write_text(owner_id)
        except OSError:
            pass
        return n

    # ---- evaluation ------------------------------------------------------
    @tracked("engine.build_snapshot")
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

        rc = self._raw_cache.get(inst.id)
        ttl = DAILY_RAW_CACHE_WITH_FEED if self.feed.healthy(inst.token) else 15
        if rc and (now - rc[0]).total_seconds() < ttl:
            raw = rc[1]
        else:
            raw = await self._candles(inst.exch_seg, inst.token, interval, days_back, include_partial=True)
            self._raw_cache[inst.id] = (now, raw)
        raw = raw[raw.index >= from_dt]
        if raw.empty:
            raise PaperError("No candle data returned by Angel One")
        confirmed = drop_incomplete_trailing_daily_bar(raw, now) if minutes is None else drop_incomplete_trailing_bars(raw, minutes, now)
        if len(confirmed) < 5:
            raise PaperError("Not enough confirmed candles yet")

        dates = [t.to_pydatetime() for t in confirmed.index]
        closes = [float(c) for c in confirmed["close"]]
        # Daily timeframe: real 15-minute candles drive the gap protocol (only
        # for days Angel One still serves; older days use the close rules).
        intraday = await self._gap_bars(inst, from_dt, now) if minutes is None else None
        chain = None
        if minutes is None and inst.instrument_type == "FUTURE":
            try:
                chain = await self._chain(inst, now, from_dt)
            except Exception:
                log.exception("contract chain evaluation failed for %s; using the single-contract result", inst.symbol)
        active, rolls, roll_marks, prev_line = inst, [], [], None
        if chain is not None:
            signals, trades = [], chain.trades
            ac = chain.active
            active = inst.model_copy(update={"token": ac.token, "trading_symbol": ac.trading_symbol, "expiry": ac.expiry.isoformat(), "exch_seg": ac.exch_seg})
            ad = chain.active_daily
            dates, closes = [t.to_pydatetime() for t in ad.index], [float(x) for x in ad["close"]]
            start_active = chain.segments[-1].start
            raw_active = (await self._contract_daily(ac, from_dt, now))
            # the chart line is the ACTIVE contract's own price wherever it has history (its System Points are the ones the rules use);
            # before that, the earlier contract. The earlier contract over the overlap is returned separately as a faint reference line.
            first_active = raw_active.index[0] if len(raw_active) else None
            older = chain.chart if first_active is None else chain.chart[chain.chart.index < first_active]
            raw = pd.concat([older, raw_active]) if len(raw_active) else chain.chart
            overlap = chain.chart if first_active is None else chain.chart[chain.chart.index >= first_active]
            prev_line = {"contract": " + ".join(sg.contract.trading_symbol for sg in chain.segments[:-1]),
                         "data": [{"time": _ts(t.to_pydatetime()), "value": float(r.close)} for t, r in overlap.iterrows()]}
            rolls = chain.rolls
            engine_note = (f"Contract chain: {' → '.join(sg.contract.trading_symbol for sg in chain.segments)}. A position still open at a contract's cutoff "
                           f"(8 days before expiry) is closed there and continued in the next contract as a fresh entry with that contract's own System Points; "
                           f"real intraday rules (1% entry-day stop, capped stop, breakeven, gap) apply in each contract")
            engine_warn = bool(chain.warnings)
            if chain.warnings:
                engine_note = "WARNING: " + "; ".join(chain.warnings) + ". " + engine_note
        else:
            signals, trades = run_top_bottom_backtest(
                inst.symbol, inst.trading_symbol, inst.expiry, "STOCK", inst.quantity, dates, closes, list(confirmed["open"]),
                intraday, [float(h) for h in confirmed["high"]], [float(x) for x in confirmed["low"]], breakeven_pct_for(inst.symbol),
            )
            engine_note, engine_warn = "Daily-close rules only", False
            if minutes is None:
                live = await self._live_intraday(inst, confirmed, trades, now)
                if live is not None:
                    trades, engine_note = live
                    engine_warn = engine_note.startswith("WARNING")
                else:
                    engine_note, engine_warn = "WARNING: intraday (1-minute) data unavailable right now, showing the daily-close result. A stop touched today may not be reflected until the next successful update", True
        ltp = float(raw["close"].iloc[-1])
        tick = self.feed.latest.get(active.token)
        live_tick = self.feed.healthy(active.token) and tick is not None
        if live_tick:
            ltp = tick.ltp

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
                "contract": t.contract or inst.trading_symbol,
                "rolled_in": t.rolled_in,
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
                "exit_reason": None if is_open else t.exit_reason,
                "breakeven": t.breakeven_date is not None,
                "gap_state": t.gap_state,
                "protective_sl": t.protective_sl if t.protective_sl_date == now.date() else None,
            }
            rows.append(row)
            if is_open:
                position = row

        # After a 2%-breakeven exit the strategy stays flat and waits for a NEW System Point (no reversal).
        waiting = position is None and bool(rows) and rows[-1].get("exit_reason") in ("BREAKEVEN_EXIT_AFTER_2_PERCENT_PROFIT", "ENTRY_DAY_STOP_EXIT", "CAPPED_1_PERCENT_STOP_EXIT")
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

        # Gap-protocol entries/exits carry an intraday time; on a daily chart they belong to that day's bar.
        bt = (lambda d: _ts(d.replace(hour=0, minute=0, second=0, microsecond=0))) if minutes is None else _ts
        system_line, stop_line = _build_level_lines(trades)
        markers = []
        for r in rolls:
            markers.append({"time": bt(r["time"]), "position": "inBar", "shape": "circle", "color": "#f59e0b",
                            "text": f"ROLL {r['from'][-8:-3]}→{r['to'][-8:-3]}"})
        for t in trades:
            markers.append({
                "time": bt(t.entry_date), "position": "belowBar" if t.direction == "BUY" else "aboveBar",
                "shape": "arrowUp" if t.direction == "BUY" else "arrowDown",
                "color": "#22c55e" if t.direction == "BUY" else "#ef4444",
                "text": f"{t.direction} {t.entry_price:.2f}",
            })
            for when, label in t.events:  # e.g. "2% PROFIT -> BREAKEVEN SL"
                markers.append({"time": bt(when), "position": "inBar", "shape": "square", "color": "#06b6d4", "text": label})

        def _line(points):
            out: dict[int, float] = {}
            for p in points:
                out[bt(p.date)] = round(p.value, 2)  # one value per bar; the latest wins
            return [{"time": k, "value": v} for k, v in sorted(out.items())]

        return {
            "instrument": active.model_dump(),
            "ltp": round(ltp, 2),
            "last_confirmed_bar": _iso(dates[-1]),
            "updated_at": _iso(now),
            "latest_top": round(tops[-1].price, 2) if tops else None,
            "latest_bottom": round(bottoms[-1].price, 2) if bottoms else None,
            "position": position,
            "waiting": waiting,
            "stats": stats,
            "rules": {"breakeven_pct": (breakeven_pct_for(inst.symbol) or 0.02) * 100, "struct_cap_pct": struct_cap_for(inst.symbol) * 100},
            "engine_note": engine_note,
            "engine_warning": engine_warn,
            "trades": rows[::-1],
            "candles": _candles_with_tick(raw, ltp if live_tick else None),
            "prev_contract_line": prev_line,
            # the System Points the rules use (active contract): the last few confirmed Tops / Bottoms, at their pivot day and close
            "system_points": [{"time": _ts(sp.pivot_date.replace(hour=0, minute=0, second=0, microsecond=0)), "kind": sp.kind, "price": round(sp.price, 2)}
                              for sp in sorted(tops[-3:] + bottoms[-3:], key=lambda x: x.pivot_date)],
            "feed": {"live": live_tick, "age_s": self.feed.age(active.token), "connected": self.feed.connected},
            "rolls": [{"time": _iso(r["time"]), "from": r["from"], "to": r["to"], "old_price": r["old_price"], "new_price": r["new_price"]} for r in rolls],
            "added_marker": _ts(added_at),
            "system_line": _line(system_line),
            "stop_line": _line(stop_line),
            "markers": sorted(markers, key=lambda m: m["time"]),
        }

    async def _candles(self, *args, **kwargs):
        """Every Angel One candle request goes through here: at most one at a time and at least CANDLE_CALL_GAP seconds apart,
        so the startup burst (daily + 15m + 1m + index) cannot trip the rate limit."""
        import time as _t
        if not hasattr(self, "_rest_lock"):
            self._rest_lock, self._rest_last = asyncio.Lock(), 0.0
        key = (args, tuple(sorted(kwargs.items())))
        if not hasattr(self, "_shared_candles"):
            self._shared_candles = {}
        async with self._rest_lock:
            hit = self._shared_candles.get(key)
            if hit and _t.monotonic() - hit[0] < CANDLE_SHARE_SECONDS:
                return hit[1].copy()                      # another user's instrument just asked for exactly this
            wait = CANDLE_CALL_GAP - (_t.monotonic() - self._rest_last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                out = await self.provider.get_intraday_ohlc(*args, **kwargs)
                self._shared_candles[key] = (_t.monotonic(), out.copy())
                if len(self._shared_candles) > 200:
                    for k in sorted(self._shared_candles, key=lambda k: self._shared_candles[k][0])[:100]:
                        self._shared_candles.pop(k, None)
                return out
            finally:
                self._rest_last = _t.monotonic()

    async def _future_contracts(self, symbol: str):
        """Live futures contracts of one underlying from the active data source (Angel One: its scrip master; Upstox: its instrument list)."""
        own = getattr(self.provider, "future_contracts", None)
        return await (own(symbol) if own else scrip_master.get_future_contracts(symbol))

    # ---- multi-contract chain (Oct -> Nov -> Dec ...) --------------------------
    async def _known_contracts(self, inst: Instrument) -> list[ContractRef]:
        """Live contracts from Angel One's master plus the ones remembered from earlier runs (an expired contract is no longer in the master)."""
        try:
            known = json.loads(CONTRACTS_PATH.read_text()) if CONTRACTS_PATH.exists() else {}
        except ValueError:
            known = {}
        mine = {c["trading_symbol"]: c for c in known.get(inst.symbol, [])}
        try:
            for c in await self._future_contracts(inst.symbol):
                if c.underlying == inst.symbol:
                    mine[c.trading_symbol] = {"trading_symbol": c.trading_symbol, "token": c.token, "exch_seg": c.exch_seg, "expiry": c.expiry, "lot_size": c.lot_size}
        except Exception:
            log.warning("could not refresh the futures contract list for %s; using the remembered one", inst.symbol)
        known[inst.symbol] = sorted(mine.values(), key=lambda c: c["expiry"])
        CONTRACTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONTRACTS_PATH.write_text(json.dumps(known, indent=1))
        first = inst.expiry or ""
        return [ContractRef(c["trading_symbol"], c["token"], c["exch_seg"], dt.date.fromisoformat(c["expiry"]), c["lot_size"])
                for c in known[inst.symbol] if c["expiry"] >= first]

    async def _contract_daily(self, c: ContractRef, from_dt: dt.datetime, now: dt.datetime) -> pd.DataFrame:
        """Daily candles of one contract: Angel One when it answers, merged with (and falling back to) the local store."""
        key = ("d", c.token)
        hit = self._chain_cache.get(key)
        if hit and (now - hit[0]).total_seconds() < DAILY_RAW_CACHE_WITH_FEED:
            return hit[1]
        stored = candle_store.get(c.token, "ONE_DAY", from_dt)
        try:
            fresh = await self._candles(c.exch_seg, c.token, "ONE_DAY", (now - from_dt).days + 2, include_partial=True)
            candle_store.put(c.token, "ONE_DAY", fresh)
        except Exception as exc:
            log.warning("daily candles for %s unavailable (%s); using the stored copy", c.trading_symbol, str(exc)[:60])
            fresh = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = candle_store.merge(stored, fresh)
        df = df[df.index >= from_dt]
        self._chain_cache[key] = (now, df)
        return df

    async def _contract_minutes(self, c: ContractRef, first_day: dt.date, now: dt.datetime, pin: dt.date | None = None, lenient: bool = False) -> list:
        key = ("m", c.token)
        hit = self._chain_cache.get(key)
        refresh = LIVE_MINUTES_REFRESH_WITH_FEED if self.feed.healthy(c.token) else LIVE_MINUTES_REFRESH_SECONDS
        if hit and (now - hit[0]).total_seconds() < refresh:
            base = hit[1]
        else:
            since = dt.datetime.combine(first_day, dt.time(9, 0))
            stored = candle_store.get(c.token, "ONE_MINUTE", since)
            days = max(LIVE_MINUTES_DAYS, min(TIMEFRAMES["1m"][2] - 1, (now.date() - first_day).days + 3))
            try:
                fresh = await self._candles(c.exch_seg, c.token, "ONE_MINUTE", days, include_partial=True)
                fresh = drop_incomplete_trailing_bars(fresh, 1, now)
                candle_store.put(c.token, "ONE_MINUTE", fresh)
            except Exception as exc:
                log.warning("1-minute candles for %s unavailable (%s); using the stored copy", c.trading_symbol, str(exc)[:60])
                if "too many requests" in str(exc).lower():
                    self._cooldown_until[("c", c.token)] = now + dt.timedelta(seconds=LIVE_RATE_LIMIT_COOLDOWN_SECONDS)
                fresh = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            df = candle_store.merge(stored, fresh)
            base = [Minute(ts.to_pydatetime(), float(r.open), float(r.high), float(r.low), float(r.close)) for ts, r in df.iterrows()]
            self._chain_cache[key] = (now, base)
        out = trim_partial_first_day(merge_minutes(base, self.feed.builder.minutes(c.token)), lenient)
        return [m for m in out if m.ts.date() >= pin] if pin else out

    @tracked("chain_eval.chain")
    async def _chain(self, inst: Instrument, now: dt.datetime, from_dt: dt.datetime):
        """Run the strategy across consecutive contracts (see app/live/chain_eval.py). None = no cutoff has passed yet: single-contract path."""
        contracts = await self._known_contracts(inst)
        if len(contracts) < 2:
            return None
        for c in contracts:
            self.feed.subscribe(c.exch_seg, c.token)
        first_day = (from_dt.date() if from_dt else now.date())
        gap_bars = await self._gap_bars(inst, from_dt, now)
        from app.strategy.top_bottom.warmup import reconstruct_state

        async def get_daily(c):
            df = await self._contract_daily(c, from_dt, now)
            return drop_incomplete_trailing_daily_bar(df, now) if len(df) else df

        async def get_minutes(c):
            if c.token == contracts[0].token:
                # the index chain has tracked 1-minute data from a fixed day; a stock future starts on the day it was assigned
                if inst.symbol in INDEX_TOKENS:
                    start = FIRST_CONTRACT_MINUTES_FROM
                else:   # a stock: from a week before the first contract's cutoff (or the day it was assigned, if that is earlier), so the roll can be priced
                    start = min(dt.date.fromisoformat(inst.added_at[:10]), contract_cutoff(contracts[0].expiry) - dt.timedelta(days=6))
                return await self._contract_minutes(c, start, now, start, lenient=inst.symbol not in INDEX_TOKENS)
            return await self._contract_minutes(c, contract_cutoff(contracts[0].expiry), now, lenient=inst.symbol not in INDEX_TOKENS)

        def daily_engine(c, df):
            _sig, trs = run_top_bottom_backtest(inst.symbol, c.trading_symbol, c.expiry.isoformat(), "STOCK", inst.quantity,
                                                [t.to_pydatetime() for t in df.index], [float(x) for x in df["close"]], list(df["open"]),
                                                gap_bars if c.token == inst.token else None, [float(h) for h in df["high"]], [float(x) for x in df["low"]],
                                                breakeven_pct_for(inst.symbol))
            return trs

        return await chain_eval.evaluate(
            contracts=contracts, from_dt=from_dt, now=now, symbol=inst.symbol, get_daily=get_daily, get_minutes=get_minutes,
            daily_engine=daily_engine, reconstruct=lambda rows, fd: reconstruct_state(rows, fd, breakeven_pct_for(inst.symbol)),
            entry_day_stop_pct=ENTRY_DAY_STOP_PCT, struct_cap_distance=struct_cap_for(inst.symbol), breakeven_pct=breakeven_pct_for(inst.symbol))

    async def _live_intraday(self, inst: Instrument, confirmed: pd.DataFrame, daily_trades: list, now: dt.datetime):
        """Live version of the replay's hybrid mode. Structure (System Points) still comes from confirmed DAILY closes;
        the last LIVE_MINUTES_DAYS days are executed on real 1-minute candles, so a stop touch exits at the stop and the
        position reverses at once (always-reversing), breakeven and the 15-minute gap rules fire intraday, exactly as in the
        Index Replay. Earlier history keeps the daily-close result. Returns (trades, note) or None to keep the daily result
        (any failure here must never take the instrument down)."""
        good = self._live_good.get(inst.id)

        def reuse(why: str):
            if good and (now - good[0]).total_seconds() < LIVE_REUSE_MAX_SECONDS:
                return good[1], f"WARNING: {why}. Showing the last successful intraday evaluation from {good[0]:%H:%M:%S}"
            return None

        cool = self._cooldown_until.get(inst.id)
        if cool and now < cool:
            return reuse("Angel One rate limit, waiting before the next 1-minute request")
        try:
            cached = self._minutes.get(inst.id)
            refresh = LIVE_MINUTES_REFRESH_WITH_FEED if self.feed.healthy(inst.token) else LIVE_MINUTES_REFRESH_SECONDS
            if cached and (now - cached[0]).total_seconds() < refresh:
                mins = cached[1]
            else:
                df = await self._candles(inst.exch_seg, inst.token, "ONE_MINUTE", LIVE_MINUTES_DAYS, include_partial=True)
                df = drop_incomplete_trailing_bars(df, 1, now)
                mins = [Minute(ts.to_pydatetime(), float(r.open), float(r.high), float(r.low), float(r.close)) for ts, r in df.iterrows()]
                self._minutes[inst.id] = (now, mins)
            mins = trim_partial_first_day(merge_minutes(mins, self.feed.builder.minutes(inst.token)), inst.symbol not in INDEX_TOKENS)   # REST is authoritative; ticks fill the closing / forming minute
            if len(mins) < 30:
                return None
            from app.strategy.top_bottom.warmup import reconstruct_state
            rows = [(t.to_pydatetime(), float(r.open), float(r.high), float(r.low), float(r.close)) for t, r in confirmed.iterrows()]
            if len(rows) < 5:
                return None
            first_day = mins[0].ts.date()      # run_intraday never uses a day's own daily candle on that day, so no filtering is needed
            initial, _warn = reconstruct_state(rows, first_day, breakeven_pct_for(inst.symbol))
            res = run_intraday(inst.symbol, [r[0] for r in rows], [r[4] for r in rows], mins, initial, entry_day_stop_pct=ENTRY_DAY_STOP_PCT, struct_cap_distance=struct_cap_for(inst.symbol),
                               breakeven_pct=breakeven_pct_for(inst.symbol))
            older = [t for t in daily_trades if t.exit_reason != "END_OF_BACKTEST" and t.exit_date is not None and t.exit_date.date() < first_day]
            note = f"Intraday rules live since {first_day} (real 1-minute candles: stop touch exits and reverses, breakeven and gap rules intraday, {ENTRY_DAY_STOP_PCT * 100:g}% stop on the entry day, and while the structural stop is {struct_cap_for(inst.symbol) * 100:g}%+ away); earlier history uses daily closes"
            out = older + res.trades
            self._live_good[inst.id] = (now, out, note)
            return out, note
        except Exception as exc:
            log.warning("live intraday evaluation failed for %s: %s", inst.symbol, exc)
            if "too many requests" in str(exc).lower() or "rate" in str(exc).lower():
                self._cooldown_until[inst.id] = now + dt.timedelta(seconds=LIVE_RATE_LIMIT_COOLDOWN_SECONDS)
            return reuse(f"intraday data fetch failed ({str(exc)[:60]})")

    async def _gap_bars(self, inst: Instrument, from_dt: dt.datetime, now: dt.datetime) -> dict | None:
        """Completed 15m candles grouped by day, cached for a few minutes. A
        failure here never blocks the instrument: the close rules still run."""
        cached = self._bars15.get(inst.id)
        if cached and not self._new_15m_candle_closed(cached[0], now) and (now - cached[0]).total_seconds() < GAP_15M_REFRESH_SECONDS:
            return cached[1]
        try:
            days = min((now - from_dt).days + 2, TIMEFRAMES["15m"][2])
            df = await self._candles(inst.exch_seg, inst.token, "FIFTEEN_MINUTE", days)
        except Exception:
            log.exception("15m fetch for gap protocol failed for %s", inst.symbol)
            return cached[1] if cached else None
        out: dict = {}
        for ts, r in df.iterrows():
            start = ts.to_pydatetime()
            out.setdefault(start.date(), []).append(Bar15(start, float(r.open), float(r.high), float(r.low), float(r.close)))
        self._bars15[inst.id] = (now, out)
        return out

    @staticmethod
    def _new_15m_candle_closed(fetched_at: dt.datetime, now: dt.datetime) -> bool:
        """True once a 15-minute candle has completed after the last fetch (+20 s for Angel One to publish it), so the
        gap protocol sees the 09:15-09:30 candle (and every later one) right after it closes, not up to 10 minutes late."""
        if not market_open(now):
            return False
        boundary = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
        return fetched_at < boundary + dt.timedelta(seconds=20) <= now

    # ---- live journal ------------------------------------------------------
    def _journal(self, inst: Instrument, snap: dict) -> None:
        """Append ENTRY / EXIT / SL->BREAKEVEN / GAP events for LIVE trades the first time they are seen.
        Append-only: a later recomputation never rewrites what was recorded."""
        try:
            seen = json.loads(JOURNAL_SEEN.read_text()) if JOURNAL_SEEN.exists() else {}
        except ValueError:
            seen = {}
        first_run = inst.id not in seen                 # events found on the very first pass are history, not "just happened"
        mine = seen.setdefault(inst.id, {})
        events, now = [], _iso(now_ist())
        for r in snap["trades"]:
            if not r["live"]:
                continue
            key = f"{r['entry_date']}|{r['direction']}"
            old = mine.get(key)
            base = {"at": now, "backfill": first_run, "owner": getattr(inst, "owner", None), "instrument_id": inst.id, "symbol": inst.symbol, "trading_symbol": inst.trading_symbol,
                    "direction": r["direction"], "entry_date": r["entry_date"], "entry_price": r["entry_price"], "quantity": inst.quantity}
            if old is None:
                events.append({**base, "event": "ENTRY", "initial_sl": r["initial_sl"], "active_stop": r["active_stop"]})
                if r["status"] != "OPEN":
                    events.append({**base, "event": "EXIT", "exit_date": r["exit_date"], "exit_price": r["exit_price"],
                                   "reason": r["exit_reason"], "pnl_amount": r["pnl_amount"]})
            else:
                if old["status"] == "OPEN" and r["status"] != "OPEN":
                    events.append({**base, "event": "EXIT", "exit_date": r["exit_date"], "exit_price": r["exit_price"],
                                   "reason": r["exit_reason"], "pnl_amount": r["pnl_amount"]})
                if r["breakeven"] and not old.get("breakeven"):
                    events.append({**base, "event": "SL_TO_BREAKEVEN", "active_stop": r["active_stop"]})
                if r.get("gap_state") and r.get("gap_state") != old.get("gap_state"):
                    events.append({**base, "event": "GAP", "gap_state": r["gap_state"], "protective_sl": r.get("protective_sl")})
            mine[key] = {"status": r["status"], "breakeven": r["breakeven"], "gap_state": r.get("gap_state")}
        JOURNAL_SEEN.parent.mkdir(parents=True, exist_ok=True)
        if not events:
            JOURNAL_SEEN.write_text(json.dumps(seen))
            return
        with JOURNAL_PATH.open("a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
                log.info("JOURNAL %s %s %s", e["event"], e["symbol"], e.get("reason") or e.get("gap_state") or e.get("entry_price"))
        JOURNAL_SEEN.write_text(json.dumps(seen))

    def journal(self, limit: int = 300, owner=ALL) -> list[dict]:
        if not JOURNAL_PATH.exists():
            return []
        lines = JOURNAL_PATH.read_text(encoding="utf-8").splitlines()
        if owner is not ALL:
            try:
                legacy = LEGACY_OWNER_PATH.read_text().strip() or None
            except OSError:
                legacy = None
            # an event without an owner was written before user accounts existed: it belongs to the account that adopted that watchlist
            events = [e for e in (json.loads(x) for x in lines if x.strip()) if e.get("owner", legacy) == owner or (owner is None and "owner" not in e)]
            return list(reversed(events[-limit:]))
        return [json.loads(x) for x in reversed(lines[-limit:]) if x.strip()]

    async def refresh_one(self, inst: Instrument) -> None:
        owner = getattr(inst, "owner", None)
        paused = ops_flags.engine_paused(owner)
        if paused and inst.id in self.snapshots:    # kill switch / strategy OFF: keep the last stored snapshot, evaluate nothing
            return
        try:
            # first look after a restart while paused: build ONE read-only snapshot so the page has something to show (nothing is journaled)
            self.snapshots[inst.id] = await self._build_snapshot(inst)
            if paused:
                self.snapshots[inst.id]["paused"] = True
                self.errors.pop(inst.id, None)
                return
            self.errors.pop(inst.id, None)
            if self.faults.pop(inst.id, None) is not None:
                ops_incidents.record("engine", f"{inst.symbol} recovered", severity="info", user_id=owner, auto_action="fault cleared")
            self._journal(inst, self.snapshots[inst.id])
        except Exception as exc:  # one bad instrument (or user) must never stop the others: isolate it, record it, go on
            log.exception("refresh failed for %s", inst.symbol)
            msg = str(exc) or exc.__class__.__name__
            self.errors[inst.id] = msg
            if inst.id not in self.faults:
                self.faults[inst.id] = {"message": f"{type(exc).__name__}: {msg[:200]}", "since": now_ist().isoformat(timespec="seconds")}
                ops_incidents.record("engine", f"{inst.symbol} evaluation failed: {type(exc).__name__}: {msg[:200]}", user_id=owner,
                                     auto_action="instrument isolated; other instruments keep running")

    async def refresh_all(self) -> None:
        async with self._lock:
            for inst in list(self.instruments.values()):
                await self.refresh_one(inst)
                await asyncio.sleep(0.4)  # stay under Angel One's candle-API rate limit
            if not ops_flags.engine_paused():
                self.last_tick = now_ist()

    async def _loop(self) -> None:
        while True:
            try:
                await self.refresh_all()
            except Exception:
                log.exception("tick failed")
            if market_open() and self.feed.healthy():
                try:                                         # a new tick arrived: re-check stops / entries (at most about once a second)
                    await asyncio.wait_for(self.feed.updated.wait(), 5)
                except asyncio.TimeoutError:
                    pass
                self.feed.updated.clear()
                await asyncio.sleep(1.0)
            else:
                await asyncio.sleep(20 if market_open() else 300)

    def start(self) -> None:
        for inst in self.instruments.values():
            self.feed.subscribe(inst.exch_seg, inst.token)
        for ex, tok in self.index_tokens.values():   # index prices for the day-change display
            self.feed.subscribe(ex, tok)
        self.feed.start()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        await self.feed.stop()
        if self._task:
            self._task.cancel()

    async def index_quote(self, name: str) -> dict:
        """Index price with today's change in points and % versus the previous trading day's close (real Angel One data)."""
        ex, tok = self.index_tokens[name]
        now = now_ist()
        cached = self._prev_close.get(name)
        if cached is None or (now - cached[0]).total_seconds() > 600:
            df = await self._candles(ex, tok, "ONE_DAY", 10, include_partial=True)
            prev = None if df is None or df.empty else df[df.index.date < now.date()]
            prev_close = float(prev["close"].iloc[-1]) if prev is not None and not prev.empty else None
            last_close = float(df["close"].iloc[-1]) if df is not None and not df.empty else None
            self._prev_close[name] = cached = (now, prev_close, last_close)
        prev_close, last_close = cached[1], cached[2]
        tick = self.feed.latest.get(tok)
        live = self.feed.healthy(tok) and tick is not None
        price = tick.ltp if live else last_close
        change = None if price is None or prev_close is None else round(price - prev_close, 2)
        return {"symbol": name, "price": price, "prev_close": prev_close, "change": change,
                "pct": None if change is None or not prev_close else round(change / prev_close * 100, 2),
                "live": live, "age_s": self.feed.age(tok),
                "source": (getattr(self.feed, "source", "") + " live price" if getattr(self.feed, "source", "") else "WebSocket tick") if live else "REST daily candle"}

    # ---- views -----------------------------------------------------------
    def overview(self, owner=ALL) -> dict:
        items = []
        for inst in self.instruments.values():
            if owner is not ALL and inst.owner != owner:
                continue
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
            "feed": self.feed.status(),
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
