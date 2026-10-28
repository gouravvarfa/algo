"""
Upstox as the DATA SOURCE of the live engine (instead of Angel One): same methods the engine calls on AngelOneProvider, answered from Upstox's API.
Chosen with DATA_SOURCE=upstox (see app/main.py). Read only: it never places an order.

What differs from Angel One and is handled here:
- A symbol's "token" is Upstox's instrument_key (NSE_FO|48699, NSE_INDEX|Nifty Bank), so every place that passes a token around just carries that string.
- Upstox serves at most about a month of 1/5/15-minute candles per request (3 months for 30m / 1H, years for daily): longer windows are asked for in pieces.
- The login is OAuth and ends every day at about 03:30 IST. With no valid login every call says so in plain words (open the Upstox page and Connect).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd

from app.indicators.confirmed_bars import drop_incomplete_trailing_bars, drop_incomplete_trailing_daily_bar
from app.providers import upstox_client, upstox_instruments
from app.providers.angelone_scrip_master import FutureContract, ScripMatch
from app.services import upstox_credential_store as store

IST = ZoneInfo("Asia/Kolkata")
COLUMNS = ["open", "high", "low", "close", "volume"]

# Angel One's interval names (what the engine speaks) -> Upstox (unit, interval, longest span one request may cover in days, minutes per bar)
INTERVALS: dict[str, tuple[str, str, int, int | None]] = {
    "ONE_MINUTE": ("minutes", "1", 28, 1), "THREE_MINUTE": ("minutes", "3", 28, 3), "FIVE_MINUTE": ("minutes", "5", 28, 5),
    "TEN_MINUTE": ("minutes", "10", 28, 10), "FIFTEEN_MINUTE": ("minutes", "15", 28, 15), "THIRTY_MINUTE": ("minutes", "30", 85, 30),
    "ONE_HOUR": ("hours", "1", 85, 60), "ONE_DAY": ("days", "1", 3000, None),
}
INDEX_KEYS = {"BANKNIFTY": "NSE_INDEX|Nifty Bank", "NIFTY": "NSE_INDEX|Nifty 50"}
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


class UpstoxNotConnected(RuntimeError):
    """The Upstox login is missing or has ended."""


@dataclass
class UpstoxSession:
    access_token: str


def angel_style_symbol(underlying: str, expiry: dt.date) -> str:
    """BANKNIFTY27OCT26FUT: the name shape the rest of the app already reads (roll labels slice it), whatever the data source."""
    return f"{underlying}{expiry:%d}{MONTHS[expiry.month - 1]}{expiry:%y}FUT"


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS)


def frame(*candle_lists: list[list]) -> pd.DataFrame:
    """Upstox candles [ISO time, o, h, l, c, volume, oi], newest first, in one or more lists -> a DataFrame oldest first, indexed by the naive Indian
    start time of each candle (the shape Angel One's provider returns). A later list wins for a bar present in several (today's is the freshest)."""
    rows: dict[pd.Timestamp, tuple] = {}
    for candles in candle_lists:
        for r in candles:
            try:
                ts = pd.Timestamp(dt.datetime.fromisoformat(r[0]).replace(tzinfo=None))
                rows[ts] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0))
            except (IndexError, TypeError, ValueError):
                continue                                  # one odd row must not lose the whole series
    if not rows:
        return _empty()
    df = pd.DataFrame.from_dict(rows, orient="index", columns=COLUMNS).sort_index()
    df.index.name = "date"
    return df.astype(float)


class UpstoxProvider:
    name = "Upstox"
    index_tokens = {"BANKNIFTY": ("NSE", INDEX_KEYS["BANKNIFTY"]), "NIFTY": ("NSE", INDEX_KEYS["NIFTY"])}

    def __init__(self) -> None:
        self._rows: list[dict] | None = None

    @staticmethod
    def accepts_token(token: str) -> bool:
        """An Upstox instrument_key always looks like SEGMENT|id. An Angel One token (48699) does not: such saved instruments belong to the other data source."""
        return "|" in token

    # ---- the login -----------------------------------------------------------------------------------------------------------------------
    def access_token(self) -> str:
        creds = store.get()
        state = store.token_state(creds)
        if state != "connected":
            raise UpstoxNotConnected("The Upstox login has ended (it ends every day at about 03:30 IST). Open the Upstox page in the Developer console and press Connect with Upstox."
                                     if state == "expired" else "Upstox is not connected yet. Open the Upstox page in the Developer console and press Connect with Upstox.")
        return creds.access_token

    def forget_login(self) -> None:
        creds = store.get()
        if creds is not None:
            creds.access_token = None
            store.save(creds)

    async def _call(self, make_call):
        """Run one Upstox call with the current login; an ended login is forgotten, so the Upstox page says 'connect again'."""
        try:
            return await make_call(self.access_token())
        except upstox_client.UpstoxApiError as exc:
            if exc.kind == "auth":
                self.forget_login()
                raise UpstoxNotConnected("Upstox ended the login. Open the Upstox page in the Developer console and press Connect with Upstox again.") from exc
            raise

    async def call(self, make_call):
        """The public way to run any Upstox call with the saved login (option chain, margin): `await provider.call(lambda token: upstox_client.x(token, ...))`."""
        return await self._call(make_call)

    # ---- what the live feed and the broker-shaped bits expect ------------------------------------------------------------------------------
    async def get_session(self) -> UpstoxSession:
        return UpstoxSession(self.access_token())

    def reset_session(self) -> None:
        """Nothing to reset: every call reads the saved login."""

    def adopt_session(self, _session) -> None:
        """Only Angel One logs in from the Developer console's Angel One page; this source has its own page."""

    def make_feed(self):
        from app.live.upstox_feed import UpstoxPollFeed
        return UpstoxPollFeed(self)

    # ---- candles -------------------------------------------------------------------------------------------------------------------------
    async def get_intraday_ohlc(self, exch_seg: str, symbol_token: str, interval: str, days_back: int = 5, include_partial: bool = False) -> pd.DataFrame:
        """Candles of `symbol_token` (an Upstox instrument_key) for the last `days_back` days, oldest first. Only fully closed candles unless
        include_partial=True (the forming candle is kept, built from real trades up to now)."""
        if interval not in INTERVALS:
            raise ValueError(f"unsupported interval {interval}")
        unit, step, longest, minutes = INTERVALS[interval]
        today = dt.datetime.now(IST).date()
        start = today - dt.timedelta(days=max(1, days_back))
        pieces: list[list] = []
        end = today
        while end >= start:
            window_start = max(start, end - dt.timedelta(days=longest - 1))
            pieces.append(await self._call(lambda t, a=end, b=window_start: upstox_client.get_candles(t, symbol_token, unit, step, a.isoformat(), b.isoformat())))
            end = window_start - dt.timedelta(days=1)
        todays = await self._call(lambda t: upstox_client.get_intraday_candles(t, symbol_token, unit, step))
        df = frame(*reversed(pieces), todays)
        if df.empty:
            return df
        now = dt.datetime.now(IST).replace(tzinfo=None)
        if not include_partial:
            df = drop_incomplete_trailing_daily_bar(df, now) if minutes is None else drop_incomplete_trailing_bars(df, minutes, now)
        return df[COLUMNS].astype(float)

    async def get_daily_ohlc_range(self, exch_seg: str, symbol_token: str, from_dt: dt.datetime, to_dt: dt.datetime) -> pd.DataFrame:
        candles = await self._call(lambda t: upstox_client.get_candles(t, symbol_token, "days", "1", to_dt.date().isoformat(), from_dt.date().isoformat()))
        return frame(candles)

    # ---- symbols -------------------------------------------------------------------------------------------------------------------------
    async def _list(self) -> list[dict]:
        if self._rows is None:
            try:
                self._rows = await upstox_instruments.load()
            except upstox_instruments.InstrumentsUnavailable as exc:
                raise RuntimeError(str(exc)) from exc
        return self._rows

    async def resolve_index(self, name: str) -> ScripMatch | None:
        key = INDEX_KEYS.get(name.upper())
        if key:
            return ScripMatch(token=key, trading_symbol=name.upper(), exch_seg="NSE")
        for r in await self._list():
            if r["kind"] == "INDEX" and name.upper() in (r["symbol"].upper(), r["name"].upper()):
                return ScripMatch(token=r["key"], trading_symbol=r["symbol"], exch_seg="NSE")
        return None

    async def resolve_equity(self, symbol: str) -> ScripMatch | None:
        for r in await self._list():
            if r["kind"] == "EQ" and r["symbol"].upper() == symbol.strip().upper():
                return ScripMatch(token=r["key"], trading_symbol=r["symbol"], exch_seg="NSE")
        return None

    async def future_contracts(self, underlying: str) -> list[FutureContract]:
        """Every live futures contract of one underlying, nearest expiry first (what scrip_master.get_future_contracts gives for Angel One)."""
        want = underlying.strip().upper()
        out = []
        for r in await self._list():
            if r["kind"] == "FUT" and (r.get("underlying") or "").upper() == want and r.get("expiry"):
                expiry = dt.date.fromisoformat(r["expiry"])
                out.append(FutureContract(underlying=want, trading_symbol=angel_style_symbol(want, expiry), token=r["key"], exch_seg="NFO",
                                          expiry=expiry.isoformat(), is_index=want in INDEX_KEYS, lot_size=int(float(r.get("lot") or 0))))
        return sorted(out, key=lambda c: c.expiry)

    async def resolve_stock_future(self, symbol: str) -> ScripMatch | None:
        contracts = await self.future_contracts(symbol)
        return ScripMatch(token=contracts[0].token, trading_symbol=contracts[0].trading_symbol, exch_seg="NFO") if contracts else None
