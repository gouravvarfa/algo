"""Dry-run orders: for every live journal event, the Angel One SmartAPI orders a real account WOULD send, written to a file and never sent.

Reversal method B (chosen by the owner): the exchange holds a stop order for the open quantity only; when it fills the position is closed,
and only then the opposite entry is sent as a LIMIT order with a small allowance (in POINTS) over the last price, followed by its own stop order.
Each record keeps the price the strategy used and the live price at that moment, so the slippage a real account would see can be measured; a signal
noticed late, or priced from a feed that was not live, is flagged and kept out of the slippage statistics.
(The realistic fill simulation, with its own position and P&L, is app/paper_trading/shadow.py.)
Nothing here imports or calls the broker."""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

DRYRUN_PATH = Path("data/paper_trading/dryrun_orders.jsonl")
CONTRACTS_PATH = Path("data/paper_trading/contracts.json")      # every futures contract seen per symbol, with its token (an expired one is no longer in Angel One's master)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TAG = "ALGO-DRY"
DEFAULT_TICK = 0.05                 # used only when the instrument master did not give the contract's tick size; the row says so (tick_size_assumed)

# Allowances are in POINTS, per symbol: a percentage is ~0.3 point on a Rs 668 stock but ~27 points on BankNifty.
ENTRY_BUFFER_POINTS = {"BANKNIFTY": 2.0, "NIFTY": 2.0}
DEFAULT_ENTRY_BUFFER_POINTS = 1.0   # stock futures
# A stop order's LIMIT price sits this many points past its trigger so a fast move still fills.
# VERIFY WITH ANGEL ONE: the exchange limits how far the limit may be from the trigger (a forum report shows error AB1007); these values are cautious guesses.
STOP_LIMIT_POINTS = {"BANKNIFTY": 10.0, "NIFTY": 5.0}
DEFAULT_STOP_LIMIT_POINTS = 1.0

LATE_AFTER_SECONDS = 120            # a signal noticed more than this after its candle closed is "late"
# exits made by the stop order already resting on the exchange; the others (gap exit, roll) need an exit order from the app
STOP_EXITS = {"TRAILING_STOP_REVERSAL", "STOP_LOSS_REVERSAL", "BREAKEVEN_EXIT_AFTER_2_PERCENT_PROFIT", "ENTRY_DAY_STOP_EXIT", "CAPPED_1_PERCENT_STOP_EXIT"}
NO_ORDER_EXITS = {"END_OF_BACKTEST"}    # the position is only being marked, nothing was closed


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST).replace(tzinfo=None)


def entry_buffer(symbol: str) -> float:
    return ENTRY_BUFFER_POINTS.get(symbol, DEFAULT_ENTRY_BUFFER_POINTS)


def stop_limit_points(symbol: str) -> float:
    return STOP_LIMIT_POINTS.get(symbol, DEFAULT_STOP_LIMIT_POINTS)


def rnd(x: float, tick: float, mode: str = "near") -> float:
    """Round to the price grid: 'up' / 'down' lean toward the more aggressive side of a limit, 'near' is the nearest tick."""
    q = x / tick
    n = math.ceil(q - 1e-9) if mode == "up" else math.floor(q + 1e-9) if mode == "down" else round(q)
    return round(n * tick, 2)


def _side(direction: str, closing: bool) -> str:
    return ("SELL" if direction == "BUY" else "BUY") if closing else direction


def _s(x: float | None) -> str | None:
    return None if x is None else f"{x:.2f}"


def resolve_contract(symbol: str, trading_symbol: str, path: Path | None = None) -> dict | None:
    """A contract's token from the remembered list (offline; never guessed)."""
    try:
        known = json.loads((path or CONTRACTS_PATH).read_text())
    except (OSError, ValueError):
        return None
    for c in known.get(symbol, []):
        if c.get("trading_symbol") == trading_symbol:
            return c
    return None


def event_contract(event: dict, snap: dict) -> tuple[dict, bool]:
    """The contract this event belongs to: the trade's own contract (a roll exit closes the OLD contract), else the active one.
    Second value True = the contract could not be resolved (its token is unknown, so the orders carry no token)."""
    active = snap.get("instrument") or {}
    name = event.get("contract")
    if not name or name == active.get("trading_symbol"):
        return active, False
    found = resolve_contract(event.get("symbol", ""), name)
    if found:
        return found, False
    return {"trading_symbol": name, "token": None, "exch_seg": "NFO"}, True


def _tick(contract: dict) -> tuple[float, bool]:
    t = contract.get("tick_size") or 0
    return (float(t), False) if t and float(t) > 0 else (DEFAULT_TICK, True)


def _payload(contract: dict, side: str, qty: int, ordertype: str, price: float | None = None, trigger: float | None = None) -> dict:
    p = {"variety": "STOPLOSS" if trigger is not None else "NORMAL", "tradingsymbol": contract.get("trading_symbol"), "symboltoken": contract.get("token"),
         "transactiontype": side, "exchange": contract.get("exch_seg", "NFO"), "ordertype": ordertype, "producttype": "CARRYFORWARD",
         "duration": "DAY", "quantity": str(qty), "ordertag": TAG}
    if price is not None:
        p["price"] = _s(price)
    if trigger is not None:
        p["triggerprice"] = _s(trigger)
    return p


