"""Read-only view of a user's own Angel One account: log in with the user's stored details, then read funds, positions and today's orders.

Nothing in this module can place, modify or cancel an order. Every screen and the real-order executor share ONE session per person and day
(app/broker/orders.py _session): Test connection and the preflight check log in afresh and that new session is the one everybody uses from then on;
nothing here logs out (a logout here ended the session the executor was using).
"""
from __future__ import annotations

import asyncio

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
    """Only a refused login (wrong details) marks the account as failed. A network hiccup must not quietly switch real trading off until someone
    presses Test connection; and a login that works again puts the account back to connected."""
    try:
        s = await login(c, user_id)
    except ao.AngelOneApiError as exc:
        refused = exc.kind == "auth" and not exc.retryable      # Angel One said the details are wrong; a 403 rate-limit page at login is not that
        if refused:
            store.set_status(c, user_id, "error", str(exc))
        raise store.BrokerError(400 if refused else 502, "LOGIN_FAILED", str(exc)) from exc
    r = store.row(c, user_id)
    if r is not None and r["status"] == "error":
        store.set_status(c, user_id, "connected", None)
    return s


async def check(c, user_id: str) -> dict:
    """Log in once and remember the verdict. The only thing returned is whether it worked."""
    from app.broker import orders
    store.throttle(user_id)
    await orders._session(c, user_id, fresh=True)
    store.set_status(c, user_id, "connected", None)
    return store.public(store.row(c, user_id))


async def summary(c, user_id: str) -> dict:
    from app.broker import orders as _orders
    store.throttle(user_id)
    s = await _orders._session(c, user_id)
    try:
        funds, positions, orders = await ao.get_funds(s), await ao.get_positions(s), await ao.get_order_book(s)
    except ao.AngelOneApiError as exc:
        _orders.drop_if_dead(user_id, exc, s)
        raise store.BrokerError(502, "READ_FAILED", str(exc)) from exc
    store.set_status(c, user_id, "connected", None)
    pos = [_position(p) for p in positions]
    return {"funds": _funds(funds), "positions": pos, "open_positions": [p for p in pos if p["net_qty"]], "orders": [_order(o) for o in orders],
            "note": "Read directly from your Angel One account just now. This screen cannot place or change orders."}


async def preflight(c, user_id: str) -> dict:
    """A dry check of everything real trading depends on, against the person's real Angel One account, WITHOUT sending any order: login, funds, positions, today's
    orders, a live price, the margin calculator. Each step is reported on its own so a failure says exactly which part is wrong."""
    from app.options import master
    store.throttle(user_id)
    steps: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        steps.append({"name": name, "ok": bool(ok), "detail": detail})

    from app.broker import orders
    try:
        s = await orders._session(c, user_id, fresh=True)        # a fresh login, kept as the session everybody uses (no second session, no logout)
    except store.BrokerError as exc:
        if exc.code != "LOGIN_FAILED":
            return {"ok": False, "steps": [{"name": "Account details", "ok": False, "detail": exc.message}], "hint": None}
        return {"ok": False, "steps": [{"name": "Login", "ok": False, "detail": exc.message}],
                "hint": "Check the client code, PIN, SmartAPI key and the TOTP secret. If everything is right and it still fails only here, the SmartAPI app may need this server's IP address registered."}
    add("Login", True, "Angel One accepted your client code, PIN and TOTP")
    async def step(name, coro, show):
        try:
            v = await coro
            add(name, True, show(v))
            return v
        except ao.AngelOneApiError as exc:
            add(name, False, str(exc))
            return None
    await step("Funds", ao.get_funds(s), lambda d: f"Available cash {_num(d.get('availablecash')):,.0f}" if _num(d.get("availablecash")) is not None else "Read, but no cash figure came back")
    await step("Positions", ao.get_positions(s), lambda d: f"{len([p for p in d if _num(p.get('netqty'))])} open position(s)")
    await step("Today's orders", ao.get_order_book(s), lambda d: f"{len(d)} order(s) today")
    try:
        info = await asyncio.to_thread(master.load_underlying, "NIFTY")
        ltp = await ao.get_ltp(s, "NSE", info.spot_symbol, info.spot_token)
        add("Live price", ltp is not None, f"NIFTY last price {ltp:,.2f}" if ltp is not None else "Angel One returned no price")
        nearest = min((k for k in info.contracts), key=lambda k: (k.expiry, abs(k.strike - (ltp or k.strike))), default=None)
        if nearest is not None:
            d = await ao.get_margin(s, [{"exchange": "NFO", "qty": nearest.lot_size, "price": 0, "productType": "CARRYFORWARD", "token": nearest.token, "tradeType": "SELL", "orderType": "MARKET"}])
            add("Margin calculator", d.get("totalMarginRequired") is not None, f"Margin for a one-lot sample: {_num(d.get('totalMarginRequired')) or 0:,.0f} (calculation only, nothing was ordered)")
    except ao.AngelOneApiError as exc:
        add("Live price / margin", False, str(exc))
    except Exception as exc:                       # the instrument list could not be loaded
        add("Instrument list", False, f"Could not load Angel One's instrument list: {type(exc).__name__}")
    ok = all(x["ok"] for x in steps)
    # the login worked, so the account is connected. A read that failed is shown in red but never switches real trading off: a busy Angel One
    # (a 403 rate-limit page) during this dry check used to mark the account 'error', which put it on paper and released an open real position.
    store.set_status(c, user_id, "connected", None)
    return {"ok": ok, "steps": steps, "hint": None if ok else "Fix the first red step. Nothing was ordered."}
