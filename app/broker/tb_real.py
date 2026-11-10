"""Real Angel One orders for the Top-Bottom strategy: the shadow account's lifecycle (app/paper_trading/shadow.py), but with real orders and real fills.

What it does, for each person and instrument whose real switch is ON (and the account in effective real mode):
  * Starts at the NEXT new System Point after the switch was turned on. A position the paper account already holds then is not copied; its exit finds no real position and is ignored.
  * ENTRY: a LIMIT order near the live price (never beyond a small band from the strategy's price: a missed entry is never chased), then, once it fills, a STOPLOSS_LIMIT order
    for exactly that quantity sits on the exchange. An entry order that is not filled in 30 s is cancelled and placed again at the live price, three attempts at most.
  * EXIT, method B: the stop order closes the position; only then does the opposite entry go out (a reversal never holds two quantities). If the strategy has exited and our stop has
    not filled within 30 s, the stop is cancelled and an exit LIMIT order is sent. Gap exits and rolls send the exit order at once.
  * The stop follows the strategy's stop (break-even, gap protection) by modifying the resting stop order.
  * Before every ENTRY: lots within the person's limit, today's loss under the cap, margin available (fail closed), market hours of the instrument's session, at most 40 orders a day.
  * Anything it cannot be sure of HALTS that instrument (a request that timed out, a broker position that does not match what we hold): no further order is sent until the person
    clears the halt after looking at Angel One. Nothing is retried blind.
  * Switching the instrument off, or the account leaving real mode, cancels an entry that has not filled. An open position keeps its resting stop order and is then the person's to manage.
Every order is stored (real_tb_orders) and audited. The state survives a restart. Orders are never market orders.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
import uuid

from app.broker import angel_orders, store
from app.db import audit, platform_db
from app.paper_trading import dryrun
from app.providers import angelone_client as ao

log = logging.getLogger("real_tb")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
RETRY_AFTER_S = 30.0
MAX_ATTEMPTS = 3
STOP_GRACE_S = 30.0
STALE_SIGNAL_S = 900.0
NO_PRICE_MISS_S = 120.0
ENTRY_BAND_PCT = 0.005
MISMATCH_HALT_S = 20.0
IDLE_POLL_S = 60.0                 # a released position (switch off) or a closed market: look at Angel One once a minute, not every 5 s
REAL_TICK_FALLBACK = 1.0           # price grid when the contract's tick is unknown: Rs 1 is a multiple of every NSE / MCX futures tick (0.05, 0.10, 0.20, 1), so the price is always valid
EXCHANGES = ("NFO", "MCX")        # MCX quantity = lots x the scrip master's lotsize (its own unit: GOLD 1 = 1 kg lot, GOLDM 100 g, SILVER 30 kg), checked Nov 2026
MAX_ORDERS_PER_DAY = 40
TICK_S = 5.0
KEEP_EVENTS = 80
ACTIVE = ("placed", "open")


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST).replace(tzinfo=None)


def _s(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _p(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def _age(s: str | None, now: dt.datetime) -> float:
    return (now - _p(s)).total_seconds() if s else 0.0


class AngelBroker:
    """The calls the executor needs, against one person's Angel One session."""

    def __init__(self, session: ao.AngelOneSession):
        self.s = session

    async def place(self, payload):
        return await angel_orders.place_payload(self.s, payload)

    async def modify(self, order_id, payload):
        await angel_orders.modify_payload(self.s, order_id, payload)

    async def cancel(self, order_id):
        await angel_orders.cancel(self.s, order_id)

    async def book(self):
        return await ao.get_order_book(self.s)

    async def positions(self):
        return await ao.get_positions(self.s)

    async def funds(self):
        return await ao.get_funds(self.s)

    async def ltp(self, exchange, symbol, token):
        return await ao.get_ltp(self.s, exchange, symbol, token)

    async def margin(self, basket):
        return await ao.get_margin(self.s, basket)


async def angel_broker_for(c, user_id: str) -> AngelBroker:
    from app.broker import orders
    return AngelBroker(await orders._session(c, user_id))


