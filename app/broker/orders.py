"""Real orders, step by step: a PREVIEW that runs every safety check and shows the person exactly what would be sent, then a SEND of that one preview.

Rules, all enforced here on the server (a screen can hide a button, it cannot skip a check):
  * only an account in effective REAL mode (platform master switch on, admin grant, verified login, person's own password confirmation) can send anything;
  * only NIFTY / BANKNIFTY options that are listed today, only LIMIT orders, price within 10% of the last traded price and on a valid tick;
  * market hours only (Mon-Fri 09:15-15:30 IST);
  * an EXIT (an order that only reduces an open position) is always allowed within the hours; an ENTRY also needs: lots within the person's limit (per order and in total
    open), today's loss under the daily cap, margin available (fail closed: if margin cannot be computed, the order is not sent), at most 30 orders a day;
  * a preview can be sent ONCE and only for two minutes; everything is checked again at the moment of sending, including the master switch;
  * every preview and every outcome is stored (table broker_orders) and the audit log gets one entry per order. No retry, ever (see angel_orders.py).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import time
import uuid

from app.broker import account, angel_orders, store
from app.db import audit
from app.ops import flags
from app.options import master
from app.providers import angelone_client as ao

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
PREVIEW_TTL_S = 120.0
MAX_PRICE_DEVIATION = 0.10
MAX_ORDERS_PER_DAY = 30
MIN_GAP_S = 2.0
PRODUCTS = ("INTRADAY", "CARRYFORWARD")
SIDES = ("BUY", "SELL")
OPEN_T, CLOSE_T = dt.time(9, 15), dt.time(15, 30)

# One Angel One session per person for the whole day (Angel One ends sessions at midnight IST), shared by the real-order executor and every
# account screen. A new login is made only on a new day or when Angel One says the session is no longer valid (drop_if_dead): every login adds a
# session on the account, and the person's account can be the very one the market data uses (it was, Nov 2026); a login every 10 minutes
# (the rule before) only multiplied them.
_sessions: dict[str, tuple[dt.date, ao.AngelOneSession]] = {}
_session_locks: dict[str, asyncio.Lock] = {}
_last_send: dict[str, float] = {}


class OrderError(Exception):
    def __init__(self, status: int, code: str, message: str, checks: list | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.checks = status, code, message, checks


def _options_on() -> None:
    if not flags.get("real.options"):
        raise OrderError(403, "OPTIONS_OFF", "Real option orders are not switched on. For now real orders are planned for the Top-Bottom strategy only.")


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def market_open(now: dt.datetime) -> bool:
    return now.weekday() < 5 and OPEN_T <= now.time() <= CLOSE_T


def chk(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


def position_of(positions: list[dict], trading_symbol: str) -> int:
    for p in positions:
        if p.get("tradingsymbol") == trading_symbol:
            try:
                return int(float(p.get("netqty") or 0))
            except ValueError:
                return 0
    return 0


def open_lots(positions: list[dict], symbol: str, lot_size: int) -> float:
    """Lots currently open in this index's options, any strike or expiry."""
    pat = re.compile(rf"^{re.escape(symbol)}\d")
    total = 0.0
    for p in positions:
        if pat.match(p.get("tradingsymbol") or ""):
            try:
                total += abs(float(p.get("netqty") or 0)) / lot_size
            except ValueError:
                pass
    return total


def classify(side: str, qty: int, net: int) -> str:
    """exit = only reduces an open position; entry = opens or adds; flip = would cross through zero (refused: exit first, then enter)."""
    if (side == "BUY" and net < 0) or (side == "SELL" and net > 0):
        return "exit" if qty <= abs(net) else "flip"
    return "entry"


