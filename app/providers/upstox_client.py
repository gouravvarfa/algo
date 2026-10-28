"""
Thin REST client over Upstox's API (OAuth login, profile, today's trades). READ ONLY: nothing here places or changes an order.

Login is OAuth: build_login_url() -> the person logs in on Upstox -> Upstox sends them back to the redirect URI with ?code=... ->
exchange_code() turns the code into an access token (this needs the API secret, so it only ever runs on the server).
"""
from __future__ import annotations

import urllib.parse
from typing import Any, Literal

import httpx

BASE_URL = "https://api.upstox.com/v2"
LOGIN_DIALOG = BASE_URL + "/login/authorization/dialog"
TOKEN_URL = BASE_URL + "/login/authorization/token"
PROFILE_URL = BASE_URL + "/user/profile"
TRADES_TODAY_URL = BASE_URL + "/order/trades/get-trades-for-day"


class UpstoxApiError(Exception):
    def __init__(self, message: str, kind: Literal["auth", "network", "data"]):
        super().__init__(message)
        self.kind = kind


def build_login_url(api_key: str, redirect_uri: str, state: str) -> str:
    q = urllib.parse.urlencode({"response_type": "code", "client_id": api_key, "redirect_uri": redirect_uri, "state": state})
    return f"{LOGIN_DIALOG}?{q}"


def _message(body: Any, default: str) -> str:
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict) and errors[0].get("message"):
            return str(errors[0]["message"])
        return str(body.get("error_description") or body.get("message") or default)
    return default


def _json(response: httpx.Response) -> Any:
    try:
        return response.json() if response.content else {}
    except ValueError:
        return {}


async def exchange_code(code: str, api_key: str, api_secret: str, redirect_uri: str) -> dict:
    """The one-time code from the login page -> {'access_token', 'user_id', 'user_name', ...}."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(TOKEN_URL, data={"code": code, "client_id": api_key, "client_secret": api_secret, "redirect_uri": redirect_uri,
                                                   "grant_type": "authorization_code"},
                                  headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise UpstoxApiError(f"Could not reach Upstox: {exc}", "network") from exc
    body = _json(r)
    if not r.is_success or not isinstance(body, dict) or not body.get("access_token"):
        raise UpstoxApiError(_message(body, "Upstox refused the login code"), "auth")
    return body


async def _get(url: str, token: str, params: dict | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, params=params, headers={"Accept": "application/json", "Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise UpstoxApiError(f"Could not reach Upstox: {exc}", "network") from exc
    body = _json(r)
    if r.status_code in (401, 403):
        raise UpstoxApiError(_message(body, "The Upstox login has ended"), "auth")
    if not r.is_success or not isinstance(body, dict) or body.get("status") != "success":
        raise UpstoxApiError(_message(body, "Upstox did not answer as expected"), "data")
    return body


async def get_profile(token: str) -> dict:
    return (await _get(PROFILE_URL, token)).get("data") or {}


async def get_trades_today(token: str) -> list[dict]:
    return (await _get(TRADES_TODAY_URL, token)).get("data") or []


# ---- market data (read only) --------------------------------------------------------------------------------------------------------------
V3_URL = "https://api.upstox.com/v3"
QUOTES_URL = BASE_URL + "/market-quote/quotes"


def _key_in_path(key: str) -> str:
    return urllib.parse.quote(key, safe="")        # "NSE_INDEX|Nifty Bank" -> "NSE_INDEX%7CNifty%20Bank"


async def get_quotes(token: str, keys: list[str]) -> dict[str, dict]:
    """{instrument_key: quote}. Upstox answers with 'NSE_INDEX:Nifty Bank' style keys; each quote carries the real key in 'instrument_token'."""
    body = await _get(QUOTES_URL, token, {"instrument_key": ",".join(keys)})
    out: dict[str, dict] = {}
    for k, v in (body.get("data") or {}).items():
        if isinstance(v, dict):
            out[v.get("instrument_token") or k.replace(":", "|", 1)] = v
    return out


LTP_URL = BASE_URL + "/market-quote/ltp"
LTP_KEYS_PER_CALL = 400


async def get_ltp(token: str, keys: list[str]) -> dict[str, float]:
    """{instrument_key: last traded price} for any number of keys (asked for in groups). Light: the live feed polls this every second or two."""
    out: dict[str, float] = {}
    for i in range(0, len(keys), LTP_KEYS_PER_CALL):
        body = await _get(LTP_URL, token, {"instrument_key": ",".join(keys[i:i + LTP_KEYS_PER_CALL])})
        for k, v in (body.get("data") or {}).items():
            if isinstance(v, dict) and isinstance(v.get("last_price"), (int, float)):
                out[v.get("instrument_token") or k.replace(":", "|", 1)] = float(v["last_price"])
    return out


OPTION_CONTRACT_URL = BASE_URL + "/option/contract"
OPTION_CHAIN_URL = BASE_URL + "/option/chain"
MARGIN_URL = BASE_URL + "/charges/margin"


async def get_option_contracts(token: str, underlying_key: str) -> list[dict]:
    """Every live option contract of one index: instrument_key, expiry, strike_price, instrument_type (CE / PE), lot_size, tick_size ..."""
    return (await _get(OPTION_CONTRACT_URL, token, {"instrument_key": underlying_key})).get("data") or []


async def get_option_chain(token: str, underlying_key: str, expiry_date: str) -> list[dict]:
    """The whole chain of one expiry in one call: per strike the call and the put with market_data (ltp, volume, oi, bid / ask) and option_greeks."""
    return (await _get(OPTION_CHAIN_URL, token, {"instrument_key": underlying_key, "expiry_date": expiry_date})).get("data") or []


async def get_margin(token: str, instruments: list[dict]) -> dict:
    """Upstox's margin CALCULATOR for a basket (it never places an order): {'margins': [...per leg], 'required_margin', 'final_margin' (after the hedge benefit)}."""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(MARGIN_URL, json={"instruments": instruments}, headers={"Accept": "application/json", "Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise UpstoxApiError(f"Could not reach Upstox: {exc}", "network") from exc
    body = _json(r)
    if r.status_code in (401, 403):
        raise UpstoxApiError(_message(body, "The Upstox login has ended"), "auth")
    if not r.is_success or not isinstance(body, dict) or body.get("status") != "success":
        raise UpstoxApiError(_message(body, "Upstox did not answer as expected"), "data")
    return body.get("data") or {}


async def get_candles(token: str, key: str, unit: str, interval: str, to_date: str, from_date: str) -> list[list]:
    """Finished candles between two dates (newest first): [ISO time, open, high, low, close, volume, open interest]."""
    body = await _get(f"{V3_URL}/historical-candle/{_key_in_path(key)}/{unit}/{interval}/{to_date}/{from_date}", token)
    return (body.get("data") or {}).get("candles") or []


async def get_intraday_candles(token: str, key: str, unit: str, interval: str) -> list[list]:
    """Today's candles, the forming one included (newest first)."""
    body = await _get(f"{V3_URL}/historical-candle/intraday/{_key_in_path(key)}/{unit}/{interval}", token)
    return (body.get("data") or {}).get("candles") or []