class RealTB:
    def __init__(self, broker_for=angel_broker_for) -> None:
        self.broker_for = broker_for
        self.state: dict[tuple[str, str], dict] = {}
        self.snaps: dict[str, dict] = {}
        self.queue: list[dict] = []
        self.lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ what the paper engine tells us (cheap, synchronous, never raises into it)
    def handle(self, events: list[dict], snap: dict, now: dt.datetime) -> None:
        self.queue.extend(e for e in events if not e.get("backfill"))

    def sync(self, inst_id: str, owner, symbol: str, snap: dict, now: dt.datetime) -> None:
        pp = snap.get("position")
        self.snaps[inst_id] = {"owner": owner, "symbol": symbol, "instrument": snap.get("instrument") or {}, "ltp": snap.get("ltp"), "snap_ltp_at": _s(now),
                               "paper": {"direction": pp["direction"], "entry_price": pp["entry_price"], "active_stop": pp["active_stop"]} if pp else None}

    # ------------------------------------------------------------------ state
    def _acct(self, c, user_id: str, inst_id: str, symbol: str, since: float) -> dict:
        a = self.state.get((user_id, inst_id))
        if a is None:
            r = c.execute("SELECT state_json FROM real_tb_state WHERE user_id=? AND instrument_id=?", (user_id, inst_id)).fetchone()
            a = json.loads(r["state_json"]) if r else {"user_id": user_id, "instrument_id": inst_id, "symbol": symbol, "since": since, "position": None, "pending_entry": None,
                                                       "orders": [], "stop_target": None, "exit_since": None, "halted": None, "missed": [], "events": [], "released": False}
            self.state[(user_id, inst_id)] = a
        if a["since"] != since and not a["position"] and not a["pending_entry"]:
            a["since"] = since                                 # switched off and on again: a fresh start
            a["released"] = False
        return a

    def _save(self, c, a: dict) -> None:
        del a["orders"][:-60]
        c.execute("INSERT INTO real_tb_state (user_id, instrument_id, state_json, updated_at) VALUES (?,?,?,?) ON CONFLICT(user_id, instrument_id) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
                  (a["user_id"], a["instrument_id"], json.dumps(a), time.time()))
        c.commit()

    def _ev(self, a: dict, now: dt.datetime, text: str, **kv) -> None:
        a["events"].append({"at": _s(now), "text": text, **kv})
        del a["events"][:-KEEP_EVENTS]
        log.info("REAL %s %s: %s", a["symbol"], a["user_id"][:6], text)

    def _halt(self, c, a: dict, now: dt.datetime, why: str) -> None:
        a["halted"] = why
        self._ev(a, now, f"HALTED: {why}")
        audit.append(c, "broker.tb.halt", actor_id=None, actor_role="system", target_type="user", target_id=a["user_id"], detail={"instrument": a["symbol"], "why": why[:200]})

    # ------------------------------------------------------------------ orders
    def _active(self, a: dict, role: str) -> list[dict]:
        return [o for o in a["orders"] if o["role"] == role and o["state"] in ACTIVE]

    def _orders_today(self, c, user_id: str) -> int:
        start = dt.datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return c.execute("SELECT COUNT(*) FROM real_tb_orders WHERE user_id=? AND created_at>=? AND status IN ('placed','open','filled','unknown')", (user_id, start)).fetchone()[0]

    async def _place(self, c, broker, a: dict, now: dt.datetime, role: str, payload: dict, why: str, contract: dict, attempt: int = 0) -> dict | None:
        if role == "entry" and self._orders_today(c, a["user_id"]) >= MAX_ORDERS_PER_DAY:          # a stop or an exit is never held back
            self._ev(a, now, f"{role} order not sent: {MAX_ORDERS_PER_DAY} orders already sent today")
            return None
        payload = {**payload, "ordertag": "ALGOTB"}
        oid = uuid.uuid4().hex
        t = time.time()
        o = {"id": oid, "role": role, "side": payload["transactiontype"], "qty": int(payload["quantity"]), "type": payload["ordertype"], "price": float(payload["price"]) if payload.get("price") else None,
             "trigger": float(payload["triggerprice"]) if payload.get("triggerprice") else None, "contract": contract.get("trading_symbol"), "token": contract.get("token"),
             "exch": payload.get("exchange"), "tick": dryrun._tick(contract)[0], "lot_size": contract.get("lot_size"), "why": why, "attempt": attempt, "state": "placed", "broker_id": None,
             "placed_at": _s(now), "filled_avg": None, "message": None}
        status, msg = "placed", None
        try:
            o["broker_id"] = await broker.place(payload)
        except ao.AngelOneApiError as exc:
            o["state"], status, msg = "rejected", "rejected", f"Angel One refused: {exc}"
            o["message"] = msg
        except angel_orders.Unknown as exc:
            o["state"], status, msg = "unknown", "unknown", str(exc)
            self._halt(c, a, now, f"a {role} order got no clear answer from Angel One ({exc}). Look at your Angel One orders and positions, then clear the halt.")
        a["orders"].append(o)
        c.execute("INSERT INTO real_tb_orders (id, user_id, instrument_id, symbol, contract, role, side, qty, order_type, price, trigger_price, broker_order_id, status, why, message, created_at, updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (oid, a["user_id"], a["instrument_id"], a["symbol"], o["contract"] or "", role, o["side"], o["qty"], o["type"], o["price"], o["trigger"], o["broker_id"], status, why, msg, t, t))
        c.commit()
        audit.append(c, "broker.tb.order", actor_id=None, actor_role="system", target_type="user", target_id=a["user_id"],
                     detail={"order": oid, "instrument": a["symbol"], "role": role, "side": o["side"], "qty": o["qty"], "type": o["type"], "price": o["price"], "trigger": o["trigger"],
                             "status": status, "broker_order_id": o["broker_id"]})
        self._ev(a, now, f"{role} order {status}: {o['side']} {o['qty']} {o['type']} {o['price']}" + (f" trigger {o['trigger']}" if o["trigger"] is not None else "") + f" ({why})" + (f": {msg}" if msg else ""), order=oid)
        return o

    def _mark(self, c, o: dict, a: dict, status: str, msg: str | None = None, avg: float | None = None) -> None:
        o["state"] = "filled" if status == "filled" else status
        if avg is not None:
            o["filled_avg"] = avg
        c.execute("UPDATE real_tb_orders SET status=?, message=COALESCE(?, message), filled_avg=COALESCE(?, filled_avg), updated_at=? WHERE id=?", (status, msg, avg, time.time(), o["id"]))
        c.commit()

    async def _cancel(self, c, broker, a: dict, o: dict, now: dt.datetime, why: str) -> bool:
        if o["state"] not in ACTIVE or not o.get("broker_id"):
            return True
        try:
            await broker.cancel(o["broker_id"])
        except ao.AngelOneApiError as exc:
            self._ev(a, now, f"cancel of {o['role']} order refused ({exc}); it may already have filled", order=o["id"])
            return False
        except angel_orders.Unknown as exc:
            self._halt(c, a, now, f"a cancel got no clear answer from Angel One ({exc}). Look at your Angel One orders, then clear the halt.")
            return False
        self._mark(c, o, a, "cancelled", why)
        self._ev(a, now, f"{o['role']} order cancelled: {why}", order=o["id"])
        return True

    # ------------------------------------------------------------------ the strategy's signals
    async def _on_event(self, c, e: dict, now: dt.datetime) -> None:
        uid, inst_id = e.get("owner"), e.get("instrument_id")
        if not uid or not inst_id:
            return
        row = c.execute("SELECT real_on, changed_at FROM user_instrument_real WHERE user_id=? AND instrument_id=?", (uid, inst_id)).fetchone()
        if row is None or not row["real_on"] or not store.instrument_real(c, uid, inst_id):
            return
        try:
            t = dt.datetime.fromisoformat(e["at"]) if e.get("at") else None
            at = (t if t.tzinfo else t.replace(tzinfo=IST)).timestamp() if t else 0     # the engine writes IST wall time without a zone; the server runs on UTC
        except ValueError:
            at = 0
        a = self._acct(c, uid, inst_id, e["symbol"], row["changed_at"])
        if at and at < row["changed_at"] - 60:                                            # minute precision: same-minute events count as after
            self._ev(a, now, f"{e['event']} {e['direction']} ignored: it happened before the real switch was turned on")
            self._save(c, a)
            return
        if a["halted"]:
            self._ev(a, now, f"{e['event']} {e['direction']} not acted on: this instrument is halted ({a['halted'][:80]})")
            self._save(c, a)
            return
        sig = dryrun._minute(e.get("exit_date") if e["event"] == "EXIT" else e.get("entry_date") if e["event"] == "ENTRY" else None)
        if sig and (now - sig).total_seconds() > STALE_SIGNAL_S:
            self._ev(a, now, f"{e['event']} {e['direction']} ignored: the signal was {int((now - sig).total_seconds() // 60)} min old when it was noticed")
            self._save(c, a)
            return
        snap = {"instrument": (self.snaps.get(inst_id) or {}).get("instrument") or {}}
        if e["event"] == "ENTRY":
            await self._on_entry(c, a, e, snap, now)
        elif e["event"] == "EXIT":
            self._on_exit(c, a, e, now)
        self._save(c, a)

    async def _on_entry(self, c, a: dict, e: dict, snap: dict, now: dt.datetime) -> None:
        contract, unresolved = dryrun.event_contract(e, snap)
        if unresolved or not contract.get("token"):
            self._miss(a, now, "contract token unknown, cannot trade it", e)
            return
        contract = dict(contract)
        if not contract.get("tick_size"):
            known = dryrun.resolve_contract(e["symbol"], contract.get("trading_symbol") or "") or {}
            contract["tick_size"] = known.get("tick_size") or 0
        if not contract.get("tick_size"):
            contract["tick_size"] = REAL_TICK_FALLBACK                                   # BANKNIFTY futures trade on a 0.20 grid since 2026: a guessed 0.10 would be refused
        inst = snap.get("instrument") or {}
        contract["lot_size"] = contract.get("lot_size") or inst.get("lot_size")
        lots = store.instrument_lots(c, a["user_id"], a["instrument_id"])                # the person's chosen lots, not the paper quantity
        contract["lots"] = lots
        contract["session"] = inst.get("session", "NSE")
        lot_size = contract.get("lot_size")
        if not lot_size:
            self._miss(a, now, "lot size of the contract unknown, cannot size the real order", e)
            return
        qty = int(lots) * int(lot_size)
        p = a["position"]
        roll = bool(p and p["side"] == e["direction"] and p["contract"] != contract.get("trading_symbol"))
        if p and p["side"] == e["direction"] and not roll:
            self._ev(a, now, f"ENTRY {e['direction']} ignored: a real position on that side is already open")
            return
        if roll:                                                                  # expiry roll: close the old contract now, then the same side in the new one
            a["exit_since"] = _s(now - dt.timedelta(seconds=STOP_GRACE_S + 1))
        a["pending_entry"] = {"side": e["direction"], "qty": qty, "strategy_price": float(e["entry_price"]), "strategy_stop": e.get("active_stop"), "contract": contract,
                              "created_at": _s(now), "attempts": 0, "signal_time": e.get("entry_date")}
        if p:
            if a["exit_since"] is None:
                a["exit_since"] = _s(now)
            self._ev(a, now, f"strategy reversed to {e['direction']}: the open position closes first (method B), then the entry goes out")
        else:
            self._ev(a, now, f"System Point {e['direction']}: entry will be sent")

    def _on_exit(self, c, a: dict, e: dict, now: dt.datetime) -> None:
        pe, p = a["pending_entry"], a["position"]
        if pe and pe["side"] == e["direction"] and not p:
            a["pending_entry"] = None
            a["_cancel_entry"] = True
            self._ev(a, now, "the strategy closed the trade this entry belonged to: entry dropped")
            return
        if not p or p["side"] != e["direction"]:
            self._ev(a, now, f"EXIT {e['direction']} ({e.get('reason')}): no real position on that side, nothing to do")
            return
        reason = e.get("reason")
        if reason in dryrun.NO_ORDER_EXITS:
            return
        if reason in dryrun.STOP_EXITS:
            if a["exit_since"] is None:
                a["exit_since"] = _s(now)
            self._ev(a, now, f"strategy exit ({reason}): our stop order is expected to fill")
        else:
            a["exit_since"] = _s(now - dt.timedelta(seconds=STOP_GRACE_S + 1))
            self._ev(a, now, f"strategy exit ({reason}): sending an exit LIMIT order now")

    def _miss(self, a: dict, now: dt.datetime, reason: str, e: dict | None = None) -> None:
        pe = a["pending_entry"] or {}
        a["missed"].append({"at": _s(now), "side": (e or pe).get("direction") or pe.get("side"), "strategy_price": (e or {}).get("entry_price") or pe.get("strategy_price"), "reason": reason})
        del a["missed"][:-50]
        a["pending_entry"] = None
        a["_cancel_entry"] = True
        self._ev(a, now, f"ENTRY MISSED: {reason}")

    # ------------------------------------------------------------------ the loop
    async def tick(self, now: dt.datetime | None = None) -> None:
        now = now or now_ist()
        async with self.lock:
            c = platform_db.connect()
            try:
                events, self.queue = self.queue, []
                for e in events:
                    try:
                        await self._on_event(c, e, now)
                    except Exception:
                        log.exception("real order step failed for an event")
                keys = {(r["user_id"], r["instrument_id"]): r["symbol"] for r in c.execute("SELECT user_id, instrument_id, symbol FROM user_instrument_real WHERE real_on=1")}
                for (uid, iid), a in list(self.state.items()):
                    if a["position"] or a["pending_entry"] or self._active(a, "entry") or self._active(a, "stop") or self._active(a, "exit"):
                        keys.setdefault((uid, iid), a["symbol"])
                for (uid, iid), sym in keys.items():
                    try:
                        await self._step(c, uid, iid, sym, now)
                    except Exception:
                        log.exception("real order step failed for %s", sym)
            finally:
                c.close()

    async def _step(self, c, uid: str, iid: str, sym: str, now: dt.datetime) -> None:
        row = c.execute("SELECT real_on, changed_at FROM user_instrument_real WHERE user_id=? AND instrument_id=?", (uid, iid)).fetchone()
        a = self._acct(c, uid, iid, sym, row["changed_at"] if row else 0)
        busy = bool(a["position"] or a["pending_entry"] or any(o["state"] in ACTIVE for o in a["orders"]) or a.get("_cancel_entry"))
        if not busy:
            return                                                        # nothing open, nothing waiting: no call to Angel One at all
        session = (a["pending_entry"] or {}).get("contract", {}).get("session") or (self.snaps.get(iid) or {}).get("instrument", {}).get("session", "NSE")
        quiet = a.get("released") or (not a["pending_entry"] and not a.get("exit_since") and not self._market_open(session, now))
        if quiet and a.get("polled_at") and _age(a["polled_at"], now) < IDLE_POLL_S:
            return
        a["polled_at"] = _s(now)
        try:
            broker = await self.broker_for(c, uid)
            book = {str(o.get("orderid")): o for o in await broker.book()}
        except Exception as exc:
            from app.broker import orders as _orders
            _orders._drop(uid)                                            # the next round logs in afresh
            self._ev(a, now, f"Angel One not reachable this round ({type(exc).__name__}); nothing was sent")
            self._save(c, a)
            return
        await self._update_orders(c, a, book, now)
        real_ok = bool(row and row["real_on"]) and store.instrument_real(c, uid, iid)
        if a.get("manual_exit") and not a["position"]:
            a.pop("manual_exit", None)
        if a.get("manual_exit") and not a["halted"]:                       # the person pressed Exit now: drop any entry, then close with a LIMIT order
            for o in self._active(a, "entry"):
                await self._cancel(c, broker, a, o, now, "exit now was pressed")
            a["pending_entry"], a["released"] = None, False
            await self._reconcile(c, broker, a, now)
            if not a["halted"]:
                p = a["position"]
                await self._manage_position(c, broker, a, p, self.snaps.get(iid) or {"instrument": {"session": "NSE"}}, now)
            self._save(c, a)
            return
        if a.pop("_cancel_entry", False) or not real_ok:
            for o in self._active(a, "entry"):
                await self._cancel(c, broker, a, o, now, "real orders are off or the strategy dropped this entry" if not real_ok else "the strategy dropped this entry")
            if not real_ok:
                if a["pending_entry"]:
                    a["pending_entry"] = None
                    self._ev(a, now, "real orders are off: the waiting entry was dropped")
                if a["position"] and not a["released"]:
                    a["released"] = True
                    self._ev(a, now, f"real orders are off: the open position ({a['position']['contract']}) stays at Angel One with its resting stop order; manage it there")
                self._save(c, a)
                return
        if a["halted"]:
            self._save(c, a)
            return
        await self._reconcile(c, broker, a, now)
        if not a["halted"]:
            await self._advance(c, broker, a, now)
        self._save(c, a)

    async def _update_orders(self, c, a: dict, book: dict, now: dt.datetime) -> None:
        for o in a["orders"]:
            if o["state"] not in ACTIVE or not o.get("broker_id"):
                continue
            b = book.get(str(o["broker_id"]))
            if b is None:
                continue
            st = (b.get("status") or b.get("orderstatus") or "").lower()
            try:
                filled = int(float(b.get("filledshares") or 0))
            except ValueError:
                filled = 0
            if st != "complete" and 0 < filled < o["qty"] and st in ("cancelled", "rejected"):
                self._mark(c, o, a, st, f"partly filled: {filled} of {o['qty']}")
                self._halt(c, a, now, f"a {o['role']} order ended {st} after {filled} of {o['qty']} were filled. Square off or adjust at Angel One, then clear the halt.")
                continue
            if st == "complete":
                try:
                    avg = float(b.get("averageprice") or 0) or o.get("price")
                except ValueError:
                    avg = o.get("price")
                self._mark(c, o, a, "filled", avg=avg)
                self._on_fill(c, a, o, avg, now)
            elif st in ("rejected", "cancelled"):
                self._mark(c, o, a, st, b.get("text") or st)
                self._ev(a, now, f"{o['role']} order {st} by Angel One: {b.get('text') or ''}", order=o["id"])
                if o["role"] == "stop" and st == "rejected" and a["position"]:
                    a["exit_since"] = _s(now - dt.timedelta(seconds=STOP_GRACE_S + 1))
                    self._ev(a, now, "the stop order was rejected: the position is exited now with a LIMIT order, not left unprotected")
                if o["role"] == "entry" and st == "rejected" and a["pending_entry"]:
                    self._miss(a, now, f"entry order rejected: {b.get('text') or ''}")
            elif st in ("open", "trigger pending", "open pending", "validation pending", "put order req received", "modify pending", "after market order req received"):
                o["state"] = "open"

    def _on_fill(self, c, a: dict, o: dict, avg: float | None, now: dt.datetime) -> None:
        if o["role"] == "entry":
            pe = a["pending_entry"] or {}
            ct = pe.get("contract") or {}
            a["position"] = {"side": o["side"], "qty": o["qty"], "contract": o["contract"], "token": o["token"], "exch": o["exch"], "tick": o["tick"], "lot_size": o["lot_size"],
                             "entry_price": avg, "entry_at": _s(now), "strategy_price": pe.get("strategy_price"), "symbol": a["symbol"], "tick_assumed": dryrun._tick(ct)[1] if ct else True}
            if pe.get("strategy_stop") is not None:
                a["stop_target"] = dryrun.rnd(float(pe["strategy_stop"]), o["tick"])
            a["pending_entry"] = None
            a["exit_since"] = None
            a["released"] = False
            self._ev(a, now, f"ENTRY FILLED: {o['side']} {o['qty']} {o['contract']} at {avg}", order=o["id"])
        else:
            p = a["position"]
            if p:
                pnl = ((avg or 0) - (p["entry_price"] or 0)) * p["qty"] * (1 if p["side"] == "BUY" else -1)
                self._ev(a, now, f"POSITION CLOSED by the {o['role']} order at {avg}; real P&L before charges {pnl:,.2f}", order=o["id"])
            a["position"] = None
            a["exit_since"] = None
            a["stop_target"] = None
            a["_clean_stops"] = True

    async def _reconcile(self, c, broker, a: dict, now: dt.datetime) -> None:
        """What Angel One says we hold must equal what we think we hold, on top of whatever the person holds in that contract by hand.
        While this app is flat it only notes the person's own quantity (the baseline); while it holds a position, a difference halts and a person looks."""
        if not (a["position"] or a["pending_entry"] or a["orders"]):
            return
        try:
            pos = await broker.positions()
        except Exception:
            return

        def net_of(contract: str) -> int:
            n = 0
            for p in pos or []:
                if p.get("tradingsymbol") == contract:
                    try:
                        n += int(float(p.get("netqty") or 0))
                    except ValueError:
                        pass
            return n

        live = [o for o in a["orders"] if o["state"] in ACTIVE]
        if not a["position"] and not live:
            # flat: remember what the person holds by hand in the contracts we may trade, so a manual position never looks like ours
            watch = {o["contract"] for o in a["orders"][-10:] if o.get("contract")}
            if a["pending_entry"]:
                watch.add(a["pending_entry"]["contract"].get("trading_symbol"))
            a["baseline"] = {k: net_of(k) for k in watch if k}
            a.pop("mismatch_since", None)
            return
        if self._active(a, "entry") or self._active(a, "exit"):
            a.pop("mismatch_since", None)
            return
        contract = a["position"]["contract"] if a["position"] else live[0]["contract"]
        base = (a.get("baseline") or {}).get(contract, 0)
        mine = a["position"]["qty"] * (1 if a["position"]["side"] == "BUY" else -1) if a["position"] else 0
        net = net_of(contract)
        if net - base == mine:
            a.pop("mismatch_since", None)
            return
        a.setdefault("mismatch_since", _s(now))
        if _age(a["mismatch_since"], now) >= MISMATCH_HALT_S:                      # positions reports can lag a few seconds behind a fill
            own = f" ({base} of it held by hand before this app traded)" if base else ""
            self._halt(c, a, now, f"Angel One shows a net quantity of {net} in {contract}{own} but this app holds {mine}. If you changed your own position in this contract, check it, then clear the halt.")
            a.pop("mismatch_since", None)

    # ------------------------------------------------------------------ advancing the position / entry
    async def _advance(self, c, broker, a: dict, now: dt.datetime) -> None:
        p, pe = a["position"], a["pending_entry"]
        snap = self.snaps.get(a["instrument_id"]) or {}
        if a.pop("_clean_stops", False):
            for o in self._active(a, "stop") + self._active(a, "exit"):
                await self._cancel(c, broker, a, o, now, "the position is closed")
        # the paper position is gone while a real one is held: close ours (method B waits for the stop first)
        if p and a["instrument_id"] in self.snaps and (snap.get("paper") is None or snap["paper"]["direction"] != p["side"]) and a["exit_since"] is None:
            a["exit_since"] = _s(now)
            self._ev(a, now, "the paper position is closed: waiting for our stop order to fill")
        if p:
            await self._manage_position(c, broker, a, p, snap, now)
            return
        if pe:
            await self._manage_entry(c, broker, a, pe, now)

    def _market_open(self, session: str, now: dt.datetime) -> bool:
        from app.paper_trading.engine import market_open
        from app.strategy.top_bottom.gap_protocol import NSE, SESSIONS
        return market_open(now, SESSIONS.get(session, NSE))

    async def _entry_guard(self, c, broker, a: dict, pe: dict, now: dt.datetime) -> str | None:
        uid, ct = a["user_id"], pe["contract"]
        if ct.get("exch_seg", "NFO") not in EXCHANGES:
            return f"real orders on {ct.get('exch_seg')} are not switched on yet (quantity units at Angel One not verified)"
        r = store.row(c, uid)
        if store.effective_mode(r) != "real":
            return "real mode is not in effect: " + "; ".join(store.blockers(r))
        allowed = store.limits(r)["allowed_symbols"]
        if not store.symbol_allowed(a["symbol"], allowed):
            return f"{a['symbol']} is not in the symbols an admin allowed for you ({', '.join(allowed)})"
        if not self._market_open(ct.get("session", "NSE"), now):
            return "market closed"
        if (ct.get("lots") or 1) > store.MAX_REAL_LOTS:
            return f"more than {store.MAX_REAL_LOTS} lots"
        try:
            funds = await broker.funds()
            cash = float(funds.get("availablecash"))
            pnl = float(funds.get("m2mrealized") or 0) + float(funds.get("m2munrealized") or 0)
        except Exception:
            return "could not read your funds"
        if r["max_daily_loss"] is not None and pnl <= -r["max_daily_loss"]:
            return f"today's loss {pnl:,.0f} reached your daily cap of {r['max_daily_loss']:,.0f}"
        try:
            m = await broker.margin([{"exchange": ct.get("exch_seg", "NFO"), "qty": pe["qty"], "price": 0, "productType": "CARRYFORWARD", "token": str(ct["token"]), "tradeType": pe["side"], "orderType": "MARKET"}])
            need = float(m.get("totalMarginRequired"))
        except Exception:
            return "margin could not be computed, so the entry is not sent"
        if need > cash:
            return f"margin needed {need:,.0f} but only {cash:,.0f} is available"
        return None

    async def _manage_entry(self, c, broker, a: dict, pe: dict, now: dt.datetime) -> None:
        live = self._active(a, "entry")
        if live:
            o = live[0]
            if _age(o["placed_at"], now) > RETRY_AFTER_S:
                await self._cancel(c, broker, a, o, now, f"not filled within {int(RETRY_AFTER_S)} s")
            return
        if not self._market_open(pe["contract"].get("session", "NSE"), now):
            pe["created_at"] = _s(now)                              # waiting for the session: the patience clock starts when it opens
            return
        if _age(pe["created_at"], now) > 3600:
            return self._miss(a, now, "the entry waited over an hour")
        if pe["attempts"] >= MAX_ATTEMPTS:
            return self._miss(a, now, f"{MAX_ATTEMPTS} entry attempts were not filled")
        ct = pe["contract"]
        try:
            ltp = await broker.ltp(ct.get("exch_seg", "NFO"), ct["trading_symbol"], str(ct["token"]))
        except Exception:
            ltp = None
        if ltp is None:
            if _age(pe["created_at"], now) > NO_PRICE_MISS_S:
                self._miss(a, now, "no live price for the contract")
            return
        reason = await self._entry_guard(c, broker, a, pe, now)
        if reason:
            if reason in ("market closed",):
                return                                              # wait for the session
            return self._miss(a, now, reason)
        side, P = pe["side"], pe["strategy_price"]
        band, buf = P * ENTRY_BAND_PCT, dryrun.entry_buffer(a["symbol"], P, ct.get("exch_seg"))
        tick = dryrun._tick(ct)[0]
        if side == "BUY":
            if ltp > P + band:
                return self._miss(a, now, f"price ran {ltp - P:.2f} points past the strategy price {P} (band {band:.2f}); not chasing")
            limit = dryrun.rnd(min(P + band, ltp + buf), tick, "up")
        else:
            if ltp < P - band:
                return self._miss(a, now, f"price ran {P - ltp:.2f} points past the strategy price {P} (band {band:.2f}); not chasing")
            limit = dryrun.rnd(max(P - band, ltp - buf), tick, "down")
        pe["attempts"] += 1
        payload = dryrun._payload(ct, side, pe["qty"], "LIMIT", price=limit)
        o = await self._place(c, broker, a, now, "entry", payload, f"entry attempt {pe['attempts']} at live {ltp}", ct, attempt=pe["attempts"])
        if o is not None and o["state"] == "rejected":
            self._miss(a, now, o["message"] or "the entry order was rejected")

    async def _manage_position(self, c, broker, a: dict, p: dict, snap: dict, now: dt.datetime) -> None:
        ct = {"trading_symbol": p["contract"], "token": p["token"], "exch_seg": p["exch"], "tick_size": p["tick"], "lot_size": p.get("lot_size")}
        side_close = "SELL" if p["side"] == "BUY" else "BUY"
        stops, exits = self._active(a, "stop"), self._active(a, "exit")
        paper = snap.get("paper")
        if paper and paper["direction"] == p["side"] and paper.get("active_stop") is not None:
            target = dryrun.rnd(float(paper["active_stop"]), p["tick"])
            if a["stop_target"] is None or abs(target - a["stop_target"]) >= p["tick"] - 1e-9:
                if dryrun.may_move_stop(p["side"], a["stop_target"], target, _p(p["entry_at"]).date() if p.get("entry_at") else now.date(), p, now):
                    a["stop_target"] = target
                    a.pop("held_stop", None)
                    self._ev(a, now, f"stop target is now {target}")
                elif a.get("held_stop") != target:
                    a["held_stop"] = target
                    self._ev(a, now, f"the strategy asked to move the stop BACK to {target}; a real stop never moves back after the day after entry, it stays at {a['stop_target']}")
        market = self._market_open((snap.get("instrument") or {}).get("session", "NSE"), now)
        # exit LIMIT order not filled: cancel, then place again next round at the live price
        if exits:
            if _age(exits[0]["placed_at"], now) > RETRY_AFTER_S:
                await self._cancel(c, broker, a, exits[0], now, f"exit not filled within {int(RETRY_AFTER_S)} s")
            return
        # the stop did not close the position in time: take the stop away and exit with a LIMIT order
        if a["exit_since"] and _age(a["exit_since"], now) > STOP_GRACE_S and market:
            for o in stops:
                if not await self._cancel(c, broker, a, o, now, "exiting with a LIMIT order instead"):
                    return
            try:
                ltp = await broker.ltp(p["exch"], p["contract"], str(p["token"]))
            except Exception:
                ltp = None
            if ltp is None:
                self._ev(a, now, "exit waiting for a live price")
                return
            payload = dryrun._payload(ct, side_close, p["qty"], "LIMIT", price=dryrun._limit(ct, a["symbol"], side_close, ltp))
            await self._place(c, broker, a, now, "exit", payload, "exit the open position", ct)
            return
        if not market:
            return
        if not stops and a["stop_target"] is not None and not a["exit_since"]:
            payload = dryrun._stop_order(ct, a["symbol"], p["side"], p["qty"], a["stop_target"])
            o = await self._place(c, broker, a, now, "stop", payload, "stop-loss for the open position", ct)
            if o is not None and o["state"] == "rejected":
                a["exit_since"] = _s(now - dt.timedelta(seconds=STOP_GRACE_S + 1))
                self._ev(a, now, "the stop order was rejected: the position is exited now with a LIMIT order, not left unprotected")
        elif stops and a["stop_target"] is not None:
            o = stops[0]
            if o["trigger"] is None or abs(a["stop_target"] - o["trigger"]) >= p["tick"] - 1e-9:
                payload = dryrun._stop_order(ct, a["symbol"], p["side"], p["qty"], a["stop_target"])
                try:
                    await broker.modify(o["broker_id"], payload)
                except ao.AngelOneApiError as exc:
                    self._ev(a, now, f"stop could not be moved to {a['stop_target']}: {exc}", order=o["id"])
                except angel_orders.Unknown as exc:
                    self._halt(c, a, now, f"a stop modification got no clear answer from Angel One ({exc}). Check the stop order at Angel One, then clear the halt.")
                else:
                    o["trigger"], o["price"] = float(payload["triggerprice"]), float(payload["price"])
                    c.execute("UPDATE real_tb_orders SET trigger_price=?, price=?, updated_at=? WHERE id=?", (o["trigger"], o["price"], time.time(), o["id"]))
                    c.commit()
                    self._ev(a, now, f"stop moved to {o['trigger']}", order=o["id"])

    # ------------------------------------------------------------------ people
    def clear_halt(self, c, user_id: str, inst_id: str, actor: str) -> bool:
        a = self.state.get((user_id, inst_id))
        if a is None:
            r = c.execute("SELECT state_json FROM real_tb_state WHERE user_id=? AND instrument_id=?", (user_id, inst_id)).fetchone()
            if r is None:
                return False
            a = json.loads(r["state_json"])
            self.state[(user_id, inst_id)] = a
        if not a["halted"]:
            return False
        self._ev(a, now_ist(), f"halt cleared by {actor}: {a['halted'][:100]}")
        a["halted"] = None
        self._save(c, a)
        return True

    def exit_now(self, c, user_id: str, inst_id: str, actor: str) -> str:
        """The person wants out right now: the waiting entry is dropped, the resting stop is cancelled and the position is closed with a LIMIT
        order at the live price (retried every 30 s like any exit). The caller also turns the instrument's real switch off so nothing re-enters."""
        a = self.state.get((user_id, inst_id))
        if a is None:
            r = c.execute("SELECT state_json FROM real_tb_state WHERE user_id=? AND instrument_id=?", (user_id, inst_id)).fetchone()
            if r is None:
                return "nothing"
            a = json.loads(r["state_json"])
            self.state[(user_id, inst_id)] = a
        now = now_ist()
        if a["halted"]:
            return "halted"
        if not a["position"]:
            if a["pending_entry"] or self._active(a, "entry"):
                a["pending_entry"], a["_cancel_entry"] = None, True
                self._ev(a, now, f"exit now by {actor}: the waiting entry is cancelled")
                self._save(c, a)
                return "entry_cancelled"
            return "nothing"
        a["manual_exit"] = True
        if not self._active(a, "exit"):
            a["exit_since"] = _s(now - dt.timedelta(seconds=STOP_GRACE_S + 1))
        self._ev(a, now, f"EXIT NOW pressed by {actor}: the stop is cancelled and the position is closed with a LIMIT order")
        self._save(c, a)
        return "exiting"

    @staticmethod
    def real_trades(c, user_id: str, inst_id: str, per_qty: float = 1.0) -> list[dict]:
        """The person's REAL Top-Bottom trades, built only from fills Angel One confirmed: each entry fill paired with the stop / exit fill that closed it.
        Newest first. P&L is before charges. `per_qty`: rupees per point for one unit of quantity, i.e. the instrument's pnl_units / quantity. It is 1 for
        NSE futures, but MCX gold is quoted per 10 g while its quantity is in grams (Gold Ten: quantity 10 = Rs 1 per point, so 0.1)."""
        rows = c.execute("SELECT role, side, qty, contract, filled_avg, updated_at FROM real_tb_orders WHERE user_id=? AND instrument_id=? AND status='filled' ORDER BY updated_at",
                         (user_id, inst_id)).fetchall()
        out, cur = [], None
        for r in rows:
            if r["role"] == "entry":
                cur = {"direction": r["side"], "qty": r["qty"], "contract": r["contract"], "entry_price": r["filled_avg"], "entry_at": r["updated_at"],
                       "exit_price": None, "exit_at": None, "exit_by": None, "pnl_points": None, "pnl_amount": None, "status": "OPEN"}
                out.append(cur)
            elif cur is not None and cur["status"] == "OPEN":
                sgn = 1 if cur["direction"] == "BUY" else -1
                pts = ((r["filled_avg"] or 0) - (cur["entry_price"] or 0)) * sgn
                cur.update(exit_price=r["filled_avg"], exit_at=r["updated_at"], exit_by=r["role"], pnl_points=round(pts, 2), pnl_amount=round(pts * cur["qty"] * per_qty, 2), status="CLOSED")
        return out[::-1]

    def view(self, c, user_id: str | None) -> list[dict]:
        out = []
        q = "SELECT state_json FROM real_tb_state" + (" WHERE user_id=?" if user_id else "")
        for r in c.execute(q, (user_id,) if user_id else ()):
            a = json.loads(r["state_json"])
            live = self.state.get((a["user_id"], a["instrument_id"]), a)
            out.append({"user_id": live["user_id"], "instrument_id": live["instrument_id"], "symbol": live["symbol"], "position": live["position"], "pending_entry": bool(live["pending_entry"]),
                        "halted": live["halted"], "stop_target": live["stop_target"], "released": live.get("released", False), "exiting": bool(live.get("manual_exit")), "missed": live["missed"][-5:], "events": live["events"][-15:],
                        "orders": [{k: o[k] for k in ("id", "role", "side", "qty", "type", "price", "trigger", "state", "why", "contract")} for o in live["orders"][-10:]]})
        return out

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("real order loop")
            await asyncio.sleep(TICK_S)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