def evaluate(*, row, req: dict, contract, ltp: float | None, positions: list[dict], funds: dict, margin_required: float | None, orders_today: int, now: dt.datetime) -> tuple[list[dict], str]:
    """Every check, in the order a person would think of them. Returns (checks, intent). An order may be sent only if every check is ok."""
    lim = store.limits(row)
    lots, side, price = req["lots"], req["side"], req["price"]
    qty = lots * contract.lot_size
    net = position_of(positions, contract.symbol)
    intent = classify(side, qty, net)
    cash = None
    try:
        cash = float(funds.get("availablecash"))
    except (TypeError, ValueError):
        pass
    out = [
        chk("Real mode", store.effective_mode(row) == "real", "Real mode is on and nothing blocks it" if store.effective_mode(row) == "real" else "; ".join(store.blockers(row)) or "You are on paper"),
        chk("Market hours", market_open(now), "NSE is open" if market_open(now) else "NSE is closed (Mon-Fri 09:15-15:30 IST)"),
        chk("Symbol allowed", req["symbol"] in lim["allowed_symbols"], f"Allowed for you: {', '.join(lim['allowed_symbols'])}"),
        chk("Order type", req["side"] in SIDES and req["product"] in PRODUCTS, "Limit order, day validity"),
        chk("Position", intent != "flip", f"Your net position in this contract is {net}" if intent != "flip" else
            f"This order would take you from {net} through zero. Exit the position first, then place the new order."),
    ]
    tick = contract.tick_size or 0.05
    on_tick = abs(price / tick - round(price / tick)) < 1e-6
    out.append(chk("Price tick", price > 0 and on_tick, f"Price must be a multiple of {tick:g}"))
    if ltp is None:
        out.append(chk("Price near market", False, "Could not read the last traded price, so the limit price cannot be checked"))
    else:
        dev = abs(price - ltp) / ltp
        out.append(chk("Price near market", dev <= MAX_PRICE_DEVIATION, f"Last traded price {ltp:g}; your price is {dev * 100:.1f}% away (limit {MAX_PRICE_DEVIATION * 100:.0f}%)"))
    out.append(chk("Orders today", orders_today < MAX_ORDERS_PER_DAY, f"{orders_today} of {MAX_ORDERS_PER_DAY} orders used today"))
    if intent == "entry":
        after = open_lots(positions, req["symbol"], contract.lot_size) + lots
        out.append(chk("Lots per order", lots <= lim["max_lots"], f"Your limit is {lim['max_lots']} lot(s) per order"))
        out.append(chk("Total open lots", after <= lim["max_lots"], f"Open lots after this order: {after:g}; your limit is {lim['max_lots']}"))
        try:
            pnl = float(funds.get("m2mrealized") or 0) + float(funds.get("m2munrealized") or 0)
        except (TypeError, ValueError):
            pnl = None
        cap = lim["max_daily_loss"]
        if cap is None:
            out.append(chk("Daily loss", True, "No daily loss cap set"))
        elif pnl is None:
            out.append(chk("Daily loss", False, "Could not read today's profit and loss"))
        else:
            out.append(chk("Daily loss", pnl > -cap, f"Today {pnl:,.0f}; entries stop at a loss of {cap:,.0f}"))
        if margin_required is None or cash is None:
            out.append(chk("Margin", False, "Margin could not be computed, so the order is not sent"))
        else:
            out.append(chk("Margin", margin_required <= cash, f"Needed {margin_required:,.0f}; available {cash:,.0f}"))
    else:
        out.append(chk("Exit", True, "Reduces your open position: loss cap and margin do not apply to an exit"))
    return out, intent


def all_ok(checks: list[dict]) -> bool:
    return all(c["ok"] for c in checks)


def parse(body: dict) -> dict:
    try:
        req = {"symbol": str(body["symbol"]).upper(), "expiry": str(body["expiry"]), "strike": float(body["strike"]), "opt": str(body["opt"]).upper(),
               "side": str(body["side"]).upper(), "lots": int(body["lots"]), "price": round(float(body["price"]), 2), "product": str(body.get("product") or "CARRYFORWARD").upper()}
    except (KeyError, TypeError, ValueError) as exc:
        raise OrderError(400, "BAD_ORDER", "The order is incomplete or has a number that cannot be read.") from exc
    if req["opt"] not in ("CE", "PE") or req["side"] not in SIDES or req["product"] not in PRODUCTS or req["lots"] < 1 or req["price"] <= 0:
        raise OrderError(400, "BAD_ORDER", "Check side (BUY/SELL), option (CE/PE), lots (1 or more), price and product.")
    return req


async def _session(c, user_id: str, fresh: bool = False) -> ao.AngelOneSession:
    """The person's session of today. `fresh`: log in now (Test connection, the preflight check) and keep that one for everybody."""
    today = now_ist().date()
    hit = _sessions.get(user_id)
    if hit and not fresh and hit[0] == today:
        return hit[1]
    lock = _session_locks.setdefault(user_id, asyncio.Lock())
    async with lock:                                    # two callers at once (the executor and a screen) make ONE login, not two
        hit = _sessions.get(user_id)
        if hit and not fresh and hit[0] == today:
            return hit[1]
        s = await account._login_or_record(c, user_id)
        _sessions[user_id] = (today, s)
        return s


def _drop(user_id: str) -> None:
    _sessions.pop(user_id, None)


def drop_if_dead(user_id: str, exc: BaseException, session: ao.AngelOneSession | None = None) -> bool:
    """Forget the cached session only when Angel One said it is no longer valid; the next call then logs in once. A rate-limit page or a network
    error keeps it (a new login lifts neither). `session`: the one that failed; a newer one somebody already made is kept."""
    if not ao.session_dead(exc):
        return False
    hit = _sessions.get(user_id)
    if hit and (session is None or hit[1] is session):
        _sessions.pop(user_id, None)
    return True


