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


async def _get(url: str, token: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers={"Accept": "application/json", "Authorization": f"Bearer {token}"})
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
