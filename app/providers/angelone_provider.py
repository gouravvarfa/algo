"""
Intraday (15m/1h) OHLCV provider backed by Angel One SmartAPI — used only by
Expiry Level 1. Session is authenticated lazily and cached in-process (a
fresh TOTP-based login each time would waste Angel's rate limits and the
30-second TOTP window makes rapid re-logins fragile).
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd

from app.indicators.confirmed_bars import drop_incomplete_trailing_bars, drop_incomplete_trailing_daily_bar
from app.providers import angelone_scrip_master as scrip_master
from app.providers.angelone_client import AngelOneApiError, AngelOneInterval, AngelOneSession, get_candle_data, login
from app.services import angelone_credential_store

IST = ZoneInfo("Asia/Kolkata")

INTERVAL_MINUTES: dict[AngelOneInterval, int] = {
    "ONE_MINUTE": 1,
    "THREE_MINUTE": 3,
    "FIVE_MINUTE": 5,
    "TEN_MINUTE": 10,
    "FIFTEEN_MINUTE": 15,
    "THIRTY_MINUTE": 30,
    "ONE_HOUR": 60,
}


class AngelOneNotConfiguredError(RuntimeError):
    pass


def _format_angel_date(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")


class AngelOneProvider:
    def __init__(self) -> None:
        self._session: AngelOneSession | None = None

    async def _ensure_session(self) -> AngelOneSession:
        credentials = angelone_credential_store.get_credentials()
        if credentials is None:
            raise AngelOneNotConfiguredError(
                "Angel One credentials not configured — connect from the Expiry Level 1 page, "
                "or set ANGELONE_API_KEY/CLIENT_CODE/PIN/TOTP_SECRET in .env."
            )
        if self._session is None:
            self._session = await login(
                credentials.api_key, credentials.client_code, credentials.pin, credentials.totp_secret
            )
        return self._session

    def reset_session(self) -> None:
        """Forces the next request to re-authenticate — call after credentials change."""
        self._session = None

    def adopt_session(self, session: AngelOneSession) -> None:
        """Use a session that was just opened by the Developer console's "Connect" (it already proved the details work), so saving new
        credentials costs ONE Angel One login, not two."""
        self._session = session

    async def get_session(self) -> AngelOneSession:
        """Public accessor for the SAME session every REST call already
        uses — added 2026-09-25 for backend/live/market_data_service.py's
        WebSocket handshake (Authorization/x-api-key/x-client-code/
        x-feed-token), so the live engine never creates a second
        authentication path."""
        return await self._ensure_session()

    async def get_intraday_ohlc(
        self, exch_seg: str, symbol_token: str, interval: AngelOneInterval, days_back: int = 5,
        include_partial: bool = False,
    ) -> pd.DataFrame:
        """
        Returns a DataFrame of ONLY fully-closed candles (the still-forming
        current bar, if any, is dropped) — this is what makes "use only
        confirmed candles" true regardless of when during the session this
        is called.

        include_partial=True (2026-09-28, chart-only) keeps that still-
        forming current bar instead of dropping it — Angel One's own
        historical-candle endpoint already returns it (built from real
        completed ticks up to now, never fabricated); every OTHER caller
        (the scanner's daily fetch via market_data_router.py, Expiry Level
        1/5) omits this and is completely unaffected — they still only ever
        see confirmed candles, exactly as before.
        """
        session = await self._ensure_session()
        now_ist = dt.datetime.now(IST)
        from_dt = now_ist - dt.timedelta(days=days_back)
        from_date = _format_angel_date(from_dt.replace(tzinfo=None))
        to_date = _format_angel_date(now_ist.replace(tzinfo=None))

        try:
            rows = await get_candle_data(
                session, exchange=exch_seg, symbol_token=symbol_token, interval=interval,
                from_date=from_date, to_date=to_date,
            )
        except AngelOneApiError:
            # Angel One allows only ONE active API session per account —
            # logging in from the app/web elsewhere silently invalidates
            # this cached session, and every further call then fails
            # identically no matter how many times get_candle_data's own
            # internal retry loop tries again with the SAME dead session
            # (this was a real bug: an entire scan could fail 100% of its
            # stocks this way, recoverable only by restarting the backend).
            # Force exactly one fresh re-login and retry before giving up —
            # if the credentials themselves are the problem, this second
            # attempt fails too and the real error still surfaces normally.
            self.reset_session()
            session = await self._ensure_session()
            rows = await get_candle_data(
                session, exchange=exch_seg, symbol_token=symbol_token, interval=interval,
                from_date=from_date, to_date=to_date,
            )

        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        df = df.set_index("date").sort_index()

        now_naive = now_ist.replace(tzinfo=None)
        if not include_partial:
            if interval == "ONE_DAY":
                df = drop_incomplete_trailing_daily_bar(df, now_naive)
            else:
                df = drop_incomplete_trailing_bars(df, INTERVAL_MINUTES[interval], now_naive)

        return df[["open", "high", "low", "close", "volume"]].astype(float)

    async def get_daily_ohlc_range(
        self, exch_seg: str, symbol_token: str, from_dt: dt.datetime, to_dt: dt.datetime
    ) -> pd.DataFrame:
        """
        Same session/retry-on-dead-session handling as get_intraday_ohlc,
        but for an EXPLICIT date range instead of "the last N days from
        now" — used only by the Cup Breakout scanner's chunked multi-year
        history fetch (backend/providers/cup_history.py). Does not drop a
        trailing incomplete bar (the caller is asking for a specific past
        window, not "up to now"); Cup's own pipeline handles the current
        in-progress month separately via to_monthly().
        """
        session = await self._ensure_session()
        from_date = _format_angel_date(from_dt)
        to_date = _format_angel_date(to_dt)
        try:
            rows = await get_candle_data(
                session, exchange=exch_seg, symbol_token=symbol_token, interval="ONE_DAY",
                from_date=from_date, to_date=to_date,
            )
        except AngelOneApiError:
            self.reset_session()
            session = await self._ensure_session()
            rows = await get_candle_data(
                session, exchange=exch_seg, symbol_token=symbol_token, interval="ONE_DAY",
                from_date=from_date, to_date=to_date,
            )
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        df = df.set_index("date").sort_index()
        return df[["open", "high", "low", "close", "volume"]].astype(float)

    async def resolve_index(self, name: str) -> scrip_master.ScripMatch | None:
        return await scrip_master.resolve_index(name)

    async def resolve_equity(self, symbol: str) -> scrip_master.ScripMatch | None:
        return await scrip_master.resolve_equity(symbol)

    async def resolve_stock_future(self, symbol: str) -> scrip_master.ScripMatch | None:
        return await scrip_master.resolve_stock_future(symbol)