async def _context(c, user_id: str, req: dict):
    """Everything the checks need, read fresh from Angel One and the instrument list."""
    r = store.row(c, user_id)
    if r is None:
        raise OrderError(404, "NOT_SET", "Connect your Angel One account first.")
    info = await asyncio.to_thread(master.load_underlying, req["symbol"]) if req["symbol"] in store.SYMBOLS else None
    book = {(k.expiry.isoformat(), round(k.strike, 2), k.option_type): k for k in info.contracts} if info else {}
    contract = book.get((req["expiry"], round(req["strike"], 2), req["opt"]))
    if contract is None:
        raise OrderError(400, "NOT_LISTED", "That contract is not listed today (check symbol, expiry, strike and CE/PE).")
    try:
        s = await _session(c, user_id)
        positions, funds = await ao.get_positions(s), await ao.get_funds(s)
        ltp = await ao.get_ltp(s, "NFO", contract.symbol, contract.token)
        qty = req["lots"] * contract.lot_size
        net = position_of(positions, contract.symbol)
        margin = None
        if classify(req["side"], qty, net) == "entry":
            if req["side"] == "BUY":
                margin = req["price"] * qty
            else:
                try:
                    d = await ao.get_margin(s, [{"exchange": "NFO", "qty": qty, "price": 0, "productType": req["product"], "token": contract.token, "tradeType": "SELL", "orderType": "MARKET"}])
                    margin = d.get("totalMarginRequired")
                except ao.AngelOneApiError:
                    margin = None                      # the Margin check then fails (the order is not sent) but the person still sees the rest of the preview
    except ao.AngelOneApiError as exc:
        drop_if_dead(user_id, exc)
        raise OrderError(502, "BROKER_READ", f"Could not read your Angel One account: {exc}") from exc
    return r, contract, ltp, positions, funds, (float(margin) if margin is not None else None), s


def orders_today(c, user_id: str) -> int:
    start = now_ist().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return c.execute("SELECT COUNT(*) FROM broker_orders WHERE user_id=? AND created_at>=? AND status IN ('sending','placed','unknown')", (user_id, start)).fetchone()[0]


def view(r) -> dict:
    return {"id": r["id"], "symbol": r["symbol"], "expiry": r["expiry"], "strike": r["strike"], "opt": r["opt"], "side": r["side"], "lots": r["lots"], "qty": r["qty"],
            "price": r["price"], "product": r["product"], "intent": r["intent"], "trading_symbol": r["trading_symbol"], "status": r["status"],
            "broker_order_id": r["broker_order_id"], "message": r["message"], "created_at": r["created_at"], "updated_at": r["updated_at"],
            "checks": json.loads(r["checks_json"]) if r["checks_json"] else []}


async def preview(c, user_id: str, body: dict) -> dict:
    _options_on()
    req = parse(body)
    r, contract, ltp, positions, funds, margin, _ = await _context(c, user_id, req)
    checks, intent = evaluate(row=r, req=req, contract=contract, ltp=ltp, positions=positions, funds=funds, margin_required=margin, orders_today=orders_today(c, user_id), now=now_ist())
    pid, now = uuid.uuid4().hex, time.time()
    c.execute("INSERT INTO broker_orders (id, user_id, symbol, expiry, strike, opt, side, lots, qty, price, product, intent, trading_symbol, status, checks_json, created_at, updated_at) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'previewed', ?, ?, ?)",
              (pid, user_id, req["symbol"], req["expiry"], req["strike"], req["opt"], req["side"], req["lots"], req["lots"] * contract.lot_size, req["price"], req["product"],
               intent, contract.symbol, json.dumps(checks), now, now))
    c.commit()
    return {**view(c.execute("SELECT * FROM broker_orders WHERE id=?", (pid,)).fetchone()), "ok": all_ok(checks), "expires_in": int(PREVIEW_TTL_S), "ltp": ltp,
            "margin_required": margin, "lot_size": contract.lot_size}


def _finish(c, pid: str, status: str, message: str | None, broker_id: str | None = None) -> None:
    c.execute("UPDATE broker_orders SET status=?, message=?, broker_order_id=COALESCE(?, broker_order_id), updated_at=? WHERE id=?", (status, (message or "")[:300] or None, broker_id, time.time(), pid))
    c.commit()


