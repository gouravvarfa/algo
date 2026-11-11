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
import time
from collections import deque
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
from app.paper_trading import dryrun
from app.paper_trading.shadow import Shadow
from app.live.ticks import TickFeed, merge_minutes
from app.providers import angelone_scrip_master as scrip_master
from app.providers.angelone_provider import AngelOneProvider
from app.strategy.top_bottom.gap_protocol import NSE, SESSIONS, Bar15, Session
from app.providers.commodities import COMMODITIES
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
INDEX_CLOSE_PATH = Path("data/paper_trading/index_close.json")   # the market bar's last closes: shown at once after a restart
SNAPSHOT_DIR = Path("data/paper_trading/snapshots")              # the last good screen of each instrument: a restart shows it at once instead of nothing
SNAPSHOT_SAVE_SECONDS = 60
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
CANDLE_CALL_GAP = 1.0                 # seconds between any two Angel One candle requests
CANDLE_BACKOFF_FIRST = 5               # after a 403 / rate-limit refusal, ask Angel One nothing for this long (seconds) ...
CANDLE_BACKOFF_MAX = 60                # ... doubling on each further refusal up to this; the stored candles are used meanwhile
LIVE_MINUTES_REFRESH_WITH_FEED = 120   # the WebSocket supplies the recent minutes, so REST is only needed for history
DAILY_RAW_CACHE_WITH_FEED = 120
INDEX_WARM_GAP_S = 20.0               # the market bar's 5-minute index charts are loaded ahead of time, one index at most this often (see _warm_index_charts)
INDEX_CLOSE_RETRY_S = 300.0           # a market-bar / sidebar index's closes are asked from Angel One at most this often (see _index_close_due)
CANDLE_SHARE_SECONDS = 15              # identical candle requests made by different users within this time share one Angel One call
ALL = object()                        # "every owner" (internal callers and tests)
LEGACY_OWNER_PATH = Path("data/paper_trading/legacy_owner.txt")   # the account that adopted the pre-platform watchlist and journal
STOCK_BREAKEVEN_PCT = 0.03          # stocks arm the breakeven stop at +3% profit; the indexes (BankNifty / Nifty) keep +2%


def breakeven_pct_for(symbol: str) -> float | None:
    """None = the strategy's own index rule (2%): BankNifty, Nifty and the MCX gold / silver futures. Every other symbol is a stock future: 3%."""
    return None if symbol in INDEX_TOKENS or symbol in COMMODITIES else STOCK_BREAKEVEN_PCT


STOCK_STRUCT_CAP = 0.03             # stocks: a structural stop 3%+ away from the entry keeps the entry-day 1% stop (the indexes use 2%)


def struct_cap_for(symbol: str) -> float:
    return STRUCT_CAP_DISTANCE if symbol in INDEX_TOKENS or symbol in COMMODITIES else STOCK_STRUCT_CAP


INDEX_TOKENS = {"BANKNIFTY": ("NSE", "99926009"), "NIFTY": ("NSE", "99926000")}
# The scrolling market bar (display only: never traded, never part of a strategy). Tokens from Angel One's scrip master, checked 10 Nov 2026.
TICKER_INDICES: dict[str, tuple[str, str, str]] = {
    "NIFTY": ("NSE", "99926000", "NIFTY 50"), "BANKNIFTY": ("NSE", "99926009", "BANKNIFTY"), "SENSEX": ("BSE", "99919000", "SENSEX"),
    "FINNIFTY": ("NSE", "99926037", "FINNIFTY"), "MIDCPNIFTY": ("NSE", "99926074", "MIDCAP SELECT"), "INDIA VIX": ("NSE", "99926017", "INDIA VIX"),
    "NIFTYNXT50": ("NSE", "99926013", "NIFTY NEXT 50"), "NIFTY MIDCAP 100": ("NSE", "99926011", "MIDCAP 100"),
    "NIFTY SMLCAP 100": ("NSE", "99926032", "SMALLCAP 100"), "NIFTY IT": ("NSE", "99926008", "NIFTY IT"), "NIFTY AUTO": ("NSE", "99926029", "NIFTY AUTO"),
    "NIFTY FMCG": ("NSE", "99926021", "NIFTY FMCG"), "NIFTY PHARMA": ("NSE", "99926023", "NIFTY PHARMA"), "NIFTY METAL": ("NSE", "99926030", "NIFTY METAL"),
    "NIFTY ENERGY": ("NSE", "99926020", "NIFTY ENERGY"), "NIFTY REALTY": ("NSE", "99926018", "NIFTY REALTY"), "NIFTY MEDIA": ("NSE", "99926031", "NIFTY MEDIA"),
    "NIFTY PSU BANK": ("NSE", "99926025", "PSU BANK"), "NIFTY INFRA": ("NSE", "99926019", "NIFTY INFRA"),
}
INDEX_CHART_TF = {"1D": ("ONE_DAY", 400, 900), "15m": ("FIFTEEN_MINUTE", 20, 120), "5m": ("FIVE_MINUTE", 5, 60)}   # interval, days, cache seconds
SWEEP_MIN_SLEEP = 0.25                  # between two sweeps of the engine while ticks flow (seconds)
FAULT_RETRY_SECONDS = 5.0                # an instrument whose evaluation is failing is looked at again after this long, not on every sweep
SWEEP_DUTY = 2.0                        # ... and at least this many times the CPU the sweep used, so the engine never takes more than about a third of a core
                                        # (CPU, not wall time: a sweep that mostly WAITED for Angel One is not followed by a long pause)
LIVE_RATE_LIMIT_COOLDOWN_SECONDS = 90   # after an Angel One 'Too many requests', do not ask for 1-minute data again for this long
SINGLE_MINUTES_FROM = dt.date(2026, 10, 30)   # first day of the live 1-minute rules of an instrument not (yet) in a contract chain: fixed, see _single_minutes_from
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
    session: str = "NSE"  # the trading day the strategy's first-candle / gap rules use: NSE, or MCX for gold and silver
    point_value: float | None = None  # rupees per lot for a price move of 1.00 when the lot size alone does not say (MCX gold / silver); None = quantity x points

    @property
    def sess(self) -> Session:
        return SESSIONS.get(self.session, NSE)

    @property
    def pnl_units(self) -> int:
        """What a price point is multiplied with to get rupees: the units traded, or lots x point value for the MCX contracts (quoted in another unit than the lot)."""
        return int(round((self.lots or 1) * self.point_value)) if self.point_value else self.quantity