def _limit(contract: dict, symbol: str, side: str, ref: float) -> float:
    """An entry / exit LIMIT price: the last price plus the allowance in points, on the aggressive side, on the price grid."""
    tick, _ = _tick(contract)
    buf = entry_buffer(symbol)
    return rnd(ref + buf, tick, "up") if side == "BUY" else rnd(ref - buf, tick, "down")


def _stop_order(contract: dict, symbol: str, direction: str, qty: int, stop: float) -> dict:
    tick, _ = _tick(contract)
    side = _side(direction, closing=True)
    trigger = rnd(stop, tick)
    limit = rnd(trigger - stop_limit_points(symbol), tick, "down") if side == "SELL" else rnd(trigger + stop_limit_points(symbol), tick, "up")
    return _payload(contract, side, qty, "STOPLOSS_LIMIT", price=limit, trigger=trigger)


def orders_for(event: dict, contract: dict, ltp: float | None) -> list[dict]:
    """The SmartAPI calls a real account would make for one journal event (in order)."""
    d, qty, ev, sym = event["direction"], int(event["quantity"]), event["event"], event.get("symbol", "")
    out = []
    if ev == "ENTRY":
        ref = ltp if ltp else event["entry_price"]
        out.append({"call": "placeOrder", "why": "entry", "payload": _payload(contract, d, qty, "LIMIT", price=_limit(contract, sym, d, ref))})
        if event.get("active_stop") is not None:
            out.append({"call": "placeOrder", "why": "stop-loss for the new position (sent after the entry fills)",
                        "payload": _stop_order(contract, sym, d, qty, event["active_stop"])})
    elif ev == "EXIT":
        reason = event.get("reason")
        if reason in NO_ORDER_EXITS:
            out.append({"call": "none", "why": f"{reason}: the position is only being marked, nothing was closed"})
        elif reason in STOP_EXITS:
            out.append({"call": "none", "why": f"the stop order already on the exchange filled ({reason}); its fill closes the position"})
        else:
            ref = ltp if ltp else event["exit_price"]
            side = _side(d, closing=True)
            out.append({"call": "cancelOrder", "why": "remove the open stop order first"})
            out.append({"call": "placeOrder", "why": f"exit ({reason})", "payload": _payload(contract, side, qty, "LIMIT", price=_limit(contract, sym, side, ref))})
    elif ev in ("SL_TO_BREAKEVEN", "GAP"):
        stop = event.get("active_stop") if ev == "SL_TO_BREAKEVEN" else event.get("protective_sl")
        if stop is not None:
            out.append({"call": "modifyOrder", "why": "move the stop" if ev == "SL_TO_BREAKEVEN" else f"gap protection ({event.get('gap_state')})",
                        "payload": _stop_order(contract, sym, d, qty, stop)})
    return out


def _minute(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def record(events: list[dict], snap: dict, now: dt.datetime | None = None) -> None:
    """Write the would-be orders of these journal events. History found on an instrument's first pass (backfill) is skipped:
    a real account would not have sent those orders. Any failure here must never affect the paper engine."""
    live = [e for e in events if not e.get("backfill")]
    if not live:
        return
    now = now or now_ist()
    ltp = snap.get("ltp")
    feed = snap.get("feed") or {}
    feed_live = feed.get("live")
    DRYRUN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DRYRUN_PATH.open("a", encoding="utf-8") as f:
        for e in live:
            contract, unresolved = event_contract(e, snap)
            price = e.get("exit_price") if e["event"] == "EXIT" else e.get("entry_price") if e["event"] == "ENTRY" else None
            slip = None
            if price is not None and ltp:
                worse = (ltp - price) if _side(e["direction"], e["event"] == "EXIT") == "BUY" else (price - ltp)
                slip = round(worse, 2)
            sig = _minute(e.get("exit_date") if e["event"] == "EXIT" else e.get("entry_date") if e["event"] == "ENTRY" else None)
            delay = None if sig is None else max(0.0, round((now - (sig + dt.timedelta(minutes=1))).total_seconds(), 1))     # from the end of the signal's minute candle
            late = delay is not None and delay > LATE_AFTER_SECONDS
            f.write(json.dumps({"at": e["at"], "owner": e.get("owner"), "instrument_id": e.get("instrument_id"), "symbol": e["symbol"],
                                "contract": contract.get("trading_symbol"), "event": e["event"], "direction": e["direction"], "quantity": e["quantity"],
                                "strategy_price": price, "ltp_at_signal": ltp, "slippage_points": slip,
                                "signal_time": sig.isoformat(sep=" ") if sig else None, "detected_at": now.isoformat(sep=" ", timespec="seconds"),
                                "delay_seconds": delay, "late": late, "feed_live": feed_live, "ltp_age_s": feed.get("age_s"),
                                "slippage_valid": bool(slip is not None and not late and feed_live is True),
                                "contract_unresolved": unresolved, "tick_size_assumed": _tick(contract)[1],
                                "orders": orders_for(e, contract, ltp)}) + "\n")


def read(owner, limit: int = 300) -> list[dict]:
    if not DRYRUN_PATH.exists():
        return []
    rows = [json.loads(x) for x in DRYRUN_PATH.read_text(encoding="utf-8").splitlines() if x.strip()]
    return list(reversed([r for r in rows if r.get("owner") == owner][-limit:]))
