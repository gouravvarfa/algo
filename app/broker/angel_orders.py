"""The ONLY place in the app that sends an order to Angel One (place and cancel). Keep it small and read it whole.

No retry anywhere in here, on purpose: if a request times out we cannot know whether Angel One received it, and sending it again could open the position twice.
A timeout therefore comes back as outcome 'unknown' and the person is told to look at their Angel One order book. Everything that decides WHETHER an order may be
sent (mode, limits, margin, price sanity, hours) lives in app/broker/orders.py and runs before this module is called.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.providers import angelone_client as ao, api_health
from app.providers.angelone_client import AngelOneSession, _base_headers, _parse_json_or_raise, AngelOneApiError

PLACE = "/rest/secure/angelbroking/order/v1/placeOrder"
CANCEL = "/rest/secure/angelbroking/order/v1/cancelOrder"


class Unknown(Exception):
    """The request may or may not have reached Angel One."""


def _headers(s: AngelOneSession) -> dict[str, str]:
    return {**_base_headers(s.api_key), "Authorization": f"Bearer {s.jwt_token}"}


async def _post(s: AngelOneSession, route: str, body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0, transport=api_health.transport()) as client:
        try:
            response = await client.post(ao.BASE_URL + route, headers=_headers(s), json=body)
        except httpx.HTTPError as exc:
            raise Unknown(f"No answer from Angel One ({type(exc).__name__}). The order may or may not have been received.") from exc
    try:
        return _parse_json_or_raise(response, "data")
    except AngelOneApiError as exc:
        raise Unknown("Angel One sent an unreadable answer. The order may or may not have been received.") from exc


def _refused(d: dict[str, Any], default: str) -> AngelOneApiError:
    """Angel One's refusal, with its error code in the text (AB1010 / AG8001 = the session had expired: nothing was received, see
    angelone_client.session_dead; any other code = this request itself was refused)."""
    msg = str(d.get("message") or default)
    code = d.get("errorcode") or d.get("errorCode")
    return AngelOneApiError(f"{msg} ({code})" if code and str(code) not in msg else msg, "data", retryable=False)


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
    raise _refused(d, "Angel One rejected the order")


async def cancel(s: AngelOneSession, order_id: str) -> None:
    d = await _post(s, CANCEL, {"variety": "NORMAL", "orderid": order_id})
    if d.get("status") is not True:
        raise _refused(d, "Angel One did not cancel the order")


MODIFY = "/rest/secure/angelbroking/order/v1/modifyOrder"
_PLACE_KEYS = {"variety", "tradingsymbol", "symboltoken", "transactiontype", "exchange", "ordertype", "producttype", "duration", "price", "triggerprice", "quantity", "ordertag",
               "squareoff", "stoploss"}


def _checked(payload: dict[str, Any]) -> dict[str, Any]:
    """Only the fields Angel One's order call knows, only the order types the app uses (LIMIT, STOPLOSS_LIMIT, and MARKET: the fallback when a LIMIT
    entry / exit does not fill in 3 s, decided with the user 10 Nov 2026), always a day order. Anything else is a bug and stops here."""
    extra = set(payload) - _PLACE_KEYS - {"orderid"}
    if extra:
        raise AngelOneApiError(f"unexpected order field(s): {sorted(extra)}", "data", retryable=False)
    if payload.get("ordertype") not in ("LIMIT", "STOPLOSS_LIMIT", "MARKET") or payload.get("duration") != "DAY":
        raise AngelOneApiError("only LIMIT, STOPLOSS_LIMIT and MARKET day orders may be sent", "data", retryable=False)
    if (payload.get("ordertype") == "MARKET" and payload.get("variety") != "NORMAL") or payload.get("variety") not in ("NORMAL", "STOPLOSS") or payload.get("transactiontype") not in ("BUY", "SELL") or not payload.get("symboltoken"):
        raise AngelOneApiError("order is missing its variety, side or contract token", "data", retryable=False)
    return {**payload, "squareoff": "0", "stoploss": "0"} if "squareoff" not in payload else payload


async def place_payload(s: AngelOneSession, payload: dict[str, Any]) -> str:
    """Send one prepared LIMIT or STOPLOSS_LIMIT order (the executor builds it). Same contract as place_limit: id, AngelOneApiError (refused) or Unknown (cannot tell)."""
    d = await _post(s, PLACE, _checked(payload))
    oid = (d.get("data") or {}).get("orderid") if isinstance(d.get("data"), dict) else None
    if d.get("status") is True and oid:
        return str(oid)
    if d.get("status") is True:
        raise Unknown("Angel One accepted the request but gave no order id. Check your Angel One order book.")
    raise _refused(d, "Angel One rejected the order")


async def modify_payload(s: AngelOneSession, order_id: str, payload: dict[str, Any]) -> None:
    """Change the price / trigger of an order we placed (the stop order following the strategy's stop)."""
    p = _checked(payload)
    keys = ("variety", "ordertype", "producttype", "duration", "price", "triggerprice", "quantity", "tradingsymbol", "symboltoken", "exchange")
    body = {**{k: p[k] for k in keys if k in p}, "orderid": order_id}                   # exactly the fields Angel One's modify call takes
    d = await _post(s, MODIFY, body)
    if d.get("status") is not True:
        raise _refused(d, "Angel One did not modify the order")