class PaperError(ValueError):
    pass


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST).replace(tzinfo=None)


def _expired(expiry, now: dt.datetime) -> bool:
    """The contract's expiry day is over (an empty or unreadable expiry, e.g. an equity, never is)."""
    try:
        return dt.date.fromisoformat(str(expiry)[:10]) < now.date()
    except ValueError:
        return False


def _settled(expiry, last_ts, now: dt.datetime) -> bool:
    """An expired contract whose candles we already hold up to its expiry day: Angel One no longer serves it and its candles can no longer
    change, so asking again only spends the candle quota. (On 9 Nov 2026 the two October contracts were re-asked every 15 seconds, because no
    tick ever arrives for an expired contract, while Angel One was refusing about half of all candle requests.)"""
    if last_ts is None or not _expired(expiry, now):
        return False
    try:
        return pd.Timestamp(last_ts).date() >= dt.date.fromisoformat(str(expiry)[:10])
    except (TypeError, ValueError):
        return False


def _last_ts(df) -> object | None:
    return df.index[-1] if df is not None and len(df) else None


def _last_session_before(day: dt.date, exchange: str = "NSE") -> dt.date:
    """The last weekday before `day` that is not in the market calendar as a holiday (or a special session)."""
    from app.ops import market_calendar
    d = day - dt.timedelta(days=1)
    for _ in range(14):
        if d.weekday() < 5 and market_calendar.entry(d, exchange) is None:
            return d
        d -= dt.timedelta(days=1)
    return d


def market_open(now: dt.datetime | None = None, session: Session = NSE) -> bool:
    now = now or now_ist()
    from app.ops import market_calendar
    if market_calendar.entry(now.date(), session.name) is not None:
        return False                  # an exchange holiday, or a special session (Muhurat etc.) in which the strategy and real orders stay off
    return now.weekday() < 5 and session.first_start <= now.time() <= session.close


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


INDEX_CHART_BUCKET = {"FIVE_MINUTE": 5, "FIFTEEN_MINUTE": 15}


def _naive(df: pd.DataFrame | None) -> pd.DataFrame | None:
    """Candles indexed by IST wall time without a zone (Angel One's answer carries +05:30, the stored copy does not)."""
    if df is None or not len(df) or getattr(df.index, "tz", None) is None:
        return df
    out = df.copy()
    out.index = out.index.tz_localize(None)
    return out


