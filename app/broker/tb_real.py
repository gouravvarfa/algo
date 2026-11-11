"""Real Angel One orders for the Top-Bottom strategy: the shadow account's lifecycle (app/paper_trading/shadow.py), but with real orders and real fills.

What it does, for each person and instrument whose real switch is ON (and the account in effective real mode):
  * Starts at the NEXT new System Point after the switch was turned on. A position the paper account already holds then is not copied; its exit finds no real position and is ignored.
  * ENTRY: a LIMIT order at the live price plus a small allowance; not filled in 3 s -> cancelled, then a MARKET order (decided with the user, 10 Nov 2026: an entry is never
    skipped for price). Once it fills, a STOPLOSS_LIMIT order for exactly that quantity sits on the exchange, its trigger one tick beyond the strategy's stop.
  * EXIT, method B: the stop order closes the position; only then does the opposite entry go out (a reversal never holds two quantities). If the strategy has exited and our stop has
    not filled within 3 s, the stop is cancelled and an exit LIMIT order is sent (then MARKET). Gap exits and rolls send the exit order at once.
  * The stop follows the strategy's stop (break-even, gap protection, the trail after the close) by modifying the resting stop order; it never moves back after the first trading day.
  * Before every ENTRY: lots within the person's limit, today's loss under the cap, margin available (fail closed), market hours of the instrument's session, at most 40 orders a day.
  * A CANCEL counts only once Angel One's order book shows the order cancelled: an order can fill in the very instant it is cancelled, and Angel One accepts the cancel request
    anyway. Until then nothing new is sent for it (a MARKET retry or an exit after a "cancelled" order that had in fact filled would double the position).
  * After the app closes a position it checks that Angel One shows it closed too; an answer that says the session expired means nothing was received and the order goes again
    after a fresh login (it is never taken as a refusal, which would exit a position or drop an entry).
  * Anything it cannot be sure of HALTS that instrument (a request that timed out, a broker position that does not match what we hold, exits refused again and again): no further
    order is sent until the person clears the halt after looking at Angel One. Nothing is retried blind.
  * Switching the instrument off, or the account leaving real mode, cancels an entry that has not filled. An open position keeps its resting stop order and is then the person's to manage.
Every order is stored (real_tb_orders) and audited. The state survives a restart. Angel One allows about one order-book / positions read a second, so each round reads
them once per person for all of that person's instruments.
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
RETRY_AFTER_S = 3.0               # decided with the user, 10 Nov 2026: a LIMIT entry / exit not filled in 3 s goes again at the market
MAX_ATTEMPTS = 3
STOP_GRACE_S = 3.0                # a stop that triggered (or should have) and did not fill in 3 s: exit at the market
STALE_SIGNAL_S = 900.0
NO_PRICE_MISS_S = 120.0
ENTRY_BAND_PCT = 0.005
MISMATCH_HALT_S = 20.0
IDLE_POLL_S = 60.0                 # a released position (switch off) or a closed market: look at Angel One once a minute, not every 5 s
GAP_WAIT_MAX_S = 1500.0            # a gap open: wait for the strategy's 15-minute decision at most this long (25 min), then the ordinary stop
REAL_TICK_FALLBACK = 1.0           # price grid when the contract's tick is unknown: Rs 1 is a multiple of every NSE / MCX futures tick (0.05, 0.10, 0.20, 1), so the price is always valid
EXCHANGES = ("NFO", "MCX")        # MCX quantity = lots x the scrip master's lotsize (its own unit: GOLD 1 = 1 kg lot, GOLDM 100 g, SILVER 30 kg), checked Nov 2026
MAX_ORDERS_PER_DAY = 40
TICK_S = 5.0
KEEP_EVENTS = 80
ACTIVE = ("placed", "open")                  # live at Angel One: managed (waited for, modified, cancelled)
TRACKED = ACTIVE + ("cancelling",)           # outcome not known yet: read from the order book every round (a cancel sent but not yet confirmed)
CANCEL_CONFIRM_S = 15.0                      # a cancel the order book has not confirmed in this long is sent again ...
MAX_CANCEL_TRIES = 3                         # ... and after this many the instrument halts
CONFIRM_PAUSE_S = 1.1                        # before reading the order book again to confirm a cancel (about one read a second is allowed)
MAX_EXIT_REJECTS = 3                         # exit orders refused in a row -> halt (an endless stream of refused orders helps nobody)
STALL_RELOGIN_S = 90.0                       # Angel One failing every call this long: log in again once (a dead session that does not say so)
RELOGIN_MIN_GAP_S = 300.0
MISSING_HALT_S = 60.0                        # an order Angel One accepted today but its order book does not list after this long: how it ended is unknown
GUARD_FUNDS = "could not read your funds"    # entry checks that a busy Angel One can fail: asked again for up to NO_PRICE_MISS_S before the entry is dropped
GUARD_MARGIN = "margin could not be computed, so the entry is not sent"


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


class _RoundBroker:
    """One person's broker for one executor round. The order book and the positions are read ONCE per round for all of that person's instruments
    (Angel One answers about one such read a second; a read per instrument made it refuse them). A failed read is remembered for the round too,
    so the person's other instruments wait for the next round instead of hammering Angel One. book(fresh=True) reads again (a cancel confirmation)."""

    def __init__(self, inner, memo: dict):
        self._inner, self._memo = inner, memo

    async def _once(self, key: str, call, fresh: bool = False):
        if fresh or key not in self._memo:
            try:
                self._memo[key] = (True, await call())
            except Exception as exc:
                self._memo[key] = (False, exc)
        ok, v = self._memo[key]
        if not ok:
            raise v
        return v

    async def book(self, fresh: bool = False):
        return await self._once("book", self._inner.book, fresh)

    async def positions(self):
        return await self._once("positions", self._inner.positions)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class RealTB:
    def __init__(self, broker_for=angel_broker_for) -> None:
        self.broker_for = broker_for
        self.state: dict[tuple[str, str], dict] = {}
        self.snaps: dict[str, dict] = {}
        self.queue: list[dict] = []
        self.lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._fail_since: dict[str, dt.datetime] = {}               # per person: since when every round's first Angel One read has failed
        self._relogin_at: dict[str, dt.datetime] = {}

    # ------------------------------------------------------------------ what the paper engine tells us (cheap, synchronous, never raises into it)
    def handle(self, events: list[dict], snap: dict, now: dt.datetime) -> None:
        self.queue.extend(e for e in events if not e.get("backfill"))

    def sync(self, inst_id: str, owner, symbol: str, snap: dict, now: dt.datetime) -> None:
        if not snap.get("real_ready", True):
            return          # not a result a real account may act on (the daily-close fallback, an expired contract's result): the last good one stays
        pp = snap.get("position")
        self.snaps[inst_id] = {"owner": owner, "symbol": symbol, "instrument": snap.get("instrument") or {}, "ltp": snap.get("ltp"), "snap_ltp_at": _s(now),
                               "paper": {"direction": pp["direction"], "entry_price": pp["entry_price"], "active_stop": pp["active_stop"],
                                         "gap_state": pp.get("gap_state"), "protective_sl": pp.get("protective_sl")} if pp else None}

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

    def _cancelling(self, a: dict, role: str | None = None) -> list[dict]:
        """Orders whose cancel Angel One accepted but whose end the order book has not shown yet: they may still have filled."""
        return [o for o in a["orders"] if o["state"] == "cancelling" and (role is None or o["role"] == role)]

    @staticmethod
    def _unsettled(a: dict) -> bool:
        return any(o["state"] in TRACKED for o in a["orders"])

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
            if self._dead_session(a, broker, exc):
                # the session had expired, so Angel One received nothing: NOT a refusal of this order (a refused stop would exit the position, a refused
                # entry would be dropped). The same step runs again next round, after a fresh login.
                o["state"], status, msg = "not_sent", "not_sent", f"not received, Angel One's session had expired ({exc}): sent again after a fresh login"
            else:
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
        return None if o["state"] == "not_sent" else o

    @staticmethod
    def _dead_session(a: dict, broker, exc: BaseException) -> bool:
        """Angel One said the session is no longer valid: forget it (the next round logs in once) and tell the caller nothing was done."""
        from app.broker import orders as _orders
        return _orders.drop_if_dead(a["user_id"], exc, getattr(broker, "s", None))

    def _mark(self, c, o: dict, a: dict, status: str, msg: str | None = None, avg: float | None = None) -> None:
        o["state"] = "filled" if status == "filled" else status
        if avg is not None:
            o["filled_avg"] = avg
        c.execute("UPDATE real_tb_orders SET status=?, message=COALESCE(?, message), filled_avg=COALESCE(?, filled_avg), updated_at=? WHERE id=?", (status, msg, avg, time.time(), o["id"]))
        c.commit()

    async def _cancel(self, c, broker, a: dict, o: dict, now: dt.datetime, why: str) -> bool:
        """Ask Angel One to cancel. True = the request was accepted, which does NOT mean the order is cancelled: it may have filled in the same instant
        (the exchange then refuses the cancel while Angel One already said yes). The order stays 'cancelling' until the order book shows how it ended."""
        if o["state"] not in ACTIVE or not o.get("broker_id"):
            return True
        try:
            await broker.cancel(o["broker_id"])
        except ao.AngelOneApiError as exc:
            self._dead_session(a, broker, exc)
            self._ev(a, now, f"cancel of {o['role']} order refused ({exc}); it may already have filled", order=o["id"])
            return False
        except angel_orders.Unknown as exc:
            self._halt(c, a, now, f"a cancel got no clear answer from Angel One ({exc}). Look at your Angel One orders, then clear the halt.")
            return False
        o["state"], o["cancel_at"], o["cancel_tries"] = "cancelling", _s(now), o.get("cancel_tries", 0) + 1
        c.execute("UPDATE real_tb_orders SET status='cancelling', message=?, updated_at=? WHERE id=?", (why, time.time(), o["id"]))
        c.commit()
        self._ev(a, now, f"{o['role']} order: cancel sent ({why}); waiting for Angel One's order book to confirm it", order=o["id"])
        return True

    async def _confirm_cancels(self, c, broker, a: dict, now: dt.datetime) -> None:
        """Read the order book again (after a short pause: about one read a second is allowed) so a cancel just sent is settled in this round."""
        if not self._cancelling(a):
            return
        await asyncio.sleep(CONFIRM_PAUSE_S)
        try:
            book = {str(x.get("orderid")): x for x in await broker.book(fresh=True)}
        except Exception:
            return                                                         # settled next round
        await self._update_orders(c, a, book, now)

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
                    if a["position"] or a["pending_entry"] or self._unsettled(a) or a.get("expect_flat"):
                        keys.setdefault((uid, iid), a["symbol"])
                memo: dict[str, dict] = {}                                  # per person: this round's order book and positions, read once
                for (uid, iid), sym in keys.items():
                    try:
                        await self._step(c, uid, iid, sym, now, memo.setdefault(uid, {}))
                    except Exception:
                        log.exception("real order step failed for %s", sym)
            finally:
                c.close()

    async def _step(self, c, uid: str, iid: str, sym: str, now: dt.datetime, memo: dict | None = None) -> None:
        row = c.execute("SELECT real_on, changed_at FROM user_instrument_real WHERE user_id=? AND instrument_id=?", (uid, iid)).fetchone()
        a = self._acct(c, uid, iid, sym, row["changed_at"] if row else 0)
        busy = bool(a["position"] or a["pending_entry"] or self._unsettled(a) or a.get("_cancel_entry") or a.get("expect_flat"))
        if not busy:
            return                                                        # nothing open, nothing waiting: no call to Angel One at all
        session = (a["pending_entry"] or {}).get("contract", {}).get("session") or (self.snaps.get(iid) or {}).get("instrument", {}).get("session", "NSE")
        quiet = a.get("released") or (not a["pending_entry"] and not a.get("exit_since") and not self._cancelling(a) and not self._market_open(session, now))
        if quiet and a.get("polled_at") and _age(a["polled_at"], now) < IDLE_POLL_S:
            return
        a["polled_at"] = _s(now)
        try:
            broker = _RoundBroker(await self.broker_for(c, uid), memo if memo is not None else {})
            book = {str(o.get("orderid")): o for o in await broker.book()}
        except Exception as exc:
            self._unreachable(c, a, uid, exc, now)
            self._save(c, a)
            return
        self._fail_since.pop(uid, None)
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
                if a["position"] and a.get("unwanted_fill") and not a["halted"]:
                    self._halt(c, a, now, f"an entry filled although it was being cancelled after real orders were switched off: {a['position']['side']} "
                                          f"{a['position']['qty']} {a['position']['contract']} is open at Angel One with NO stop order. Close it there now, then clear the halt.")
                elif a["position"] and not a["released"]:
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

    def _unreachable(self, c, a: dict, uid: str, exc: BaseException, now: dt.datetime) -> None:
        """This round could not read Angel One, so nothing was sent. An expired session is forgotten at once (the next round logs in once). Anything
        else (a rate-limit page, network trouble) keeps the session: a new login lifts neither, and every login adds a session on the account. Only
        when every call has failed for STALL_RELOGIN_S (a dead session that does not say so) is there one fresh login, at most one per RELOGIN_MIN_GAP_S."""
        from app.broker import orders as _orders
        if _orders.drop_if_dead(uid, exc):
            note = "Angel One's session had expired: a fresh login next round"
        else:
            since = self._fail_since.setdefault(uid, now)
            last = self._relogin_at.get(uid)
            if (now - since).total_seconds() >= STALL_RELOGIN_S and (last is None or (now - last).total_seconds() >= RELOGIN_MIN_GAP_S):
                _orders._drop(uid)
                self._relogin_at[uid] = now
                note = f"every call has failed for {int((now - since).total_seconds())} s: one fresh login next round"
            else:
                note = "trying again next round"
        self._ev(a, now, f"Angel One not reachable this round ({type(exc).__name__}: {str(exc)[:80]}); nothing was sent; {note}")

    async def _update_orders(self, c, a: dict, book: dict, now: dt.datetime) -> None:
        for o in a["orders"]:
            if o["state"] not in TRACKED or not o.get("broker_id"):
                continue
            was_cancelling = o["state"] == "cancelling"
            b = book.get(str(o["broker_id"]))
            if b is None:
                if o.get("placed_at") and _p(o["placed_at"]).date() < now.date():
                    # a DAY order ends with its session and Angel One's book starts empty each day: an order of an earlier day that we never saw end
                    # (the app was down at the close) is over. A stop is placed again when the market opens; it is never taken as still resting.
                    self._mark(c, o, a, "cancelled", "a day order of an earlier session: no longer in Angel One's order book")
                    self._ev(a, now, f"{o['role']} order of {o['placed_at'][:10]} is no longer at Angel One (a day order ends with its session)", order=o["id"])
                elif not a["halted"] and _age(o.get("placed_at"), now) > MISSING_HALT_S:
                    self._halt(c, a, now, f"a {o['role']} order Angel One accepted today (order id {o['broker_id']}) is not in its order book after "
                                          f"{int(_age(o.get('placed_at'), now))} s, so how it ended is unknown. Look at your Angel One orders, then clear the halt.")
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
                if was_cancelling:
                    self._ev(a, now, f"the {o['role']} order had FILLED before the cancel reached the exchange: it counts as filled", order=o["id"])
                self._on_fill(c, a, o, avg, now)
            elif st in ("rejected", "cancelled"):
                self._mark(c, o, a, st, b.get("text") or st)
                self._ev(a, now, f"{o['role']} order {st}" + (" (confirmed by Angel One's order book)" if was_cancelling else f" by Angel One: {b.get('text') or ''}"), order=o["id"])
                if o["role"] == "stop" and st == "rejected" and a["position"] and not was_cancelling:
                    a["exit_since"] = _s(now - dt.timedelta(seconds=STOP_GRACE_S + 1))
                    self._ev(a, now, "the stop order was rejected: the position is exited now with a LIMIT order, not left unprotected")
                if o["role"] == "entry" and st == "rejected" and a["pending_entry"] and not was_cancelling:
                    self._miss(a, now, f"entry order rejected: {b.get('text') or ''}")
            elif was_cancelling:
                # still open (or 'cancel pending') after the cancel was accepted: wait; if it lasts, the cancel is sent again, and in the end a person looks
                if _age(o.get("cancel_at"), now) > CANCEL_CONFIRM_S:
                    if a["halted"]:
                        pass                                                   # a person is already looking
                    elif o.get("cancel_tries", 1) >= MAX_CANCEL_TRIES:
                        self._halt(c, a, now, f"Angel One accepted {o['cancel_tries']} cancels of a {o['role']} order but its order book still shows it '{st}'. "
                                              "Look at the order at Angel One (cancel it there, or note that it filled), then clear the halt.")
                    else:
                        o["state"] = "open"                                    # managed again: the usual step cancels it once more
                        self._ev(a, now, f"the {o['role']} order still shows '{st}' {int(_age(o.get('cancel_at'), now))} s after its cancel was accepted: cancelling again", order=o["id"])
            elif st in ("open", "trigger pending", "open pending", "validation pending", "put order req received", "modify pending", "after market order req received"):
                o["state"] = "open"
                if o["role"] == "stop":
                    if st == "open":                       # a stop-loss LIMIT order shows 'open' once its trigger was hit and its limit waits for a fill
                        o.setdefault("triggered_at", _s(now))
                    elif st == "trigger pending":
                        o.pop("triggered_at", None)

    def _on_fill(self, c, a: dict, o: dict, avg: float | None, now: dt.datetime) -> None:
        if o["role"] == "entry":
            held = a["position"]
            if held:
                # a second entry fill on top of an open position: never planned (a cancel is confirmed before any retry), so a person looks
                held["qty"] += o["qty"] if held["side"] == o["side"] else -o["qty"]
                self._ev(a, now, f"ENTRY FILLED AGAIN: {o['side']} {o['qty']} {o['contract']} at {avg} on top of the open position", order=o["id"])
                if not a["halted"]:
                    self._halt(c, a, now, f"two entry orders filled: this app now holds {held['qty']} of {o['contract']} ({held['side']}) while the stop order covers less. "
                                          "Bring the position at Angel One back to what you want, then clear the halt.")
                return
            pe = a["pending_entry"]
            wanted = bool(pe and pe.get("side") == o["side"])
            pe = pe or {}
            ct = pe.get("contract") or {}
            a["position"] = {"side": o["side"], "qty": o["qty"], "contract": o["contract"], "token": o["token"], "exch": o["exch"], "tick": o["tick"], "lot_size": o["lot_size"],
                             "entry_price": avg, "entry_at": _s(now), "strategy_price": pe.get("strategy_price"), "symbol": a["symbol"], "tick_assumed": dryrun._tick(ct)[1] if ct else True}
            if pe.get("strategy_stop") is not None:
                a["stop_target"] = dryrun.rnd(float(pe["strategy_stop"]), o["tick"])
            a["pending_entry"] = None
            a["exit_since"] = None
            a["released"] = False
            a.pop("expect_flat", None)
            self._ev(a, now, f"ENTRY FILLED: {o['side']} {o['qty']} {o['contract']} at {avg}", order=o["id"])
            if not wanted:
                # it filled while it was being cancelled because the strategy no longer wanted it (or real orders were switched off): close it again
                a["unwanted_fill"] = True
                a["exit_since"] = _s(now - dt.timedelta(seconds=STOP_GRACE_S + 1))
                self._ev(a, now, "this entry filled although it was being cancelled (the strategy no longer wants it): it is closed again now")
        else:
            p = a["position"]
            if p:
                pnl = ((avg or 0) - (p["entry_price"] or 0)) * p["qty"] * (1 if p["side"] == "BUY" else -1)
                self._ev(a, now, f"POSITION CLOSED by the {o['role']} order at {avg}; real P&L before charges {pnl:,.2f}", order=o["id"])
                a["expect_flat"] = {"contract": p["contract"], "at": _s(now)}         # _reconcile checks that Angel One shows it closed too
            elif not a["halted"]:
                self._halt(c, a, now, f"a {o['role']} order ({o['side']} {o['qty']} {o['contract']}) filled while this app held no position, so Angel One now "
                                      "shows a position this app did not want (an exit filled twice). Close it at Angel One now, then clear the halt.")
            a["position"] = None
            a["exit_since"] = None
            a["stop_target"] = None
            a.pop("exit_tries", None)
            a.pop("exit_rejects", None)
            a.pop("unwanted_fill", None)
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

        live = [o for o in a["orders"] if o["state"] in TRACKED]
        if not a["position"] and not live:
            ef = a.get("expect_flat")
            if ef:
                # this app just closed its position: Angel One must show the contract back at the person's own quantity. Anything else means an
                # exit filled twice (or a fill this app never saw): never taken as the person's own position (the baseline below would do that)
                base = (a.get("baseline") or {}).get(ef["contract"], 0)
                net = net_of(ef["contract"])
                if net == base:
                    a.pop("expect_flat", None)
                elif _age(ef["at"], now) >= MISMATCH_HALT_S and not a["halted"]:
                    a.pop("expect_flat", None)
                    self._halt(c, a, now, f"this app closed its position in {ef['contract']}, but Angel One still shows a net quantity of {net} there "
                                          f"(expected {base}). Close what should not be open at Angel One now, then clear the halt.")
                return                                             # the baseline is not taken again until this is settled
            # flat: remember what the person holds by hand in the contracts we may trade, so a manual position never looks like ours
            watch = {o["contract"] for o in a["orders"][-10:] if o.get("contract")}
            if a["pending_entry"]:
                watch.add(a["pending_entry"]["contract"].get("trading_symbol"))
            a["baseline"] = {k: net_of(k) for k in watch if k}
            a.pop("mismatch_since", None)
            return
        if self._active(a, "entry") or self._active(a, "exit") or self._cancelling(a, "entry") or self._cancelling(a, "exit"):
            a.pop("mismatch_since", None)                          # an order whose outcome is still coming: the position is not settled yet
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
            await self._confirm_cancels(c, broker, a, now)              # a leftover stop that fills now must be seen before the next entry
            if a["halted"]:
                return
            p, pe = a["position"], a["pending_entry"]
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
        except Exception as exc:
            self._dead_session(a, broker, exc)
            return GUARD_FUNDS
        if r["max_daily_loss"] is not None and pnl <= -r["max_daily_loss"]:
            return f"today's loss {pnl:,.0f} reached your daily cap of {r['max_daily_loss']:,.0f}"
        try:
            m = await broker.margin([{"exchange": ct.get("exch_seg", "NFO"), "qty": pe["qty"], "price": 0, "productType": "CARRYFORWARD", "token": str(ct["token"]), "tradeType": pe["side"], "orderType": "MARKET"}])
            need = float(m.get("totalMarginRequired"))
        except Exception as exc:
            self._dead_session(a, broker, exc)
            return GUARD_MARGIN
        if need > cash:
            return f"margin needed {need:,.0f} but only {cash:,.0f} is available"
        return None

    async def _manage_entry(self, c, broker, a: dict, pe: dict, now: dt.datetime) -> None:
        live = self._active(a, "entry")
        if live:
            o = live[0]
            if _age(o["placed_at"], now) > RETRY_AFTER_S:
                await self._cancel(c, broker, a, o, now, f"not filled within {int(RETRY_AFTER_S)} s")
                await self._confirm_cancels(c, broker, a, now)
            if not (a["pending_entry"] and not self._unsettled(a)):
                return                                              # filled after all, or the cancel is not confirmed yet: nothing new goes out
        if self._cancelling(a):
            return                                                  # an order whose cancel is not confirmed yet may still fill: wait for its end
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
        if self._orders_today(c, a["user_id"]) >= MAX_ORDERS_PER_DAY:
            return self._miss(a, now, f"{MAX_ORDERS_PER_DAY} orders were already sent today")
        reason = await self._entry_guard(c, broker, a, pe, now)
        if reason:
            if reason in ("market closed",):
                return                                              # wait for the session
            if reason in (GUARD_FUNDS, GUARD_MARGIN) and _age(pe["created_at"], now) <= NO_PRICE_MISS_S:
                if pe.get("waiting") != reason:                     # a busy Angel One is asked again next round (the entry is not dropped for that)
                    pe["waiting"] = reason
                    self._ev(a, now, f"entry waits: {reason}; asking Angel One again")
                return
            return self._miss(a, now, reason)
        side = pe["side"]
        buf, tick = dryrun.entry_buffer(a["symbol"], ltp, ct.get("exch_seg")), dryrun._tick(ct)[0]
        # decided with the user (10 Nov 2026): the first try is a LIMIT at the live price; not filled in RETRY_AFTER_S -> at the MARKET. Never skipped.
        if pe["attempts"] == 0:
            payload = dryrun._payload(ct, side, pe["qty"], "LIMIT", price=dryrun.rnd(ltp + buf, tick, "up") if side == "BUY" else dryrun.rnd(ltp - buf, tick, "down"))
        else:
            payload = dryrun._payload(ct, side, pe["qty"], "MARKET", price=0)
        attempt = pe["attempts"] + 1
        o = await self._place(c, broker, a, now, "entry", payload, f"entry attempt {attempt} at live {ltp}", ct, attempt=attempt)
        if o is None:
            return                                                  # nothing reached Angel One (its session had expired): the same attempt next round
        pe["attempts"] = attempt
        if o["state"] == "rejected":
            self._miss(a, now, o["message"] or "the entry order was rejected")

    async def _gap_wait(self, broker, a: dict, p: dict, paper: dict | None, session: str, now: dt.datetime) -> bool:
        """True = do not place the stop yet. The FIRST stop of the day finds price already BEYOND it: the market opened through the stop (a gap). The
        strategy then suspends its stop until its first 15-minute candle completes (it starts when the market opens that day: NSE 09:15, MCX 09:00, or
        later on a late-opening day such as MCX's 17:00 session on an NSE holiday), and then either exits and reverses (that candle's low / high broke)
        or holds with that low / high as today's stop. A stop sent now would be refused (its trigger is passed) and the position closed at the gap,
        which the strategy does not do. So the real position waits for the strategy's decision, at most GAP_WAIT_MAX_S."""
        today = now.date().isoformat()
        same = bool(paper and paper["direction"] == p["side"])
        if same and paper.get("protective_sl") is not None:
            return False                                       # decided and held: the stop goes to today's protective level
        started = a.get("gap_wait_at") if a.get("gap_wait") == today else None
        if started and _age(started, now) > GAP_WAIT_MAX_S:
            return False                                       # no decision in time: the ordinary stop (refused -> exit at once)
        if same and paper.get("gap_state") == "GAP_PENDING":
            pending = True
        elif started:
            pending = True                                     # waiting since the open: the strategy's decision is due
        elif any(o["role"] == "stop" and (o.get("placed_at") or "")[:10] == today for o in a["orders"]):
            pending = False                                    # a stop already went out today: this is not the open
        else:
            try:
                ltp = await broker.ltp(p["exch"], p["contract"], str(p["token"]))
            except Exception:
                ltp = None
            if ltp is None:                                    # Angel One did not answer: the paper engine's live price of the same contract
                sn = self.snaps.get(a["instrument_id"]) or {}
                if (sn.get("instrument") or {}).get("trading_symbol") == p["contract"] and sn.get("ltp") is not None:
                    ltp = float(sn["ltp"])
            # strictly beyond, as the strategy's own gap test (an open exactly AT the stop is no gap: the stop goes out as usual)
            pending = ltp is not None and ((ltp < a["stop_target"]) if p["side"] == "BUY" else (ltp > a["stop_target"]))
        if pending and not started:
            a["gap_wait"], a["gap_wait_at"] = today, _s(now)
            self._ev(a, now, f"the market opened beyond the stop {a['stop_target']}: as the strategy's gap rule, no stop until its first 15-minute candle "
                             f"completes; then that candle's {'low' if p['side'] == 'BUY' else 'high'} is the stop, or the position exits and reverses")
        return pending

    async def _manage_position(self, c, broker, a: dict, p: dict, snap: dict, now: dt.datetime) -> None:
        ct = {"trading_symbol": p["contract"], "token": p["token"], "exch_seg": p["exch"], "tick_size": p["tick"], "lot_size": p.get("lot_size")}
        side_close = "SELL" if p["side"] == "BUY" else "BUY"
        stops, exits = self._active(a, "stop"), self._active(a, "exit")
        paper = snap.get("paper")
        session = (snap.get("instrument") or {}).get("session", "NSE")
        market = self._market_open(session, now)
        dryrun.note_trading_day(p, _p(p["entry_at"]).date() if p.get("entry_at") else now.date(), now, market)
        if paper and paper["direction"] == p["side"]:
            prot = paper.get("protective_sl")       # today's gap protection: the first 15-minute candle's low (BUY) / high (SELL) is the strategy's stop today
            raw = prot if prot is not None else paper.get("active_stop")
            if raw is not None:
                target = dryrun.rnd(float(raw), p["tick"])
                if a["stop_target"] is None or abs(target - a["stop_target"]) >= p["tick"] - 1e-9:
                    if prot is not None or dryrun.may_move_stop(p["side"], a["stop_target"], target, p, now):
                        a["stop_target"] = target
                        a.pop("held_stop", None)
                        self._ev(a, now, f"stop target is now {target}" + (" (today's gap protection: the first 15-minute candle)" if prot is not None else ""))
                    elif a.get("held_stop") != target:
                        a["held_stop"] = target
                        self._ev(a, now, f"the strategy asked to move the stop BACK to {target}; a real stop never moves back after the first trading day after entry, it stays at {a['stop_target']}")
        # our stop triggered but its limit did not fill (price ran past it): do not wait for the strategy to notice, exit with a LIMIT order
        trig = next((o for o in stops if o.get("triggered_at")), None)
        if trig and market and not a["exit_since"] and _age(trig["triggered_at"], now) > STOP_GRACE_S:
            try:
                ltp = await broker.ltp(p["exch"], p["contract"], str(p["token"]))
            except Exception:
                ltp = None
            past = ltp is not None and trig["trigger"] is not None and ((ltp <= trig["trigger"]) if p["side"] == "BUY" else (ltp >= trig["trigger"]))
            if past:
                a["exit_since"] = _s(now - dt.timedelta(seconds=STOP_GRACE_S + 1))
                self._ev(a, now, f"the stop triggered at {trig['trigger']} but did not fill within {int(STOP_GRACE_S)} s (price {ltp} ran past its limit): exiting with a LIMIT order", order=trig["id"])
        # exit order not filled in RETRY_AFTER_S: cancel it; only once Angel One's order book confirms the cancel does the next exit go (at the market)
        if exits:
            if _age(exits[0]["placed_at"], now) > RETRY_AFTER_S:
                await self._cancel(c, broker, a, exits[0], now, f"exit not filled within {int(RETRY_AFTER_S)} s")
                await self._confirm_cancels(c, broker, a, now)
            if a["position"] is not p or a["halted"] or self._active(a, "exit") or self._cancelling(a, "exit"):
                return                                         # it filled after all (the position is closed), or the cancel is not confirmed yet
        elif self._cancelling(a, "exit"):
            return
        # the stop did not close the position in time: take the stop away and exit with a LIMIT order
        if a["exit_since"] and _age(a["exit_since"], now) > STOP_GRACE_S and market:
            for o in stops:
                if not await self._cancel(c, broker, a, o, now, "exiting with a LIMIT order instead"):
                    return
            await self._confirm_cancels(c, broker, a, now)
            if a["position"] is not p or a["halted"] or self._active(a, "stop") or self._cancelling(a, "stop"):
                return                                         # the stop filled in that instant (closed: no exit order), or its cancel is not confirmed yet
            try:
                ltp = await broker.ltp(p["exch"], p["contract"], str(p["token"]))
            except Exception as exc:
                self._dead_session(a, broker, exc)
                ltp = None
            if ltp is None:
                self._ev(a, now, "exit waiting for a live price")
                return
            tries = a.get("exit_tries", 0)
            payload = (dryrun._payload(ct, side_close, p["qty"], "LIMIT", price=dryrun._limit(ct, a["symbol"], side_close, ltp)) if tries == 0
                       else dryrun._payload(ct, side_close, p["qty"], "MARKET", price=0))          # a LIMIT first; not filled in RETRY_AFTER_S -> the MARKET
            o = await self._place(c, broker, a, now, "exit", payload, "exit the open position", ct)
            if o is None:
                return                                         # nothing reached Angel One (its session had expired): again next round
            a["exit_tries"] = tries + 1
            if o["state"] == "rejected":
                a["exit_rejects"] = a.get("exit_rejects", 0) + 1
                if a["exit_rejects"] >= MAX_EXIT_REJECTS and not a["halted"]:
                    self._halt(c, a, now, f"Angel One refused {a['exit_rejects']} exit orders in a row (last: {o['message']}). The position {p['side']} {p['qty']} "
                                          f"{p['contract']} is still OPEN with no stop order: close it at Angel One now, then clear the halt.")
            else:
                a.pop("exit_rejects", None)
            return
        if not market:
            return
        if not stops and not self._cancelling(a, "stop") and a["stop_target"] is not None and not a["exit_since"]:
            if await self._gap_wait(broker, a, p, paper, session, now):
                return
            payload = dryrun._stop_order(ct, a["symbol"], p["side"], p["qty"], a["stop_target"])
            o = await self._place(c, broker, a, now, "stop", payload, "stop-loss for the open position", ct)
            if o is not None and o["state"] == "rejected":
                a["exit_since"] = _s(now - dt.timedelta(seconds=STOP_GRACE_S + 1))
                self._ev(a, now, "the stop order was rejected: the position is exited now with a LIMIT order, not left unprotected")
        elif stops and a["stop_target"] is not None and not stops[0].get("triggered_at"):
            o = stops[0]
            want = dryrun.stop_trigger(a["stop_target"], dryrun._tick(ct)[0], side_close)
            if o["trigger"] is None or abs(want - o["trigger"]) >= p["tick"] - 1e-9:
                payload = dryrun._stop_order(ct, a["symbol"], p["side"], p["qty"], a["stop_target"])
                try:
                    await broker.modify(o["broker_id"], payload)
                except ao.AngelOneApiError as exc:
                    self._dead_session(a, broker, exc)                 # the resting stop stays where it was; the move is tried again next round
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
