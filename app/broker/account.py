"""Read-only view of a user's own Angel One account: log in with the user's stored details, then read funds, positions and today's orders.

Nothing in this module can place, modify or cancel an order. The login is made fresh for each check (a check is a deliberate click, rate-limited per user).
"""
from __future__ import annotations

from app.broker import store
from app.providers import angelone_client as ao


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _funds(d: dict) -> dict:
    return {"available_cash": _num(d.get("availablecash")), "net": _num(d.get("net")), "utilised_debits": _num(d.get("utiliseddebits")),
            "span_margin": _num(d.get("utilisedspan")), "exposure_margin": _num(d.get("utilisedexposure")),
            "m2m_realised": _num(d.get("m2mrealized")), "m2m_unrealised": _num(d.get("m2munrealized"))}


def _position(p: dict) -> dict:
    return {"symbol": p.get("tradingsymbol"), "exchange": p.get("exchange"), "product": p.get("producttype"), "net_qty": _num(p.get("netqty")),
            "buy_qty": _num(p.get("buyqty")), "sell_qty": _num(p.get("sellqty")), "avg_price": _num(p.get("netprice")), "ltp": _num(p.get("ltp")),
            "pnl": _num(p.get("pnl")), "realised": _num(p.get("realised")), "unrealised": _num(p.get("unrealised"))}


def _order(o: dict) -> dict:
    return {"order_id": o.get("orderid"), "symbol": o.get("tradingsymbol"), "side": o.get("transactiontype"), "qty": _num(o.get("quantity")),
            "filled": _num(o.get("filledshares")), "price": _num(o.get("price")), "avg_fill": _num(o.get("averageprice")), "type": o.get("ordertype"),
            "status": o.get("status") or o.get("orderstatus"), "text": o.get("text"), "time": o.get("updatetime")}


async def login(c, user_id: str) -> ao.AngelOneSession:
    r = store.row(c, user_id)
    if r is None:
        raise store.BrokerError(404, "NOT_SET", "No Angel One account is set up for this user.")
    return await ao.login(**store.secrets(r))


async def _login_or_record(c, user_id: str) -> ao.AngelOneSession:
    try:
        return await login(c, user_id)
    except ao.AngelOneApiError as exc:
        store.set_status(c, user_id, "error", str(exc))
        raise store.BrokerError(400 if exc.kind == "auth" else 502, "LOGIN_FAILED", str(exc)) from exc


async def check(c, user_id: str) -> dict:
    """Log in once and remember the verdict. The only thing returned is whether it worked."""
    store.throttle(user_id)
    s = await _login_or_record(c, user_id)
    await ao.logout(s)
    store.set_status(c, user_id, "connected", None)
    return store.public(store.row(c, user_id))


async def summary(c, user_id: str) -> dict:
    store.throttle(user_id)
    s = await _login_or_record(c, user_id)
    try:
        funds, positions, orders = await ao.get_funds(s), await ao.get_positions(s), await ao.get_order_book(s)
    except ao.AngelOneApiError as exc:
        raise store.BrokerError(502, "READ_FAILED", str(exc)) from exc
    finally:
        await ao.logout(s)
    store.set_status(c, user_id, "connected", None)
    pos = [_position(p) for p in positions]
    return {"funds": _funds(funds), "positions": pos, "open_positions": [p for p in pos if p["net_qty"]], "orders": [_order(o) for o in orders],
            "note": "Read directly from your Angel One account just now. This screen cannot place or change orders."}
