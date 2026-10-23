"""
Thin REST client over Angel One's SmartAPI — the only market-data provider
in this project (daily, intraday 15m/1h, and stock-future OHLCV).

Endpoints/payload shapes are taken from Angel One's own SmartAPI reference
(https://smartapi.angelone.in/docs), the same source the project's other
Angel One integration (a separate, unrelated project on this machine) was
built from — nothing was copied from that project; this is an independent
implementation.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, TypeVar

import httpx
import pyotp

from app.ops import circuit, incidents
from app.ops.stats import tracked
from app.providers import call_metrics

logger = logging.getLogger("scanner.angelone_client")

T = TypeVar("T")

BASE_URL = "https://apiconnect.angelone.in"

ROUTES = {
    "login": "/rest/auth/angelbroking/user/v1/loginByPassword",
    "candle_data": "/rest/secure/angelbroking/historical/v1/getCandleData",
    "logout": "/rest/secure/angelbroking/user/v1/logout",
    "margin": "/rest/secure/angelbroking/margin/v1/batch",   # margin CALCULATOR only: it never places an order
}

AngelOneInterval = Literal[
    "ONE_MINUTE", "THREE_MINUTE", "FIVE_MINUTE", "TEN_MINUTE",
    "FIFTEEN_MINUTE", "THIRTY_MINUTE", "ONE_HOUR", "ONE_DAY",
]


def _rest_opened(b) -> None:
    incidents.record("angelone.rest", f"circuit OPEN after {b.threshold} failures in {b.window_s:.0f}s ({b.last_error})", severity="warning",
                     auto_action=f"calls blocked for {b.cooldown_s:.0f}s, then one trial call")


_rest = circuit.breaker("angelone.rest", threshold=8, window_s=60.0, cooldown_s=30.0, on_open=_rest_opened)


def _is_outage(exc: BaseException) -> bool:
    """Counts toward the breaker: network trouble, non-JSON replies and rate-limit refusals. NOT a deliberate 'bad token / bad parameter' answer."""
    if isinstance(exc, AngelOneApiError):
        return exc.retryable or exc.kind == "network" or "too many" in str(exc).lower() or "rate" in str(exc).lower()
    return True


class AngelOneApiError(Exception):
    def __init__(self, message: str, kind: Literal["auth", "network", "data"], retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        # True for transient-looking failures (network errors, non-JSON
        # responses — often a rate-limit page or a momentary proxy hiccup,
        # observed in practice on the historical-candle endpoint). False for
        # a real JSON error body Angel One returned deliberately (bad
        # symbol, invalid auth, etc.) — retrying those would just waste
        # calls against a strict daily/per-second quota for no benefit.
        self.retryable = retryable


@dataclass
class AngelOneSession:
    api_key: str
    jwt_token: str
    feed_token: str
    refresh_token: str
    client_code: str


def _base_headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-PrivateKey": api_key,
        # Angel's API expects these to be present; server-side we have no
        # meaningful local/public IP or MAC to report, so — like every
        # community integration — we send harmless placeholders.
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00",
    }


def generate_totp(base32_secret: str) -> str:
    return pyotp.TOTP(base32_secret).now()


async def _retry_async(
    fn: Callable[[], Awaitable[T]], max_attempts: int = 3, base_delay_seconds: float = 0.75
) -> T:
    """Retries only AngelOneApiError(retryable=True) with exponential backoff.
    A non-retryable error (a real API-reported failure) or the final attempt
    propagates immediately."""
    last_exc: AngelOneApiError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn()
        except AngelOneApiError as exc:
            if not exc.retryable or attempt == max_attempts - 1:
                raise
            last_exc = exc
            delay = base_delay_seconds * (2**attempt)
            logger.warning(
                "Angel One call failed (attempt %d/%d, retryable): %s — retrying in %.1fs",
                attempt + 1, max_attempts, exc, delay,
            )
            await asyncio.sleep(delay)
    assert last_exc is not None  # unreachable: loop always returns or raises above
    raise last_exc


async def login(api_key: str, client_code: str, pin: str, totp_secret: str) -> AngelOneSession:
    async def _attempt() -> AngelOneSession:
        totp = generate_totp(totp_secret)  # regenerated each attempt: a stale 30s-window code would just fail again
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.post(
                    BASE_URL + ROUTES["login"],
                    headers=_base_headers(api_key),
                    json={"clientcode": client_code, "password": pin, "totp": totp},
                )
            except httpx.HTTPError as exc:
                raise AngelOneApiError(f"Could not reach Angel One: {exc}", "network", retryable=True) from exc

        body = _parse_json_or_raise(response, "auth")
        if response.status_code != 200 or body.get("status") is not True or not body.get("data", {}).get("jwtToken"):
            # Wrong credentials/PIN/TOTP secret won't fix itself on retry.
            raise AngelOneApiError(body.get("message", "Angel One authentication failed"), "auth", retryable=False)

        data = body["data"]
        return AngelOneSession(
            api_key=api_key,
            jwt_token=data["jwtToken"],
            feed_token=data.get("feedToken", ""),
            refresh_token=data.get("refreshToken", ""),
            client_code=client_code,
        )

    return await _retry_async(_attempt)


async def logout(session: AngelOneSession) -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                BASE_URL + ROUTES["logout"],
                headers={**_base_headers(session.api_key), "Authorization": f"Bearer {session.jwt_token}"},
                json={"clientcode": session.client_code},
            )
    except httpx.HTTPError:
        pass  # best-effort — a failed logout shouldn't block anything


def _parse_json_or_raise(response: httpx.Response, kind: Literal["auth", "network", "data"]) -> dict[str, Any]:
    try:
        return response.json()
    except ValueError as exc:
        # A non-JSON body (e.g. a blank/HTML page) on a 403/5xx from this
        # endpoint has been observed to be transient in practice — retryable.
        raise AngelOneApiError(
            f"Angel One returned a non-JSON response (HTTP {response.status_code}).", kind, retryable=True
        ) from exc


@tracked("angelone.candles")
async def get_candle_data(
    session: AngelOneSession,
    exchange: str,
    symbol_token: str,
    interval: AngelOneInterval,
    from_date: str,
    to_date: str,
) -> list[list[Any]]:
    """`from_date`/`to_date` format: 'YYYY-MM-DD HH:MM'. Returns rows of
    [timestamp_iso, open, high, low, close, volume]. Transient failures
    (network errors, non-JSON responses) are retried automatically."""

    async def _attempt() -> list[list[Any]]:
        call_metrics.record_angelone_call()
        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                response = await client.post(
                    BASE_URL + ROUTES["candle_data"],
                    headers={**_base_headers(session.api_key), "Authorization": f"Bearer {session.jwt_token}"},
                    json={
                        "exchange": exchange,
                        "symboltoken": symbol_token,
                        "interval": interval,
                        "fromdate": from_date,
                        "todate": to_date,
                    },
                )
            except httpx.HTTPError as exc:
                raise AngelOneApiError(f"Could not reach Angel One: {exc}", "network", retryable=True) from exc

        body = _parse_json_or_raise(response, "data")
        if response.status_code != 200 or body.get("status") is not True:
            # A real JSON error body — Angel One deliberately rejected the
            # request (bad token, bad params, etc.), so retrying won't help.
            raise AngelOneApiError(body.get("message", "Unable to load candle data"), "data", retryable=False)

        return body.get("data") or []

    return await _rest.acall(lambda: _retry_async(_attempt), count_if=_is_outage)


@tracked("angelone.margin")
async def get_margin(session: AngelOneSession, positions: list[dict[str, Any]]) -> dict[str, Any]:
    """Angel One's margin calculator for a basket (SPAN + exposure, with the hedge benefit of the whole basket).
    `positions`: [{"exchange", "qty", "price", "productType", "token", "tradeType", "orderType"}]. Read-only: no order is placed."""

    async def _attempt() -> dict[str, Any]:
        call_metrics.record_angelone_call()
        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                response = await client.post(
                    BASE_URL + ROUTES["margin"],
                    headers={**_base_headers(session.api_key), "Authorization": f"Bearer {session.jwt_token}"},
                    json={"positions": positions},
                )
            except httpx.HTTPError as exc:
                raise AngelOneApiError(f"Could not reach Angel One: {exc}", "network", retryable=True) from exc
        body = _parse_json_or_raise(response, "data")
        if response.status_code != 200 or body.get("status") is not True:
            raise AngelOneApiError(body.get("message", "Unable to calculate margin"), "data", retryable=False)
        return body.get("data") or {}

    return await _rest.acall(lambda: _retry_async(_attempt), count_if=_is_outage)
