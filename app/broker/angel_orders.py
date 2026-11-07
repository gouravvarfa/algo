"""The ONLY place in the app that sends an order to Angel One (place and cancel). Keep it small and read it whole.

No retry anywhere in here, on purpose: if a request times out we cannot know whether Angel One received it, and sending it again could open the position twice.
A timeout therefore comes back as outcome 'unknown' and the person is told to look at their Angel One order book. Everything that decides WHETHER an order may be
sent (mode, limits, margin, price sanity, hours) lives in app/broker/orders.py and runs before this module is called.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.providers.angelone_client import BASE_URL, AngelOneSession, _base_headers, _parse_json_or_raise, AngelOneApiError

PLACE = "/rest/secure/angelbroking/order/v1/placeOrder"
CANCEL = "/rest/secure/angelbroking/order/v1/cancelOrder"


class Unknown(Exception):
    """The request may or may not have reached Angel One."""


def _headers(s: AngelOneSession) -> dict[str, str]:
    return {**_base_headers(s.api_key), "Authorization": f"Bearer {s.jwt_token}"}


async def _post(s: AngelOneSession, route: str, body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            response = await client.post(BASE_URL + route, headers=_headers(s), json=body)
        except httpx.HTTPError as exc:
            raise Unknown(f"No answer from Angel One ({type(exc).__name__}). The order may or may not have been received.") from exc
    try:
        return _parse_json_or_raise(response, "data")
    except AngelOneApiError as exc:
        raise Unknown("Angel One sent an unreadable answer. The order may or may not have been received.") from exc


async def place_limit(s: AngelOneSession, *, trading_symbol: str, token: str, side: str, qty: int, price: float, product: str) -> str:
    """One NFO limit order, day validity. Returns Angel One's order id; raises AngelOneApiError when Angel One refused it, Unknown when we cannot tell."""
    body = {"variety": "NORMAL", "tradingsymbol": trading_symbol, "symboltoken": token, "transactiontype": side, "exchange": "NFO", "ordertype": "LIMIT",
            "producttype": product, "duration": "DAY", "price": f"{price:.2f}", "squareoff": "0", "stoploss": "0", "quantity": str(qty)}
    d = await _post(s, PLACE, body)
    oid = (d.get("data") or {}).get("orderid") if isinstance(d.get("data"), dict) else None
    if d.get("status") is True and oid:
        return str(oid)
    if d.get("status") is True:
        raise Unknown("Angel One accepted the request but gave no order id. Check your Angel One order book.")
    raise AngelOneApiError(d.get("message") or "Angel One rejected the order", "data", retryable=False)


async def cancel(s: AngelOneSession, order_id: str) -> None:
    d = await _post(s, CANCEL, {"variety": "NORMAL", "orderid": order_id})
    if d.get("status") is not True:
        raise AngelOneApiError(d.get("message") or "Angel One did not cancel the order", "data", retryable=False)
