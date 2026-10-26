"""Dry-run orders: for every live journal event, the Angel One SmartAPI orders a real account WOULD send, written to a file and never sent.

Reversal method B (chosen by the owner): the exchange holds a stop order for the open quantity only; when it fills the position is closed,
and only then the opposite entry is sent as a LIMIT order with a small allowance over the last price, followed by its own stop order.
Each record keeps the price the strategy used and the live price at that moment, so the slippage a real account would see can be measured.
Nothing here imports or calls the broker."""
from __future__ import annotations

import json
from pathlib import Path

DRYRUN_PATH = Path("data/paper_trading/dryrun_orders.jsonl")
ENTRY_ALLOWANCE_PCT = 0.0005    # entry LIMIT price: up to 0.05% worse than the last price (about 1-2 points on a stock future)
STOP_LIMIT_PCT = 0.002          # a stop order's LIMIT price: 0.2% past its trigger, so a fast move still fills
TAG = "ALGO-DRY"
# exits made by the stop order already resting on the exchange; the others (gap exit, roll) need an exit order from the app
STOP_EXITS = {"TRAILING_STOP_REVERSAL", "BREAKEVEN_EXIT_AFTER_2_PERCENT_PROFIT", "ENTRY_DAY_STOP_EXIT", "CAPPED_1_PERCENT_STOP_EXIT"}


def _side(direction: str, closing: bool) -> str:
    return ("SELL" if direction == "BUY" else "BUY") if closing else direction


def _r(x: float | None) -> str | None:
    return None if x is None else f"{round(x / 0.05) * 0.05:.2f}"      # NSE tick size


def _payload(contract: dict, side: str, qty: int, ordertype: str, price: float | None = None, trigger: float | None = None) -> dict:
    p = {"variety": "STOPLOSS" if trigger is not None else "NORMAL", "tradingsymbol": contract["trading_symbol"], "symboltoken": contract["token"],
         "transactiontype": side, "exchange": contract.get("exch_seg", "NFO"), "ordertype": ordertype, "producttype": "CARRYFORWARD",
         "duration": "DAY", "quantity": str(qty), "ordertag": TAG}
    if price is not None:
        p["price"] = _r(price)
    if trigger is not None:
        p["triggerprice"] = _r(trigger)
    return p


def _stop_order(contract: dict, direction: str, qty: int, stop: float) -> dict:
    side = _side(direction, closing=True)
    limit = stop * (1 - STOP_LIMIT_PCT) if side == "SELL" else stop * (1 + STOP_LIMIT_PCT)
    return _payload(contract, side, qty, "STOPLOSS_LIMIT", price=limit, trigger=stop)


def orders_for(event: dict, contract: dict, ltp: float | None) -> list[dict]:
    """The SmartAPI calls a real account would make for one journal event (in order)."""
    d, qty, ev = event["direction"], int(event["quantity"]), event["event"]
    out = []
    if ev == "ENTRY":
        ref = ltp if ltp else event["entry_price"]
        limit = ref * (1 + ENTRY_ALLOWANCE_PCT) if d == "BUY" else ref * (1 - ENTRY_ALLOWANCE_PCT)
        out.append({"call": "placeOrder", "why": "entry", "payload": _payload(contract, d, qty, "LIMIT", price=limit)})
        if event.get("active_stop") is not None:
            out.append({"call": "placeOrder", "why": "stop-loss for the new position (sent after the entry fills)",
                        "payload": _stop_order(contract, d, qty, event["active_stop"])})
    elif ev == "EXIT":
        if event.get("reason") in STOP_EXITS:
            out.append({"call": "none", "why": f"the stop order already on the exchange filled ({event.get('reason')}); its fill closes the position"})
        else:
            ref = ltp if ltp else event["exit_price"]
            side = _side(d, closing=True)
            limit = ref * (1 + ENTRY_ALLOWANCE_PCT) if side == "BUY" else ref * (1 - ENTRY_ALLOWANCE_PCT)
            out.append({"call": "cancelOrder", "why": "remove the open stop order first"})
            out.append({"call": "placeOrder", "why": f"exit ({event.get('reason')})", "payload": _payload(contract, side, qty, "LIMIT", price=limit)})
    elif ev in ("SL_TO_BREAKEVEN", "GAP"):
        stop = event.get("active_stop") if ev == "SL_TO_BREAKEVEN" else event.get("protective_sl")
        if stop is not None:
            out.append({"call": "modifyOrder", "why": "move the stop" if ev == "SL_TO_BREAKEVEN" else f"gap protection ({event.get('gap_state')})",
                        "payload": _stop_order(contract, d, qty, stop)})
    return out


def record(events: list[dict], snap: dict) -> None:
    """Write the would-be orders of these journal events. History found on an instrument's first pass (backfill) is skipped:
    a real account would not have sent those orders. Any failure here must never affect the paper engine."""
    live = [e for e in events if not e.get("backfill")]
    if not live:
        return
    contract = snap.get("instrument") or {}
    ltp = snap.get("ltp")
    DRYRUN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DRYRUN_PATH.open("a", encoding="utf-8") as f:
        for e in live:
            price = e.get("exit_price") if e["event"] == "EXIT" else e.get("entry_price") if e["event"] == "ENTRY" else None
            slip = None
            if price is not None and ltp:
                worse = (ltp - price) if _side(e["direction"], e["event"] == "EXIT") == "BUY" else (price - ltp)
                slip = round(worse, 2)
            f.write(json.dumps({"at": e["at"], "owner": e.get("owner"), "instrument_id": e.get("instrument_id"), "symbol": e["symbol"],
                                "contract": contract.get("trading_symbol"), "event": e["event"], "direction": e["direction"], "quantity": e["quantity"],
                                "strategy_price": price, "ltp_at_signal": ltp, "slippage_points": slip,
                                "orders": orders_for(e, contract, ltp)}) + "\n")


def read(owner, limit: int = 300) -> list[dict]:
    if not DRYRUN_PATH.exists():
        return []
    rows = [json.loads(x) for x in DRYRUN_PATH.read_text(encoding="utf-8").splitlines() if x.strip()]
    return list(reversed([r for r in rows if r.get("owner") == owner][-limit:]))