def _tick_buckets(minutes: list, size: int, now: dt.datetime, start: dt.time = dt.time(9, 15)) -> pd.DataFrame:
    """Today's `size`-minute candles built from the 1-minute bars of live ticks, on Angel One's grid (09:15, 09:20, ...). No API call."""
    base = dt.datetime.combine(now.date(), start)
    rows: dict = {}
    for m, o, h, l, c in minutes:
        t = m.replace(tzinfo=None) if m.tzinfo else m
        if t.date() != now.date() or t < base:
            continue
        k = base + dt.timedelta(minutes=(int((t - base).total_seconds()) // 60) // size * size)
        r = rows.get(k)
        rows[k] = [o, h, l, c] if r is None else [r[0], max(r[1], h), min(r[2], l), c]
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    keys = sorted(rows)
    return pd.DataFrame([rows[k] + [0.0] for k in keys], index=pd.DatetimeIndex(keys), columns=["open", "high", "low", "close", "volume"])


def _with_tick_buckets(df: pd.DataFrame | None, ticks: pd.DataFrame) -> pd.DataFrame | None:
    """Angel One's candles are kept as they are; candles built from ticks only stretch the last one (the forming candle) and add the periods
    Angel One has not given yet."""
    if ticks is None or ticks.empty:
        return df
    if df is None or not len(df):
        return ticks
    last = df.index[-1]
    out = df.copy()
    if last in ticks.index:
        r = ticks.loc[last]
        out.loc[last, "high"], out.loc[last, "low"], out.loc[last, "close"] = max(out.loc[last, "high"], r.high), min(out.loc[last, "low"], r.low), r.close
    newer = ticks[ticks.index > last]
    return pd.concat([out, newer]) if len(newer) else out


def _is_current_bar(t, interval: str, now: dt.datetime) -> bool:
    """Is the candle starting at `t` the one of the period now running? (A live price must never be written into an older candle.)"""
    t = t.to_pydatetime().replace(tzinfo=None) if hasattr(t, "to_pydatetime") else t
    if interval == "ONE_DAY":
        return t.date() == now.date()
    size = INDEX_CHART_BUCKET.get(interval, 1)
    return now - dt.timedelta(minutes=size) < t <= now


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
        self.faults: dict[str, dict] = {}        # instrument id -> {message, since}: an instrument whose evaluation keeps failing (isolated; the others go on)
        self.last_tick: dt.datetime | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._bars15: dict[str, tuple[dt.datetime, dict]] = {}  # instrument id -> (fetched at, date -> [Bar15])
        self._minutes: dict[str, tuple[dt.datetime, list]] = {}  # instrument id -> (fetched at, 1-minute candles)
        self.feed = TickFeed(provider.get_session, provider.reset_session)
        self.shadow = Shadow()                           # the simulated "real account" (BankNifty only); it never sends anything to a broker
        self.feed.listeners.append(self.shadow.on_tick)
        self._raw_cache: dict[str, tuple[dt.datetime, pd.DataFrame]] = {}
        self._prev_close: dict[str, tuple] = {}
        self._live_good: dict[str, tuple[dt.datetime, list, str]] = {}  # instrument id -> (evaluated at, trades, note) of the last SUCCESSFUL intraday run
        self._cooldown_until: dict = {}
        self._chain_cache: dict = {}
        self._sweeps: deque = deque(maxlen=300)             # (time, sweep milliseconds): how long one pass over all instruments takes
        self.last_sweep_s = 0.0
        self.last_sweep_cpu_s = 0.0                          # the CPU that pass used (the pause after it is based on this)
        self._load()

    # ---- persistence -----------------------------------------------------
    def _load(self) -> None:
        if STATE_PATH.exists():
            seen: dict[tuple, str] = {}
            dropped = 0
            for raw in json.loads(STATE_PATH.read_text()):
                inst = Instrument.model_validate(raw)
                key = (inst.owner, inst.symbol, inst.instrument_type, inst.timeframe)
                if key in seen:                  # the same strategy added twice for one person (two opens of the page at once): keep the first
                    dropped += 1
                    continue
                seen[key] = inst.id
                self.instruments[inst.id] = inst
                self._restore_snapshot(inst.id)
            if dropped:
                log.warning("removed %d duplicate instrument(s) from the watchlist", dropped)
                self._save()

    def _restore_snapshot(self, instrument_id: str) -> None:
        try:
            snap = json.loads((SNAPSHOT_DIR / f"{instrument_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        snap["restored"] = True                     # shown until the first fresh evaluation replaces it
        self.snapshots[instrument_id] = snap

    def _store_snapshot(self, instrument_id: str) -> None:
        """At most once a minute per instrument. Failing to store must never touch the engine."""
        saved = self.__dict__.setdefault("_snap_saved", {})
        if time.monotonic() - saved.get(instrument_id, -SNAPSHOT_SAVE_SECONDS) < SNAPSHOT_SAVE_SECONDS:
            return
        saved[instrument_id] = time.monotonic()
        try:
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            tmp = SNAPSHOT_DIR / f"{instrument_id}.tmp"
            tmp.write_text(json.dumps(self.snapshots[instrument_id], default=str), encoding="utf-8")
            tmp.replace(SNAPSHOT_DIR / f"{instrument_id}.json")
        except (OSError, TypeError, ValueError):
            log.warning("could not store the snapshot of %s", instrument_id)

    def _save(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps([i.model_dump() for i in self.instruments.values()], indent=2))

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
            contracts = [c for c in await scrip_master.get_future_contracts(symbol) if c.underlying == symbol and c.expiry >= today]
            if not contracts:
                raise PaperError(f"{symbol}: no live futures contract found")
            c = min(contracts, key=lambda x: x.expiry)
            fields = dict(
                quantity=size * c.lot_size, lots=size, lot_size=c.lot_size, token=c.token,
                trading_symbol=c.trading_symbol, exch_seg=c.exch_seg, expiry=c.expiry,
            )
            if c.exch_seg == "MCX":
                fields.update(session="MCX", point_value=COMMODITIES[symbol]["point_value"])
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

        try:
            raw = await self._own_candles(inst, interval, from_dt, days_back, now)
        except Exception as exc:
            if not (minutes is None and inst.instrument_type == "FUTURE"):
                raise
            log.warning("own candles of %s unavailable (%s); going on with the contract chain", inst.symbol, str(exc)[:60])      # Angel One refusing must not stop the chain
            raw = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        # A futures instrument is evaluated across its contract chain (Oct -> Nov -> ...): the candles of its OWN first contract are not needed once that
        # contract has expired and Angel One no longer serves it, so an empty answer for it must not stop the chain.
        chain_capable = minutes is None and inst.instrument_type == "FUTURE"
        if raw.empty and not chain_capable:
            raise PaperError("No candle data returned by Angel One")
        confirmed = raw if raw.empty else (drop_incomplete_trailing_daily_bar(raw, now, inst.sess.close) if minutes is None else drop_incomplete_trailing_bars(raw, minutes, now))
        if len(confirmed) < 5 and not chain_capable:
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
        if chain is None and len(confirmed) < 5:
            raise PaperError("No candle data returned by Angel One" if raw.empty else "Not enough confirmed candles yet")
        active, rolls, roll_marks, prev_line = inst, [], [], None
        # real_ready: may a real account act on this result? Only a proper intraday evaluation of a contract that still trades. The daily-close
        # fallback (1-minute data unavailable) and the result of an EXPIRED own contract (the roll window, before the next contract's first minute)
        # can show a different position than the live rules: a real account must not exit or move its stop on them, and they are not journaled.
        real_ready = True
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
                inst.symbol, inst.trading_symbol, inst.expiry, "STOCK", inst.pnl_units, dates, closes, list(confirmed["open"]),
                intraday, [float(h) for h in confirmed["high"]], [float(x) for x in confirmed["low"]], breakeven_pct_for(inst.symbol), session=inst.sess,
            )
            engine_note, engine_warn = "Daily-close rules only", False
            if minutes is None:
                live = await self._live_intraday(inst, confirmed, trades, now)
                if live is not None:
                    trades, engine_note = live
                    engine_warn = engine_note.startswith("WARNING")
                else:
                    engine_note, engine_warn = "WARNING: intraday (1-minute) data unavailable right now, showing the daily-close result. A stop touched today may not be reflected until the next successful update", True
                    real_ready = False
                if inst.instrument_type == "FUTURE" and _expired(getattr(inst, "expiry", ""), now):
                    real_ready = False              # the chain could not run yet (roll day, before the next contract's first minute): an expired contract's result
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
                "pnl_amount": round(pts * inst.pnl_units, 2),
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
            "real_ready": real_ready,
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

    def _refresh_later(self, key, load) -> None:
        """Fetch newer candles in the background: the sweep keeps going on what it has, so a slow or refusing Angel One never holds up stops and entries.
        Only one refresh per key runs at a time. The very first load of a contract is still awaited: a strategy must not be evaluated without its history."""
        tasks = self.__dict__.setdefault("_bg_tasks", {})
        running = tasks.get(key)
        if running is not None and not running.done():
            return

        async def run():
            try:
                await load()
            except Exception as exc:
                log.warning("background candle refresh failed for %s: %s", key, str(exc)[:80])
        tasks[key] = asyncio.create_task(run())

    async def _candles(self, *args, **kwargs):
        """Every Angel One candle request goes through here: at most one at a time and at least CANDLE_CALL_GAP seconds apart,
        so the startup burst (daily + 15m + 1m + index) cannot trip the rate limit.
        Requests for the same contract and interval share one call: a recent fetch covering MORE days serves a shorter request (cut to
        that request's own window), so the BankNifty strategies of several users cost one call instead of one each.
        After a 403 / rate-limit refusal nothing is asked for a short, growing pause; callers fall back to the stored candles."""
        import time as _t
        if not hasattr(self, "_rest_lock"):
            self._rest_lock, self._rest_last = asyncio.Lock(), 0.0
            self._backoff_until, self._backoff = 0.0, 0.0
        exch_seg, token, interval = args[:3]
        days = args[3] if len(args) > 3 else kwargs.get("days_back", 5)
        key = (exch_seg, token, interval, bool(kwargs.get("include_partial", False)))
        if not hasattr(self, "_shared_candles"):
            self._shared_candles = {}
        async with self._rest_lock:
            hit = self._shared_candles.get(key)
            if hit and _t.monotonic() - hit[0] < CANDLE_SHARE_SECONDS and hit[1] >= days:
                df = hit[2]
                if hit[1] > days and len(df):             # a longer fetch: keep only what this request would have got
                    cut = pd.Timestamp(now_ist() - dt.timedelta(days=days))
                    cut = cut.tz_convert(df.index.tz) if df.index.tz is not None else cut.tz_localize(None)
                    df = df[df.index >= cut]
                return df.copy()
            if _t.monotonic() < self._backoff_until:
                raise RuntimeError(f"Angel One refused recently (HTTP 403 / rate limit); pausing requests for {self._backoff_until - _t.monotonic():.0f} s")
            wait = CANDLE_CALL_GAP - (_t.monotonic() - self._rest_last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                out = await self.provider.get_intraday_ohlc(*args, **kwargs)
            except Exception as exc:
                msg = str(exc).lower()
                if "403" in msg or "too many" in msg or "rate" in msg:
                    self._backoff = min(CANDLE_BACKOFF_MAX, self._backoff * 2 or CANDLE_BACKOFF_FIRST)
                    self._backoff_until = _t.monotonic() + self._backoff
                    log.warning("Angel One refused a candle request (%s); pausing candle requests for %.0f s", str(exc)[:60], self._backoff)
                raise
            finally:
                self._rest_last = _t.monotonic()
            self._backoff = 0.0
            self._shared_candles[key] = (_t.monotonic(), days, out.copy())
            if len(self._shared_candles) > 200:
                for k in sorted(self._shared_candles, key=lambda k: self._shared_candles[k][0])[:100]:
                    self._shared_candles.pop(k, None)
            return out

    # ---- multi-contract chain (Oct -> Nov -> Dec ...) --------------------------
    async def _known_contracts(self, inst: Instrument) -> list[ContractRef]:
        """Live contracts from Angel One's master plus the ones remembered from earlier runs (an expired contract is no longer in the master)."""
        try:
            known = json.loads(CONTRACTS_PATH.read_text()) if CONTRACTS_PATH.exists() else {}
        except ValueError:
            known = {}
        mine = {c["trading_symbol"]: c for c in known.get(inst.symbol, [])}
        try:
            for c in await scrip_master.get_future_contracts(inst.symbol):
                if c.underlying == inst.symbol:
                    mine[c.trading_symbol] = {"trading_symbol": c.trading_symbol, "token": c.token, "exch_seg": c.exch_seg, "expiry": c.expiry, "lot_size": c.lot_size, "tick_size": c.tick_size}
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
        if hit:
            if (now - hit[0]).total_seconds() >= DAILY_RAW_CACHE_WITH_FEED and not _settled(c.expiry, _last_ts(hit[1]), now):
                self._refresh_later(key, lambda: self._load_daily(c, from_dt, now_ist()))
            return hit[1]
        return await self._load_daily(c, from_dt, now)

    async def _load_daily(self, c: ContractRef, from_dt: dt.datetime, now: dt.datetime) -> pd.DataFrame:
        key = ("d", c.token)
        stored = candle_store.get(c.token, "ONE_DAY", from_dt)
        if _settled(c.expiry, _last_ts(stored), now):
            fresh = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])     # final: nothing to ask Angel One
        else:
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
        if hit:
            base = hit[1]
            if (now - hit[0]).total_seconds() >= refresh and not _settled(c.expiry, base[-1].ts if base else None, now):   # the ticks cover the newest minutes meanwhile
                self._refresh_later(key, lambda: self._load_minutes(c, first_day, now_ist()))
        else:
            base = await self._load_minutes(c, first_day, now)
        out = trim_partial_first_day(merge_minutes(base, self.feed.builder.minutes(c.token)), lenient)
        return [m for m in out if m.ts.date() >= pin] if pin else out

    async def _load_minutes(self, c: ContractRef, first_day: dt.date, now: dt.datetime) -> list:
        since = dt.datetime.combine(first_day, dt.time(9, 0))
        stored = candle_store.get(c.token, "ONE_MINUTE", since)
        days = max(LIVE_MINUTES_DAYS, min(TIMEFRAMES["1m"][2] - 1, (now.date() - first_day).days + 3))
        if _settled(c.expiry, _last_ts(stored), now):
            fresh = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])     # final: nothing to ask Angel One
        else:
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
        self._chain_cache[("m", c.token)] = (now, base)
        return base

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
            return drop_incomplete_trailing_daily_bar(df, now, inst.sess.close) if len(df) else df

        async def get_minutes(c):
            if c.token == contracts[0].token:
                # the index chain has tracked 1-minute data from a fixed day; a stock future starts on the day it was assigned
                if inst.symbol in INDEX_TOKENS:
                    start = FIRST_CONTRACT_MINUTES_FROM
                else:   # a stock: the first day its single-contract rules used (so the first roll does not change its history), or a week before the
                        # first contract's cutoff if that is earlier, so the roll can be priced
                    start = min(self._single_minutes_from(inst), contract_cutoff(contracts[0].expiry) - dt.timedelta(days=6))
                return await self._contract_minutes(c, start, now, start, lenient=inst.symbol not in INDEX_TOKENS)
            return await self._contract_minutes(c, contract_cutoff(contracts[0].expiry), now, lenient=inst.symbol not in INDEX_TOKENS)

        def daily_engine(c, df):
            _sig, trs = run_top_bottom_backtest(inst.symbol, c.trading_symbol, c.expiry.isoformat(), "STOCK", inst.pnl_units,
                                                [t.to_pydatetime() for t in df.index], [float(x) for x in df["close"]], list(df["open"]),
                                                gap_bars if c.token == inst.token else None, [float(h) for h in df["high"]], [float(x) for x in df["low"]],
                                                breakeven_pct_for(inst.symbol), session=inst.sess)
            return trs

        return await chain_eval.evaluate(
            contracts=contracts, from_dt=from_dt, now=now, symbol=inst.symbol, get_daily=get_daily, get_minutes=get_minutes,
            daily_engine=daily_engine, reconstruct=lambda rows, fd: reconstruct_state(rows, fd, breakeven_pct_for(inst.symbol)),
            entry_day_stop_pct=ENTRY_DAY_STOP_PCT, struct_cap_distance=struct_cap_for(inst.symbol), breakeven_pct=breakeven_pct_for(inst.symbol), session=inst.sess)

    @staticmethod
    def _single_minutes_from(inst) -> dt.date:
        """The FIXED first day of an instrument's live 1-minute rules while it is not in a contract chain (and of its first contract once it is, so
        the history does not jump at the first roll): SINGLE_MINUTES_FROM, or LIVE_MINUTES_DAYS before the day it was added if that is later
        (the window it had when it was added). It never moves afterwards."""
        try:
            added = dt.date.fromisoformat(str(getattr(inst, "added_at", ""))[:10])
        except ValueError:
            return SINGLE_MINUTES_FROM
        return max(SINGLE_MINUTES_FROM, added - dt.timedelta(days=LIVE_MINUTES_DAYS))

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

        try:
            # The same 1-minute source as the contract chain: a FIXED first day, the stored candles (data/paper_trading/candles.db), refreshed from
            # Angel One in the background, the newest minutes from the live ticks. Before, the window was 'the last LIVE_MINUTES_DAYS days': its first
            # day moved every night and was rebuilt from the daily closes, so the history after it, even the open position, could change overnight
            # (GOLDTEN, 10 Nov 2026: the open BUY moved from 10:37 @ 144,426 to 09:00 @ 144,425 at midnight); and a refused candle request (about
            # half of them in NSE hours) froze the result for up to 20 minutes, then fell back to the daily-close result.
            start = self._single_minutes_from(inst)
            ref = ContractRef(getattr(inst, "trading_symbol", "") or inst.symbol, inst.token, inst.exch_seg, getattr(inst, "expiry", "") or "", getattr(inst, "lot_size", None) or 0)
            mins = await self._contract_minutes(ref, start, now, start, lenient=inst.symbol not in INDEX_TOKENS)
            if len(mins) < 30:
                return None
            from app.strategy.top_bottom.warmup import reconstruct_state
            rows = [(t.to_pydatetime(), float(r.open), float(r.high), float(r.low), float(r.close)) for t, r in confirmed.iterrows()]
            if len(rows) < 5:
                return None
            first_day = mins[0].ts.date()      # run_intraday never uses a day's own daily candle on that day, so no filtering is needed
            initial, _warn = reconstruct_state(rows, first_day, breakeven_pct_for(inst.symbol))
            res = run_intraday(inst.symbol, [r[0] for r in rows], [r[4] for r in rows], mins, initial, entry_day_stop_pct=ENTRY_DAY_STOP_PCT, struct_cap_distance=struct_cap_for(inst.symbol),
                               breakeven_pct=breakeven_pct_for(inst.symbol), session=inst.sess)
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

    async def _own_candles(self, inst: Instrument, interval: str, from_dt: dt.datetime, days_back: int, now: dt.datetime) -> pd.DataFrame:
        """The candles of the instrument's own contract since `from_dt`: Angel One, else the stored copy. The result MAY BE EMPTY (a contract that has expired
        is no longer served); the caller decides whether that matters. An Angel One error with nothing stored is raised as it is."""
        rc = self._raw_cache.get(inst.id)
        ttl = DAILY_RAW_CACHE_WITH_FEED if self.feed.healthy(inst.token) else 15
        if rc:
            if (now - rc[0]).total_seconds() >= ttl and not _settled(getattr(inst, "expiry", ""), _last_ts(rc[1]), now):
                self._refresh_later(("o", inst.id), lambda: self._load_own(inst, interval, from_dt, days_back, now_ist()))
            return rc[1][rc[1].index >= from_dt]
        return await self._load_own(inst, interval, from_dt, days_back, now)

    async def _load_own(self, inst: Instrument, interval: str, from_dt: dt.datetime, days_back: int, now: dt.datetime) -> pd.DataFrame:
        raw, err = None, None
        if from_dt is not None and _expired(getattr(inst, "expiry", ""), now):
            stored = candle_store.get(inst.token, interval, from_dt)
            if _settled(inst.expiry, _last_ts(stored), now):         # an expired contract we hold to its last day: final, nothing to ask Angel One
                self._raw_cache[inst.id] = (now, stored)
                return stored[stored.index >= from_dt]
        try:
            raw = await self._candles(inst.exch_seg, inst.token, interval, days_back, include_partial=True)
        except Exception as exc:                  # Angel One refused (403 / rate limit)
            err = exc
        if raw is None or raw.empty:
            stored = candle_store.get(inst.token, interval, from_dt) if from_dt else None
            if stored is not None and not stored.empty:
                log.warning("%s candles for %s unavailable (%s); using the stored copy", interval, inst.trading_symbol, str(err)[:60] if err else "empty answer")
                raw = stored
            elif err is not None:
                raise err
            elif raw is None:
                raw = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        self._raw_cache[inst.id] = (now, raw)
        return raw[raw.index >= from_dt]

    async def _gap_bars(self, inst: Instrument, from_dt: dt.datetime, now: dt.datetime) -> dict | None:
        """Completed 15m candles grouped by day, cached for a few minutes. A
        failure here never blocks the instrument: the close rules still run."""
        cached = self._bars15.get(inst.id)
        if cached:
            fresh_due = self._new_15m_candle_closed(cached[0], now, inst.sess) or (now - cached[0]).total_seconds() >= GAP_15M_REFRESH_SECONDS
            if fresh_due and not _expired(getattr(inst, "expiry", ""), now):     # an expired contract's 15m bars can no longer change (nor are they served)
                self._refresh_later(("g", inst.id), lambda: self._load_gap_bars(inst, from_dt, now_ist()))
            return cached[1]
        return await self._load_gap_bars(inst, from_dt, now)

    async def _load_gap_bars(self, inst: Instrument, from_dt: dt.datetime, now: dt.datetime) -> dict | None:
        cached = self._bars15.get(inst.id)
        try:
            days = min((now - from_dt).days + 2, TIMEFRAMES["15m"][2])
            df = await self._candles(inst.exch_seg, inst.token, "FIFTEEN_MINUTE", days)
        except Exception:
            log.warning("15m fetch for gap protocol failed for %s", inst.symbol)
            kept = cached[1] if cached else None
            self._bars15[inst.id] = (now, kept)       # remembered, so the next sweeps do not wait for Angel One again; the background refresh retries
            return kept
        out: dict = {}
        for ts, r in df.iterrows():
            start = ts.to_pydatetime()
            out.setdefault(start.date(), []).append(Bar15(start, float(r.open), float(r.high), float(r.low), float(r.close)))
        self._bars15[inst.id] = (now, out)
        return out

    @staticmethod
    def _new_15m_candle_closed(fetched_at: dt.datetime, now: dt.datetime, session: Session = NSE) -> bool:
        """True once a 15-minute candle has completed after the last fetch (+20 s for Angel One to publish it), so the
        gap protocol sees the 09:15-09:30 candle (and every later one) right after it closes, not up to 10 minutes late."""
        if not market_open(now, session):
            return False
        boundary = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
        return fetched_at < boundary + dt.timedelta(seconds=20) <= now

    # ---- live journal ------------------------------------------------------
    def _journal(self, inst: Instrument, snap: dict) -> None:
        """Append ENTRY / EXIT / SL->BREAKEVEN / GAP events for LIVE trades the first time they are seen.
        Append-only: a later recomputation never rewrites what was recorded.
        A result a real account may not act on (real_ready False: the daily-close fallback, an expired contract's result) is not journaled: its
        trades are not the live rules' trades, and every one of them would look new."""
        if not snap.get("real_ready", True):
            return
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
                    "direction": r["direction"], "entry_date": r["entry_date"], "entry_price": r["entry_price"], "quantity": inst.quantity,
                    "contract": r.get("contract")}      # the trade's OWN contract (a roll exit closes the old one)
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
        self._shadow_call(dryrun.record, events, snap)                  # the orders a real account would send (written only, never sent)
        self._shadow_step("handle", events, snap, now_ist())            # the simulated account follows the signals with its own fills
        self._real_step("handle", events, snap, now_ist())              # accounts that switched real orders on for this instrument

    @staticmethod
    def _shadow_call(fn, *args) -> None:
        """The dry run and the shadow account are observers: whatever goes wrong in them must never touch the paper engine."""
        try:
            fn(*args)
        except Exception:
            log.exception("dry-run / shadow step failed")

    def _shadow_step(self, name: str, *args) -> None:
        sh = getattr(self, "shadow", None)
        if sh is not None:
            self._shadow_call(getattr(sh, name), *args)

    def _real_step(self, name: str, *args) -> None:
        """Tell the real-order executor (app/broker/tb_real.py) what the strategy just did. It only queues the news; nothing is sent from here, and a failure here never touches the paper engine."""
        rt = getattr(self, "real", None)
        if rt is not None:
            self._shadow_call(getattr(rt, name), *args)

    def shadow_view(self, owner=ALL) -> dict:
        exits = [{"instrument_id": e.get("instrument_id"), "at": e.get("at", ""), "pnl": e.get("pnl_amount") or 0.0}
                 for e in self.journal(5000, owner=owner) if e.get("event") == "EXIT" and not e.get("backfill")]
        return self.shadow.view(None if owner is ALL else owner, exits, everyone=owner is ALL)

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
        if paused and inst.id in self.snapshots and not self.snapshots[inst.id].get("restored"):    # kill switch / strategy OFF: keep the last stored snapshot, evaluate nothing
            return
        try:
            # first look after a restart while paused: build ONE read-only snapshot so the page has something to show (nothing is journaled)
            self.snapshots[inst.id] = await self._build_snapshot(inst)
            self._store_snapshot(inst.id)
            if paused:
                self.snapshots[inst.id]["paused"] = True
                self.errors.pop(inst.id, None)
                return
            self.errors.pop(inst.id, None)
            if self.faults.pop(inst.id, None) is not None:
                ops_incidents.record("engine", f"{inst.symbol} recovered", severity="info", user_id=owner, auto_action="fault cleared")
            self._journal(inst, self.snapshots[inst.id])
            self._shadow_step("sync", inst.id, owner, inst.symbol, self.snapshots[inst.id], now_ist())
            self._real_step("sync", inst.id, owner, inst.symbol, self.snapshots[inst.id], now_ist())
        except Exception as exc:  # one bad instrument (or user) must never stop the others: isolate it, record it, go on
            log.exception("refresh failed for %s", inst.symbol)
            msg = str(exc) or exc.__class__.__name__
            self.errors[inst.id] = msg
            if inst.id not in self.faults:
                self.faults[inst.id] = {"message": f"{type(exc).__name__}: {msg[:200]}", "since": now_ist().isoformat(timespec="seconds")}
                ops_incidents.record("engine", f"{inst.symbol} evaluation failed: {type(exc).__name__}: {msg[:200]}", user_id=owner,
                                     auto_action="instrument isolated; other instruments keep running")

    def _ordered(self) -> list[Instrument]:
        """The instruments in the order a sweep visits them: those holding an open position first (their stop is what matters most), the rest as before."""
        insts = list(self.instruments.values())
        return sorted(insts, key=lambda i: 0 if (self.snapshots.get(i.id) or {}).get("position") else 1)

    @staticmethod
    def sweep_sleep(sweep_seconds: float) -> float:
        """Pause after a sweep: short, but never less than SWEEP_DUTY x the sweep itself (a slow server is not made slower by a busy loop)."""
        return max(SWEEP_MIN_SLEEP, SWEEP_DUTY * sweep_seconds)

    def sweep_stats(self) -> dict:
        ms = sorted(x[1] for x in self._sweeps)
        if not ms:
            return {"sweeps": 0}
        span = self._sweeps[-1][0] - self._sweeps[0][0]
        return {"sweeps": len(ms), "last_ms": round(self._sweeps[-1][1], 1), "avg_ms": round(sum(ms) / len(ms), 1), "p95_ms": round(ms[min(len(ms) - 1, int(len(ms) * 0.95))], 1),
                "cycle_s": round(span / (len(ms) - 1), 2) if len(ms) > 1 and span > 0 else None}

    def pause_after_sweep(self) -> float:
        """The pause after a sweep, from the CPU it used: waiting on Angel One (a refused candle request, its retries) costs no CPU, so it no
        longer turns into a long pause before the next look at stops and entries (a 30 s wait used to be followed by 60 s of sleep)."""
        return self.sweep_sleep(getattr(self, "last_sweep_cpu_s", self.last_sweep_s))

    async def refresh_all(self) -> None:
        async with self._lock:
            t0, c0 = time.perf_counter(), time.process_time()
            tried = self.__dict__.setdefault("_fault_tried", {})
            for inst in self._ordered():
                if inst.id in self.faults and time.monotonic() - tried.get(inst.id, 0.0) < FAULT_RETRY_SECONDS:
                    continue                                   # failing (Angel One refusing, say): no hammering and no log flood on a fast loop
                t1 = time.perf_counter()
                await self.refresh_one(inst)
                if time.perf_counter() - t1 > 5:
                    log.warning("slow evaluation: %s %s took %.1f s", inst.symbol, inst.id, time.perf_counter() - t1)
                if inst.id in self.faults:
                    tried[inst.id] = time.monotonic()
                await asyncio.sleep(0)       # let the web server answer between instruments; Angel One's candle limit is kept by _candles itself (gap, share, back-off)
            if not ops_flags.engine_paused():
                self.last_tick = now_ist()
            self._shadow_step("poll", now_ist())                # timers of the simulated account run even while the engine is paused (a resting stop order does not stop)
            self.last_sweep_s = time.perf_counter() - t0
            self.last_sweep_cpu_s = time.process_time() - c0
            self._sweeps.append((time.time(), self.last_sweep_s * 1000))

    def _any_market_open(self) -> bool:
        """NSE hours, or the MCX hours while a gold / silver strategy is assigned (they trade until late evening)."""
        return market_open() or any(i.session != "NSE" and market_open(session=i.sess) for i in self.instruments.values())

    async def _loop(self) -> None:
        while True:
            try:
                await self.refresh_all()
            except Exception:
                log.exception("tick failed")
            open_now = self._any_market_open()
            if open_now and self.feed.healthy():
                try:                                         # a new tick arrived: re-check stops / entries (at most about once a second)
                    await asyncio.wait_for(self.feed.updated.wait(), 5)
                except asyncio.TimeoutError:
                    pass
                self.feed.updated.clear()
                await asyncio.sleep(self.pause_after_sweep())
            else:
                await asyncio.sleep(20 if open_now else 300)

    def start(self) -> None:
        for inst in self.instruments.values():
            self.feed.subscribe(inst.exch_seg, inst.token)
        for ex, tok in INDEX_TOKENS.values():        # index prices for the day-change display
            self.feed.subscribe(ex, tok)
        for key, (ex, tok, _label) in TICKER_INDICES.items():     # the market bar: shown only, their ticks never wake the strategy loop
            if key not in INDEX_TOKENS:
                self.feed.display_only.add(tok)
            self.feed.subscribe(ex, tok)
        self.feed.start()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        await self.feed.stop()
        if self._task:
            self._task.cancel()

    async def index_quote(self, name: str) -> dict:
        """Index price and change (real Angel One data), by the same rule as the market bar (_index_view)."""
        ex, tok = INDEX_TOKENS[name]
        now = now_ist()
        self._closes_from_disk()
        cached = self._prev_close.get(name)
        tick = self.feed.latest.get(tok)
        live = self.feed.healthy(tok) and tick is not None
        if self._index_close_due(name, cached, now):
            if cached is None and not live:
                cached = await self._load_index_close(name)        # nothing to show yet: this one answer has to wait
            else:
                self._refresh_later(("i", name), lambda: self._load_index_close(name))    # a live price never waits for the candle queue
        price, ref, session = self._index_view(name, tok, now)
        change = None if price is None or ref is None else round(price - ref, 2)
        today = session == now.date()
        ref_today = next((c for d, c in reversed(cached[1]) if d < now.date()), None) if cached else None     # the reference for today's ticks
        return {"symbol": name, "price": price, "prev_close": ref, "ref_today": ref_today, "change": change, "pct": None if change is None or not ref else round(change / ref * 100, 2),
                "session": session.isoformat() if session else None, "today": today, "live": bool(live and today), "age_s": self.feed.age(tok),
                "source": "WebSocket tick" if live and today else "REST daily candle"}

    def _index_close_due(self, key: str, cached, now: dt.datetime) -> bool:
        """Ask Angel One for an index's daily closes now? Only when the reference is not complete: the closes must hold the last session before today
        AND have been loaded after that session ended (a load during a session holds its unfinished candle). At most once per INDEX_CLOSE_RETRY_S
        per index. These closes are only the reference for the change (the price itself comes from live ticks), and they share the candle queue and
        Angel One's rate limit with the strategy: after a restart in market hours the 19 market-bar indices were asked again on every poll and the
        strategy's own candles waited about 5 minutes (11 Nov 2026)."""
        tried = self.__dict__.setdefault("_index_tried", {})
        last_try = tried.get(key)
        if last_try is not None and (now - last_try).total_seconds() < INDEX_CLOSE_RETRY_S:
            return False
        if cached is not None and cached[1]:
            prev = _last_session_before(now.date())
            if cached[1][-1][0] >= prev and cached[0] >= dt.datetime.combine(prev, NSE.close) + dt.timedelta(minutes=5):
                return False
        tried[key] = now
        return True

    def _index_view(self, key: str, tok: str, now: dt.datetime) -> tuple:
        """(price, reference close, session date). Traded today: the live tick against the last close BEFORE today. No trade today (a holiday, a
        weekend, before the open): the last session's close against the session before it, as brokers show it (10 Nov 2026, an NSE holiday, showed
        +0.00% everywhere before)."""
        cached = self._prev_close.get(key)
        closes = cached[1] if cached else []
        tick = self.feed.latest.get(tok)
        if tick is not None and tick.exch_ts.date() == now.date():
            price, session = tick.ltp, now.date()
        elif closes:
            session, price = closes[-1]
        else:
            return None, None, None
        ref = next((c for d, c in reversed(closes) if d < session), None)
        return price, ref, session

    def ticker(self) -> list[dict]:
        """The market bar: every TICKER_INDICES index with its price and change versus the previous close. It never waits: the live tick, else the last
        daily close. The previous close is loaded ONCE a day per index, in the background (it shares Angel One's candle quota with the strategy)."""
        now = now_ist()
        self._closes_from_disk()
        self._warm_index_charts(now)
        out = []
        for key, (ex, tok, label) in TICKER_INDICES.items():
            cached = self._prev_close.get(key)
            if self._index_close_due(key, cached, now):
                self._refresh_later(("i", key), lambda k=key: self._load_index_close(k))
            tick = self.feed.latest.get(tok)
            price, ref, session = self._index_view(key, tok, now)
            change = None if price is None or ref is None else round(price - ref, 2)
            closes = [m[4] for m in self.feed.builder.minutes(tok) if m[0].date() == now.date()]     # today's minutes built from live ticks: no API call
            step = max(1, -(-len(closes) // 48))                                                       # at most 48 points + the last
            spark = (closes[::step] + ([closes[-1]] if (len(closes) - 1) % step else [])) if closes else []
            ref_today = next((c for d, c in reversed(cached[1]) if d < now.date()), None) if cached else None
            out.append({"key": key, "label": label, "exchange": ex, "token": tok, "ref_today": ref_today, "price": price, "prev_close": ref, "change": change,
                        "pct": None if change is None or not ref else round(change / ref * 100, 2), "session": session.isoformat() if session else None,
                        "today": session == now.date(), "live": bool(tick is not None and self.feed.healthy(tok) and session == now.date()), "spark": spark})
        return out

    def _warm_index_charts(self, now: dt.datetime) -> None:
        """Keep every market-bar index's 5-minute chart at hand, so it opens at once even the first time it is clicked: a copy kept on disk is
        taken into memory, and a chart with none is loaded from Angel One, ONE index per INDEX_WARM_GAP_S and only while Angel One is not
        refusing candle requests (the strategy's own candles come first). Once kept, a chart costs no call until someone opens it."""
        import time as _t
        last = self.__dict__.get("_warm_at")
        if last is not None and (now - last).total_seconds() < INDEX_WARM_GAP_S:
            return
        if getattr(self, "_backoff_until", 0.0) > _t.monotonic():
            return
        charts = self.__dict__.setdefault("_index_charts", {})
        interval, days, _ttl = INDEX_CHART_TF["5m"]
        for key, (_ex, tok, _label) in TICKER_INDICES.items():
            if (key, "5m") in charts:
                continue
            stored = candle_store.get(tok, interval, now - dt.timedelta(days=days + 3))
            if len(stored):
                charts[(key, "5m")] = (now - dt.timedelta(hours=1), stored)       # at hand; refreshed in the background when it is opened
                continue
            self._warm_at = now
            self._refresh_later(("ic", key, "5m"), lambda k=key: self._load_index_chart(k, "5m"))
            return

    async def index_chart(self, key: str, tf: str) -> dict:
        """Candles of one market-bar index for its chart (asked for only when a person opens it; cached, so many viewers cost one Angel One call).
        A chart seen before opens AT ONCE from the cache, and after a restart from the copy kept on disk (candles.db), and is refreshed in the
        background when older than its time to live. Today's 5 / 15-minute candles are also built from the live ticks, so the chart is current and
        still shows when Angel One refuses the candle request (it refuses about half of them in market hours: the chart said "not available" right
        after the restart of 11 Nov 2026). Only a chart with nothing at all to show waits for Angel One (and says so when it fails).
        Returns {"candles": [...], "partial": True when the earlier candles are still missing (only today's from the ticks: the screen asks again soon)}."""
        ex, tok, _label = TICKER_INDICES[key]
        interval, days, ttl = INDEX_CHART_TF[tf]
        now = now_ist()
        charts = self.__dict__.setdefault("_index_charts", {})
        hit = charts.get((key, tf))
        if hit is None:
            stored = candle_store.get(tok, interval, now - dt.timedelta(days=days + 3))
            if len(stored):
                hit = charts[(key, tf)] = (now - dt.timedelta(seconds=ttl), stored)          # shown now, refreshed in the background below
        df, err = None, None
        if hit is None:
            try:
                df = await self._load_index_chart(key, tf)
            except Exception as exc:
                err = exc                                       # today's candles from live ticks may still be shown
        else:
            df = hit[1]
            if (now - hit[0]).total_seconds() >= ttl:
                self._refresh_later(("ic", key, tf), lambda: self._load_index_chart(key, tf))
        df = _naive(df)
        size = INDEX_CHART_BUCKET.get(interval)
        if size:
            df = _with_tick_buckets(df, _tick_buckets(self.feed.builder.minutes(tok), size, now))
        if df is None or not len(df):
            if err is not None:
                raise err
            return {"candles": [], "partial": False}
        partial = err is not None                               # Angel One refused and nothing was kept: today's candles from the ticks only
        tick = self.feed.latest.get(tok)
        live = tick.ltp if tick is not None and self.feed.healthy(tok) and _is_current_bar(df.index[-1], interval, now) else None
        rows = _candles_with_tick(df, live)                     # only the bar of the current period follows the tick (never an older one)
        vols = [float(v) if v == v else 0.0 for v in df["volume"]] if "volume" in df.columns else []      # an index has no traded volume (Angel One gives 0)
        return {"candles": [{**r, "volume": vols[i] if i < len(vols) else 0.0} for i, r in enumerate(rows)], "partial": partial}

    async def _load_index_chart(self, key: str, tf: str):
        ex, tok, _label = TICKER_INDICES[key]
        interval, days, _ttl = INDEX_CHART_TF[tf]
        df = await self._candles(ex, tok, interval, days, include_partial=True)
        self.__dict__.setdefault("_index_charts", {})[(key, tf)] = (now_ist(), df)
        try:
            candle_store.put(tok, interval, df)                 # a restart then opens the chart at once from this copy
        except Exception:
            log.warning("could not keep the %s chart candles of %s", tf, key)
        return df

    async def _load_index_close(self, name: str) -> tuple:
        ex, tok = INDEX_TOKENS[name] if name in INDEX_TOKENS else TICKER_INDICES[name][:2]
        now = now_ist()
        df = await self._candles(ex, tok, "ONE_DAY", 10, include_partial=True)
        closes = [] if df is None or df.empty else [(t.date(), float(c)) for t, c in df["close"].tail(6).items()]
        self._prev_close[name] = (now, closes)                     # the last sessions' closes: the reference is picked when read (_index_view)
        self._closes_to_disk()
        return self._prev_close[name]

    def _closes_to_disk(self) -> None:
        """Failing to store must never touch the engine."""
        try:
            rows = {k: [v[0].isoformat(), [[d.isoformat(), c] for d, c in v[1]]] for k, v in self._prev_close.items() if len(v) == 2}
            INDEX_CLOSE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = INDEX_CLOSE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows), encoding="utf-8")
            tmp.replace(INDEX_CLOSE_PATH)
        except (OSError, TypeError, ValueError, AttributeError):
            log.warning("could not store the market bar's closes")

    def _closes_from_disk(self) -> None:
        """Once per process: the closes stored before a restart, shown at once (they are refreshed in the background as usual)."""
        if self.__dict__.get("_closes_loaded"):
            return
        self._closes_loaded = True
        try:
            rows = json.loads(INDEX_CLOSE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for k, v in rows.items():
            if k in self._prev_close or not isinstance(v, list):
                continue
            try:
                at = dt.datetime.fromisoformat(v[0])                               # when it was loaded: _index_close_due decides what is missing
                if len(v) == 2 and isinstance(v[1], list):
                    closes = [(dt.date.fromisoformat(d), float(c)) for d, c in v[1]]
                elif len(v) == 5 and v[4]:                     # the first layout (10 Nov 2026): last close, the close before it, the last session's date
                    last = dt.date.fromisoformat(v[4])
                    closes = [(last - dt.timedelta(days=1), float(v[3])), (last, float(v[2]))] if v[3] is not None else [(last, float(v[2]))]
                else:
                    continue
                self._prev_close[k] = (at, closes)
            except (TypeError, ValueError):
                continue

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
