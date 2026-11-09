"""
Intraday (15m/1h) OHLCV provider backed by Angel One SmartAPI — used only by
Expiry Level 1. Session is authenticated lazily and cached in-process (a
fresh TOTP-based login each time would waste Angel's rate limits and the
30-second TOTP window makes rapid re-logins fragile).
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo

import pandas as pd

from app.indicators.confirmed_bars import drop_incomplete_trailing_bars, drop_incomplete_trailing_daily_bar
from app.providers import angelone_scrip_master as scrip_master
from app.providers.angelone_client import AngelOneApiError, AngelOneInterval, AngelOneSession, get_candle_data, login
from app.services import angelone_credential_store

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("scanner.angelone_provider")

RELOGIN_STALL_S = 300.0      # safety net: no candle request has worked for this long (a rate limit lets some through), so the session may be dead after all ...
RELOGIN_MIN_GAP_S = 600.0    # ... then one fresh login, at most once in this many seconds


def _clock() -> float:
    """Seconds on a clock that only moves forward. It counts from the machine's boot, so it can be small (a CI runner, a server just rebooted)."""
    return time.monotonic()


def relogin_can_help(exc: AngelOneApiError) -> bool:
    """True when a fresh login cures the refusal: Angel One's JSON 'invalid / expired token' answer. False for a 403 rate-limit page or network
    trouble (the retryable errors): logging in again lifts no rate limit and only adds sessions on the account, which can be the very account
    real orders use. Re-logging in after every refused candle request cost ~650 logins a day on 5-6 Nov 2026, and Angel One then refused
    some logins as well."""
    if exc.kind == "auth":
        return True
    if exc.retryable:
        return False
    msg = str(exc).lower()
    return any(k in msg for k in ("token", "session", "expired", "unauthori", "ag8001", "ag8002", "ag8003", "ab1010"))

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
        self._last_ok = _clock()                # the last candle request that worked
        self._last_relogin = float("-inf")      # never (NOT 0.0: the clock starts at boot, so 0.0 would mean "just now" on a fresh machine)
        self.relogins = 0

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

    async def get_session(self) -> AngelOneSession:
        """Public accessor for the SAME session every REST call already
        uses — added 2026-09-25 for backend/live/market_data_service.py's
        WebSocket handshake (Authorization/x-api-key/x-client-code/
        x-feed-token), so the live engine never creates a second
        authentication path."""
        return await self._ensure_session()

    async def _candle_rows(self, **request) -> list:
        """getCandleData with the cached session. A refusal that says the session is dead (relogin_can_help) gets exactly one fresh login and
        one retry; so does a long stall where NOTHING has worked for RELOGIN_STALL_S (at most once per RELOGIN_MIN_GAP_S), in case a dead
        session ever shows up as something else. A 403 rate-limit page is raised as it is: the callers fall back to stored candles."""
        session = await self._ensure_session()
        try:
            rows = await get_candle_data(session, **request)
        except AngelOneApiError as exc:
            now = _clock()
            stalled = now - self._last_ok > RELOGIN_STALL_S and now - self._last_relogin > RELOGIN_MIN_GAP_S
            if not (relogin_can_help(exc) or stalled):
                raise
            log.warning("Angel One candle session looks dead (%s); logging in again once", str(exc)[:80])
            self.reset_session()
            self._last_relogin, self.relogins = now, self.relogins + 1
            session = await self._ensure_session()
            rows = await get_candle_data(session, **request)
        self._last_ok = _clock()
        return rows

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
        now_ist = dt.datetime.now(IST)
        from_dt = now_ist - dt.timedelta(days=days_back)
        from_date = _format_angel_date(from_dt.replace(tzinfo=None))
        to_date = _format_angel_date(now_ist.replace(tzinfo=None))

        # A session that died (say, invalidated elsewhere) is replaced once inside _candle_rows; a rate-limit refusal is not (see relogin_can_help).
        rows = await self._candle_rows(exchange=exch_seg, symbol_token=symbol_token, interval=interval, from_date=from_date, to_date=to_date)

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
        from_date = _format_angel_date(from_dt)
        to_date = _format_angel_date(to_dt)
        rows = await self._candle_rows(exchange=exch_seg, symbol_token=symbol_token, interval="ONE_DAY", from_date=from_date, to_date=to_date)
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