async def send(c, user_id: str, preview_id: str, actor: dict | None) -> dict:
    _options_on()
    row = c.execute("SELECT * FROM broker_orders WHERE id=? AND user_id=?", (preview_id, user_id)).fetchone()
    if row is None:
        raise OrderError(404, "NO_PREVIEW", "No such preview.")
    if row["status"] != "previewed":
        raise OrderError(409, "ALREADY_USED", f"This preview was already used (status: {row['status']}). It cannot be sent again.")
    if time.time() - row["created_at"] > PREVIEW_TTL_S:
        _finish(c, preview_id, "expired", "Preview expired before it was sent")
        raise OrderError(409, "EXPIRED", "This preview is older than two minutes. Preview the order again.")
    gap = time.monotonic() - _last_send.get(user_id, -1e9)
    if gap < MIN_GAP_S:
        raise OrderError(429, "TOO_FAST", "Wait a couple of seconds between orders.")
    req = {"symbol": row["symbol"], "expiry": row["expiry"], "strike": row["strike"], "opt": row["opt"], "side": row["side"], "lots": row["lots"], "price": row["price"], "product": row["product"]}
    r, contract, ltp, positions, funds, margin, s = await _context(c, user_id, req)                 # everything again, at the moment of sending
    checks, intent = evaluate(row=r, req=req, contract=contract, ltp=ltp, positions=positions, funds=funds, margin_required=margin, orders_today=orders_today(c, user_id), now=now_ist())
    if not all_ok(checks):
        c.execute("UPDATE broker_orders SET checks_json=?, updated_at=? WHERE id=?", (json.dumps(checks), time.time(), preview_id))
        c.commit()
        bad = next(x for x in checks if not x["ok"])
        raise OrderError(409, "CHECK_FAILED", f"Not sent: {bad['name']}. {bad['detail']}", checks)
    claimed = c.execute("UPDATE broker_orders SET status='sending', checks_json=?, updated_at=? WHERE id=? AND status='previewed'", (json.dumps(checks), time.time(), preview_id)).rowcount
    c.commit()
    if claimed != 1:
        raise OrderError(409, "ALREADY_USED", "This preview is being sent or was already sent.")
    _last_send[user_id] = time.monotonic()
    try:
        oid = await angel_orders.place_limit(s, trading_symbol=contract.symbol, token=contract.token, side=row["side"], qty=row["qty"], price=row["price"], product=row["product"])
        status, message, bid = "placed", "Sent to Angel One", oid
    except ao.AngelOneApiError as exc:
        status, message, bid = "rejected", f"Angel One refused: {exc}", None
        drop_if_dead(user_id, exc)
    except angel_orders.Unknown as exc:
        status, message, bid = "unknown", f"{exc} Check your Angel One order book before doing anything else.", None
    _finish(c, preview_id, status, message, bid)
    audit.append(c, "broker.order", actor_id=user_id, actor_role=actor["role"] if actor else None, target_type="user", target_id=user_id,
                 detail={"order": preview_id, "contract": contract.symbol, "side": row["side"], "lots": row["lots"], "price": row["price"], "intent": intent, "status": status, "broker_order_id": bid})
    out = view(c.execute("SELECT * FROM broker_orders WHERE id=?", (preview_id,)).fetchone())
    if status != "placed":
        raise OrderError(502 if status == "rejected" else 504, "NOT_PLACED" if status == "rejected" else "UNKNOWN", message, out["checks"])
    return out


async def cancel(c, user_id: str, order_id: str, actor: dict | None) -> dict:
    """Cancel an order we placed. Allowed in any mode (it only reduces risk), even with the master switch off."""
    row = c.execute("SELECT * FROM broker_orders WHERE id=? AND user_id=?", (order_id, user_id)).fetchone()
    if row is None:
        raise OrderError(404, "NO_ORDER", "No such order.")
    if row["status"] != "placed" or not row["broker_order_id"]:
        raise OrderError(409, "NOT_CANCELLABLE", f"Only a placed order can be cancelled (status: {row['status']}).")
    try:
        s = await _session(c, user_id)
        await angel_orders.cancel(s, row["broker_order_id"])
    except ao.AngelOneApiError as exc:
        raise OrderError(502, "CANCEL_REFUSED", f"Angel One did not cancel it: {exc} (it may already be filled)") from exc
    except angel_orders.Unknown as exc:
        raise OrderError(504, "UNKNOWN", f"{exc} Check your Angel One order book.") from exc
    _finish(c, order_id, "cancelled", "Cancelled by you")
    audit.append(c, "broker.order.cancel", actor_id=user_id, actor_role=actor["role"] if actor else None, target_type="user", target_id=user_id, detail={"order": order_id, "broker_order_id": row["broker_order_id"]})
    return view(c.execute("SELECT * FROM broker_orders WHERE id=?", (order_id,)).fetchone())


def listing(c, user_id: str | None, limit: int = 50) -> list[dict]:
    q = "SELECT * FROM broker_orders WHERE status!='previewed' OR created_at>? " if user_id is None else "SELECT * FROM broker_orders WHERE user_id=? AND (status!='previewed' OR created_at>?) "
    args: tuple = (time.time() - PREVIEW_TTL_S,) if user_id is None else (user_id, time.time() - PREVIEW_TTL_S)
    return [view(r) for r in c.execute(q + "ORDER BY created_at DESC LIMIT ?", (*args, max(1, min(limit, 200))))]
