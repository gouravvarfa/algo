"""Shadow account: what a REAL Angel One account would have done with the Top-Bottom signals, simulated. Nothing is sent to any broker.

It follows the strategy's signals but keeps its OWN orders, position, cash and P&L, and decides every fill itself from the real ticks that arrive
AFTER an order is placed (never from the strategy's price). It can therefore differ from the paper position: an entry may be missed, a stop may not
fill in a gap, a margin shortfall may reject an entry. Those differences are the point; they are shown, not hidden.

Method B (exit first, then entry): the exchange holds a STOPLOSS_LIMIT order for the open quantity only. When it fills, the position is closed;
only then the opposite entry goes out as a LIMIT order, followed by its own stop order. Market and IOC orders are never used.

What is an ASSUMPTION (we have no exchange order book and no traded quantity per tick): a LIMIT fills when a tick trades through its price
(strictly past it when through_tick is on), at the tick's price, never better; there are no partial fills. Every exchange limit we do not know
(freeze quantity, trigger-to-limit range, margin per lot) is left unset rather than invented, and the page says so.

BANKNIFTY futures only for now (ShadowConfig.symbols). Not covered: real orders, margin from the broker, session handling, order rate limits."""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from app.paper_trading import dryrun
from app.strategy.top_bottom.intraday import ENTRY_DAY_STOP_PCT

log = logging.getLogger("shadow")
STATE_PATH = Path("data/paper_trading/shadow_state.json")
LOG_PATH = Path("data/paper_trading/shadow_log.jsonl")
FMT = "%Y-%m-%d %H:%M:%S"
ACTIVE = ("PLACED", "OPEN", "TRIGGER_PENDING")
STALE_QUOTE_SECONDS = 10      # a last price older than this is never used to fill an order
KEEP_ORDERS, KEEP_EVENTS, KEEP_TRADES = 200, 300, 200


@dataclass
class ShadowConfig:
    symbols: tuple = ("BANKNIFTY",)
    capital: float = 200000.0
    margin_per_lot: float | None = None       # NOT guessed: with None the margin check is OFF and the page says so (env SHADOW_MARGIN_PER_LOT)
    latency_s: float = 0.3                    # order -> exchange, and fill -> our app hearing about it
    max_entry_slippage: dict = field(default_factory=lambda: {"BANKNIFTY": 30.0})    # points past the STRATEGY's trigger price; beyond it the entry is missed, never chased
    retry_after_s: float = 30.0               # an entry (or exit) order not filled after this is cancelled and re-placed at the live price
    max_attempts: int = 3                     # entry attempts, then "entry missed"
    stop_grace_s: float = 30.0                # the paper position closed but our stop order did not fill: after this, cancel it and send an exit LIMIT
    stale_signal_s: float = 900.0             # a signal older than this when noticed is ignored, never traded
    through_tick: bool = True                 # a LIMIT needs a tick strictly past its price to fill (conservative)
    stop_basis: str = "REAL_FILL"             # the entry-day 1% stop is taken from REAL_FILL (what the account holds) or STRATEGY_PRICE
    entry_day_stop_pct: float = ENTRY_DAY_STOP_PCT
    freeze_qty: dict = field(default_factory=dict)                 # symbol -> units; unset = not checked. VERIFY WITH ANGEL ONE
    max_trigger_limit_points: dict = field(default_factory=dict)   # symbol -> points; unset = not checked. VERIFY WITH ANGEL ONE
    tick_size: float = dryrun.DEFAULT_TICK                         # used only when the contract has none; the order is then marked tick_assumed
    # VERIFY CURRENT RATES (unverified as of 2026-10-27): brokerage flat per executed order, STT on the SELL side of futures (0.05% since 1 Apr 2026
    # after the Budget; it was 0.02%), NSE futures transaction charge, SEBI fee Rs 10/crore, stamp duty on the BUY side, GST on brokerage + exchange + SEBI.
    charges: dict = field(default_factory=lambda: {"brokerage_flat": 20.0, "stt_sell": 0.0005, "exchange_txn": 0.0000183, "sebi": 0.000001,
                                                   "stamp_buy": 0.00002, "gst": 0.18, "as_of": "2026-10-27", "verify": True})

    @classmethod
    def from_env(cls) -> "ShadowConfig":
        c = cls()
        try:
            if os.environ.get("SHADOW_CAPITAL"):
                c.capital = float(os.environ["SHADOW_CAPITAL"])
            if os.environ.get("SHADOW_MARGIN_PER_LOT"):
                c.margin_per_lot = float(os.environ["SHADOW_MARGIN_PER_LOT"])
        except ValueError:
            log.warning("ignoring a non-numeric SHADOW_CAPITAL / SHADOW_MARGIN_PER_LOT")
        return c


def _s(t: dt.datetime) -> str:
    return t.strftime(FMT) + f".{t.microsecond // 1000:03d}"      # milliseconds: the order latency is a fraction of a second


def _p(s: str) -> dt.datetime:
    try:
        return dt.datetime.strptime(s, FMT + ".%f")
    except ValueError:
        return dt.datetime.strptime(s, FMT)


def market_open(now: dt.datetime) -> bool:
    return now.weekday() < 5 and dt.time(9, 15) <= now.time() <= dt.time(15, 30)


def _after(secs: float, now: dt.datetime) -> dt.datetime:
    return now + dt.timedelta(seconds=secs)


class Shadow:
    def __init__(self, cfg: ShadowConfig | None = None, state_path: Path | None = None, log_path: Path | None = None) -> None:
        self.cfg = cfg or ShadowConfig.from_env()
        self.state_path, self.log_path = state_path or STATE_PATH, log_path or LOG_PATH
        self.accts: dict[str, dict] = {}
        self.seq = 0
        self.last: dict[str, tuple[float, dt.datetime]] = {}      # token -> (last traded price, local time we received it)
        self._watch: set[str] = set()
        self._dirty = False
        self._load()

    # ---------------------------------------------------------------- persistence
    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        self.seq, self.accts = int(raw.get("seq", 0)), raw.get("accts", {})
        self._rewatch()

    def flush(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"seq": self.seq, "accts": self.accts}))
            tmp.replace(self.state_path)
        except OSError:
            log.exception("could not save the shadow account")

    def _ev(self, a: dict, now: dt.datetime, text: str, **kv) -> None:
        rec = {"at": _s(now), "text": text, **kv}
        a["events"].append(rec)
        del a["events"][:-KEEP_EVENTS]
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"instrument_id": a["instrument_id"], **rec}) + "\n")
        except OSError:
            pass
        self._dirty = True

    def _rewatch(self) -> None:
        self._watch = {o["token"] for a in self.accts.values() for o in a["orders"] if o["state"] in ACTIVE}

    # ---------------------------------------------------------------- accounts
    def enabled_for(self, symbol: str) -> bool:
        return symbol in self.cfg.symbols

    def _acct(self, inst_id: str, owner, symbol: str, now: dt.datetime) -> dict:
        a = self.accts.get(inst_id)
        if a is None:
            a = {"instrument_id": inst_id, "owner": owner, "symbol": symbol, "started": _s(now), "gross": 0.0, "charges": 0.0, "closed": 0,
                 "position": None, "orders": [], "trades": [], "pending_entry": None, "exit_since": None, "stop_target": None,
                 "missed": [], "events": [], "paper": None, "stop_alarm": False}
            self.accts[inst_id] = a
            self._dirty = True
        return a

    def _orders(self, a: dict, role: str | None = None, active: bool = True) -> list[dict]:
        return [o for o in a["orders"] if (not active or o["state"] in ACTIVE) and (role is None or o["role"] == role)]

    def _tick_of(self, contract: dict) -> tuple[float, bool]:
        t = contract.get("tick_size") or 0
        return (float(t), False) if t and float(t) > 0 else (self.cfg.tick_size, True)

    # ---------------------------------------------------------------- orders
    def _charges(self, side: str, price: float, qty: int) -> float:
        c, turn = self.cfg.charges, price * qty
        brokerage = c["brokerage_flat"]
        stt = turn * c["stt_sell"] if side == "SELL" else 0.0
        exch, sebi = turn * c["exchange_txn"], turn * c["sebi"]
        stamp = turn * c["stamp_buy"] if side == "BUY" else 0.0
        gst = c["gst"] * (brokerage + exch + sebi)
        return round(brokerage + stt + exch + sebi + stamp + gst, 2)

    def _net(self, a: dict) -> float:
        return a["gross"] - a["charges"]

    def _reject_reason(self, a: dict, o: dict, now: dt.datetime) -> str | None:
        cfg, sym = self.cfg, a["symbol"]
        if not market_open(now):
            return "market closed"
        fz = cfg.freeze_qty.get(sym)
        if fz and o["qty"] > fz:
            return f"quantity {o['qty']} above the freeze quantity {fz}"
        rng = cfg.max_trigger_limit_points.get(sym)
        if rng and o["trigger"] is not None and abs(o["trigger"] - o["price"]) > rng:
            return f"limit is {abs(o['trigger'] - o['price']):.2f} points from the trigger, above the allowed {rng}"
        if o["role"] == "entry" and cfg.margin_per_lot is not None:
            lots = o.get("lots") or max(1, round(o["qty"] / (o.get("lot_size") or o["qty"])))
            need = cfg.margin_per_lot * lots
            have = cfg.capital + self._net(a)
            if need > have:
                return f"margin: {need:,.0f} needed, {have:,.0f} available"
        return None

    def _place(self, a: dict, now: dt.datetime, role: str, otype: str, side: str, qty: int, price: float, trigger: float | None,
               contract: dict, why: str, attempt: int = 0) -> dict:
        self.seq += 1
        tick, assumed = self._tick_of(contract)
        o = {"id": f"S{self.seq:06d}", "role": role, "type": otype, "side": side, "qty": qty, "price": price, "trigger": trigger,
             "contract": contract.get("trading_symbol"), "token": str(contract.get("token")), "lot_size": contract.get("lot_size"), "lots": contract.get("lots"), "tick": tick, "tick_assumed": assumed,
             "why": why, "attempt": attempt, "state": "PLACED", "placed_at": _s(now), "active_at": _s(_after(self.cfg.latency_s, now)),
             "expires_day": now.date().isoformat(), "filled_qty": 0, "avg_price": None, "fill_at": None, "charges": 0.0, "pending_modify": None,
             "history": [[_s(now), "PLACED", why]]}
        a["orders"].append(o)
        reason = self._reject_reason(a, o, now)
        if reason:
            o["state"], o["reject_reason"] = "REJECTED", reason
            o["history"].append([_s(now), "REJECTED", reason])
            self._ev(a, now, f"{role} order {o['id']} REJECTED: {reason}", order=o["id"])
        else:
            self._ev(a, now, f"{role} order {o['id']} placed: {side} {qty} {otype} {price}" + (f" trigger {trigger}" if trigger is not None else "") + f" ({why})", order=o["id"])
        del a["orders"][:-KEEP_ORDERS]
        self._rewatch()
        self._dirty = True
        return o

    def _end(self, a: dict, o: dict, state: str, now: dt.datetime, note: str = "") -> None:
        if o["state"] in ACTIVE:
            o["state"] = state
            o["history"].append([_s(now), state, note])
            self._ev(a, now, f"order {o['id']} {state}" + (f": {note}" if note else ""), order=o["id"])
            self._rewatch()

    # ---------------------------------------------------------------- the strategy's side
    def handle(self, events: list[dict], snap: dict, now: dt.datetime) -> None:
        """Journal events (ENTRY / EXIT / ...) of live paper trades, as the engine writes them."""
        for e in events:
            if e.get("backfill") or not self.enabled_for(e.get("symbol", "")):
                continue
            a = self._acct(e["instrument_id"], e.get("owner"), e["symbol"], now)
            sig = dryrun._minute(e.get("exit_date") if e["event"] == "EXIT" else e.get("entry_date") if e["event"] == "ENTRY" else None)
            if sig and (now - sig).total_seconds() > self.cfg.stale_signal_s:
                self._ev(a, now, f"{e['event']} {e['direction']} ignored: the signal was {int((now - sig).total_seconds() // 60)} min old when it was noticed")
                continue
            try:
                if e["event"] == "ENTRY":
                    self._on_entry(a, e, snap, now)
                elif e["event"] == "EXIT":
                    self._on_exit(a, e, now)
            except Exception:
                log.exception("shadow could not handle %s", e.get("event"))
        self.flush()

    def _on_entry(self, a: dict, e: dict, snap: dict, now: dt.datetime) -> None:
        contract, unresolved = dryrun.event_contract(e, snap)
        if unresolved or not contract.get("token"):
            self._miss(a, now, "contract token unknown, cannot trade it", e["direction"], e["entry_price"])
            return
        contract = dict(contract)
        inst = snap.get("instrument") or {}
        contract["lot_size"] = contract.get("lot_size") or inst.get("lot_size")
        contract["lots"] = inst.get("lots")
        p = a["position"]
        if p and p["side"] == e["direction"]:
            self._ev(a, now, f"ENTRY {e['direction']} ignored: the shadow account already holds that side")
            return
        a["pending_entry"] = {"side": e["direction"], "qty": int(e["quantity"]), "strategy_price": float(e["entry_price"]), "strategy_stop": e.get("active_stop"),
                              "contract": contract, "created_at": _s(now), "signal_time": e.get("entry_date"), "attempts": 0, "order_id": None}
        if p:                                    # method B: the opposite entry waits until our stop order closes the position
            if a["exit_since"] is None:
                a["exit_since"] = _s(now)
            self._ev(a, now, f"strategy reversed to {e['direction']}: waiting for the open position to close first (method B)")
        else:
            self._place_entry(a, now)

    def _on_exit(self, a: dict, e: dict, now: dt.datetime) -> None:
        pe = a["pending_entry"]
        if pe and pe["side"] == e["direction"] and not a["position"]:
            for o in self._orders(a, "entry"):
                self._end(a, o, "CANCELLED", now, "the strategy exited before this entry filled")
            a["pending_entry"] = None
            self._ev(a, now, "the strategy closed the trade this entry belonged to: entry dropped")
            return
        p = a["position"]
        if not p or p["side"] != e["direction"]:
            self._ev(a, now, f"paper EXIT {e['direction']} ({e.get('reason')}): the shadow account held no such position")
            return
        reason = e.get("reason")
        if reason in dryrun.NO_ORDER_EXITS:
            return
        if reason in dryrun.STOP_EXITS:           # our own stop order should fill from the ticks; if it does not, the grace timer sends an exit LIMIT
            if a["exit_since"] is None:
                a["exit_since"] = _s(now)
            self._ev(a, now, f"strategy exit ({reason}): our stop order is expected to fill")
        else:                                      # gap exit / roll: the app sends the exit itself, now, on the position's own contract
            a["exit_since"] = _s(now - dt.timedelta(seconds=self.cfg.stop_grace_s + 1))
            self._ev(a, now, f"strategy exit ({reason}): exiting with a LIMIT order")

    def _miss(self, a: dict, now: dt.datetime, reason: str, side: str | None = None, price: float | None = None) -> None:
        pe = a["pending_entry"]
        side, price = side or (pe or {}).get("side"), price if price is not None else (pe or {}).get("strategy_price")
        a["missed"].append({"at": _s(now), "side": side, "strategy_price": price, "reason": reason, "signal_time": (pe or {}).get("signal_time")})
        del a["missed"][:-KEEP_TRADES]
        for o in self._orders(a, "entry"):
            self._end(a, o, "CANCELLED", now, "entry missed")
        a["pending_entry"] = None
        self._ev(a, now, f"ENTRY MISSED ({side} at strategy price {price}): {reason}")

    def _place_entry(self, a: dict, now: dt.datetime) -> None:
        pe = a["pending_entry"]
        if not pe or a["position"]:
            return
        c, sym = pe["contract"], a["symbol"]
        if (now - _p(pe["created_at"])).total_seconds() > 3600:
            self._miss(a, now, "entry waited over an hour")
            return
        px = self.last.get(str(c["token"]))
        if px is None or (now - px[1]).total_seconds() > STALE_QUOTE_SECONDS:
            if (now - _p(pe["created_at"])).total_seconds() > 120:
                self._miss(a, now, "no live price for the contract")
            return                                  # wait for a tick; poll() tries again
        ltp, side, P = px[0], pe["side"], pe["strategy_price"]
        band, buf = self.cfg.max_entry_slippage.get(sym, 30.0), dryrun.entry_buffer(sym)
        tick, _ = self._tick_of(c)
        if side == "BUY":
            if ltp > P + band:
                return self._miss(a, now, f"price ran {ltp - P:.2f} points past the trigger {P} (limit {band:g}); not chasing")
            limit = dryrun.rnd(min(P + band, ltp + buf), tick, "up")
        else:
            if ltp < P - band:
                return self._miss(a, now, f"price ran {P - ltp:.2f} points past the trigger {P} (limit {band:g}); not chasing")
            limit = dryrun.rnd(max(P - band, ltp - buf), tick, "down")
        pe["attempts"] += 1
        o = self._place(a, now, "entry", "LIMIT", side, pe["qty"], limit, None, c, f"entry attempt {pe['attempts']} at live {ltp}", attempt=pe["attempts"])
        pe["order_id"] = o["id"]
        if o["state"] == "REJECTED":
            self._miss(a, now, f"order rejected: {o['reject_reason']}")

    # ---------------------------------------------------------------- stop management
    def _stop_target(self, a: dict, ppos: dict) -> float | None:
        """The stop our account should hold: the strategy's, except that the entry-day 1% stop is taken from the REAL fill (config)."""
        p = a["position"]
        if not p or ppos.get("active_stop") is None:
            return None
        tick, _ = self._tick_of({"tick_size": p.get("tick")})
        strat_stop, strat_entry, pct = float(ppos["active_stop"]), float(ppos["entry_price"]), self.cfg.entry_day_stop_pct
        pct_stop = strat_entry * (1 - pct) if p["side"] == "BUY" else strat_entry * (1 + pct)
        if self.cfg.stop_basis == "REAL_FILL" and abs(strat_stop - pct_stop) <= max(0.06, tick):
            return dryrun.rnd(p["entry_price"] * (1 - pct) if p["side"] == "BUY" else p["entry_price"] * (1 + pct), tick)
        return dryrun.rnd(strat_stop, tick)

    def _stop_limit(self, a: dict, p: dict, trigger: float) -> float:
        pts = dryrun.stop_limit_points(a["symbol"])
        return dryrun.rnd(trigger - pts, p["tick"], "down") if p["side"] == "BUY" else dryrun.rnd(trigger + pts, p["tick"], "up")

    def _ensure_stop(self, a: dict, now: dt.datetime) -> None:
        p, target = a["position"], a["stop_target"]
        if not p or target is None or self._orders(a, "stop") or self._orders(a, "exit") or not market_open(now):
            return
        c = {"trading_symbol": p["contract"], "token": p["token"], "tick_size": p["tick"], "lot_size": p.get("lot_size")}
        side = "SELL" if p["side"] == "BUY" else "BUY"
        o = self._place(a, now, "stop", "STOPLOSS_LIMIT", side, p["qty"], self._stop_limit(a, p, target), target, c, "stop-loss for the open position")
        if o["state"] == "REJECTED" and a["exit_since"] is None:
            a["exit_since"] = _s(now)               # an unprotected position is closed, not left open
            self._ev(a, now, "the stop order was rejected: the position will be exited with a LIMIT order")

    def sync(self, inst_id: str, owner, symbol: str, snap: dict, now: dt.datetime) -> None:
        """Called after every evaluation of a BankNifty instrument: follow the strategy's stop and notice that the paper position is gone."""
        if not self.enabled_for(symbol):
            return
        a = self._acct(inst_id, owner, symbol, now)
        pp = snap.get("position")
        a["paper"] = {"direction": pp["direction"], "entry_price": pp["entry_price"], "active_stop": pp["active_stop"]} if pp else None
        p = a["position"]
        if p:
            if pp is None or pp["direction"] != p["side"]:
                if a["exit_since"] is None:
                    a["exit_since"] = _s(now)
                    self._ev(a, now, "the paper position is closed: waiting for our stop order to fill")
            else:
                target = self._stop_target(a, pp)
                if target is not None and (a["stop_target"] is None or abs(target - a["stop_target"]) >= p["tick"] - 1e-9):
                    a["stop_target"] = target
                    for o in self._orders(a, "stop"):
                        o["pending_modify"] = {"at": _s(_after(self.cfg.latency_s, now)), "trigger": target, "price": self._stop_limit(a, p, target)}
                        self._ev(a, now, f"stop moves to {target} (the strategy's stop changed)", order=o["id"])
        self.flush()

    # ---------------------------------------------------------------- the market's side
    def on_tick(self, tick) -> None:
        """Every accepted tick of the live feed (registered as a TickFeed listener)."""
        ts = dt.datetime.fromtimestamp(tick.recv_at, dryrun.IST).replace(tzinfo=None)
        self.last[tick.token] = (tick.ltp, ts)
        if tick.token not in self._watch:
            return
        for a in self.accts.values():
            self._advance(a, tick.token, tick.ltp, ts)
        self.flush()

    def _advance(self, a: dict, token: str, ltp: float, ts: dt.datetime) -> None:
        for o in [o for o in a["orders"] if o["token"] == token and o["state"] in ACTIVE]:
            if o["state"] not in ACTIVE:
                continue
            if o["expires_day"] < ts.date().isoformat():
                self._end(a, o, "EXPIRED", ts, "DAY validity ended")
                continue
            if o["state"] == "PLACED":
                if ts < _p(o["active_at"]):
                    continue
                o["state"] = "TRIGGER_PENDING" if o["type"] == "STOPLOSS_LIMIT" else "OPEN"
                o["history"].append([_s(ts), o["state"], "reached the exchange"])
            pm = o.get("pending_modify")
            if pm and ts >= _p(pm["at"]):
                o["trigger"], o["price"], o["pending_modify"] = pm["trigger"], pm["price"], None
                o["history"].append([_s(ts), "MODIFIED", f"trigger {o['trigger']} limit {o['price']}"])
                self._dirty = True
            if o["state"] == "TRIGGER_PENDING":
                if not (ltp <= o["trigger"] if o["side"] == "SELL" else ltp >= o["trigger"]):
                    continue
                o["state"], o["triggered_at"] = "OPEN", _s(ts)
                o["history"].append([_s(ts), "TRIGGERED", f"price {ltp} crossed the trigger {o['trigger']}"])
                self._ev(a, ts, f"stop order {o['id']} triggered at {ltp}", order=o["id"])
            through = self.cfg.through_tick
            ok = (ltp < o["price"] if through else ltp <= o["price"]) if o["side"] == "BUY" else (ltp > o["price"] if through else ltp >= o["price"])
            if ok:
                self._fill(a, o, ltp, ts)
            elif o["type"] == "STOPLOSS_LIMIT" and o.get("triggered_at") and not a["stop_alarm"]:
                a["stop_alarm"] = True
                self._ev(a, ts, f"STOP NOT FILLED: price {ltp} is past the stop's limit {o['price']}; the position is still open", order=o["id"])
        self._rewatch()

    def _fill(self, a: dict, o: dict, price: float, ts: dt.datetime) -> None:
        o["state"], o["filled_qty"], o["avg_price"], o["fill_at"] = "COMPLETE", o["qty"], price, _s(ts)
        o["charges"] = self._charges(o["side"], price, o["qty"])
        a["charges"] += o["charges"]
        o["history"].append([_s(ts), "COMPLETE", f"filled at {price}"])
        self._ev(a, ts, f"order {o['id']} FILLED: {o['side']} {o['qty']} at {price} (charges {o['charges']})", order=o["id"])
        self._dirty = True
        if o["role"] == "entry":
            pe = a["pending_entry"] or {}
            slip = round((price - pe.get("strategy_price", price)) * (1 if o["side"] == "BUY" else -1), 2)
            a["position"] = {"side": o["side"], "qty": o["qty"], "entry_price": price, "entry_time": _s(ts), "contract": o["contract"], "token": o["token"],
                             "tick": o["tick"], "lot_size": o.get("lot_size"), "strategy_entry": pe.get("strategy_price"), "slippage_points": slip,
                             "entry_charges": o["charges"], "attempts": o["attempt"], "lots": o.get("lots")}
            ppos = {"active_stop": pe.get("strategy_stop"), "entry_price": pe.get("strategy_price", price)}
            a["stop_target"] = self._stop_target(a, ppos)
            a["pending_entry"], a["exit_since"], a["stop_alarm"] = None, None, False
            if a["stop_target"] is None:
                self._ev(a, ts, "the strategy gave no stop for this entry: the position is NOT protected")
            else:
                self._ensure_stop(a, _after(self.cfg.latency_s, ts))
        else:                                       # a stop or exit order closed the position
            p = a["position"]
            if p:
                sign = 1 if p["side"] == "BUY" else -1
                gross = round((price - p["entry_price"]) * p["qty"] * sign, 2)
                a["gross"] += gross
                a["closed"] += 1
                a["trades"].append({"side": p["side"], "qty": p["qty"], "contract": p["contract"], "entry_time": p["entry_time"], "entry": p["entry_price"],
                                    "exit_time": _s(ts), "exit": price, "gross": gross, "charges": round(p["entry_charges"] + o["charges"], 2),
                                    "net": round(gross - p["entry_charges"] - o["charges"], 2), "exit_by": o["role"], "slippage_points": p["slippage_points"]})
                del a["trades"][:-KEEP_TRADES]
            a["position"], a["exit_since"], a["stop_target"], a["stop_alarm"], a["exit_attempts"] = None, None, None, False, 0
            for other in self._orders(a):
                if other["role"] in ("stop", "exit"):
                    self._end(a, other, "CANCELLED", ts, "position closed")
            if a["pending_entry"]:
                self._place_entry(a, _after(self.cfg.latency_s, ts))

    # ---------------------------------------------------------------- time
    def poll(self, now: dt.datetime) -> None:
        """Called every engine loop (about once a second while ticks flow): timers, day expiry, stop re-placement."""
        for a in self.accts.values():
            try:
                self._timers(a, now)
            except Exception:
                log.exception("shadow timer failed")
        self.flush()

    def _timers(self, a: dict, now: dt.datetime) -> None:
        for o in self._orders(a):
            if o["expires_day"] < now.date().isoformat():
                self._end(a, o, "EXPIRED", now, "DAY validity ended" + (" (the stop must be placed again at the open)" if o["role"] == "stop" else ""))
        for o in self._orders(a):                    # a quiet market: use the last price if it is fresh
            q = self.last.get(o["token"])
            if q and (now - q[1]).total_seconds() <= STALE_QUOTE_SECONDS:
                self._advance(a, o["token"], q[0], now)
                break
        pe = a["pending_entry"]
        if pe and not a["position"]:
            o = next((x for x in self._orders(a, "entry") if x["id"] == pe.get("order_id")), None)
            if o is None:
                self._place_entry(a, now)
            elif now >= _p(o["active_at"]) + dt.timedelta(seconds=self.cfg.retry_after_s):
                self._end(a, o, "CANCELLED", now, f"not filled in {self.cfg.retry_after_s:g} s")
                if pe["attempts"] >= self.cfg.max_attempts:
                    self._miss(a, now, f"not filled after {pe['attempts']} attempts")
                else:
                    self._place_entry(a, now)
        p = a["position"]
        if p and a["exit_since"] and (now - _p(a["exit_since"])).total_seconds() > self.cfg.stop_grace_s and market_open(now):
            ex = self._orders(a, "exit")
            if ex and now >= _p(ex[0]["active_at"]) + dt.timedelta(seconds=self.cfg.retry_after_s):
                self._end(a, ex[0], "CANCELLED", now, "exit not filled, re-pricing")
                ex = []
            if not ex:
                q = self.last.get(p["token"])
                if q and (now - q[1]).total_seconds() <= STALE_QUOTE_SECONDS:
                    for s in self._orders(a, "stop"):
                        self._end(a, s, "CANCELLED", now, "replaced by an exit order (the stop did not fill)")
                    side = "SELL" if p["side"] == "BUY" else "BUY"
                    c = {"trading_symbol": p["contract"], "token": p["token"], "tick_size": p["tick"], "lot_size": p.get("lot_size")}
                    px = dryrun.rnd(q[0] - dryrun.entry_buffer(a["symbol"]), p["tick"], "down") if side == "SELL" else dryrun.rnd(q[0] + dryrun.entry_buffer(a["symbol"]), p["tick"], "up")
                    a["exit_attempts"] = a.get("exit_attempts", 0) + 1
                    self._place(a, now, "exit", "LIMIT", side, p["qty"], px, None, c, f"exit attempt {a['exit_attempts']} (the stop order did not close the position)")
                    if a["exit_attempts"] > 3:
                        self._ev(a, now, f"EXIT NOT FILLED after {a['exit_attempts'] - 1} attempts: the position is still open")
        self._ensure_stop(a, now)

    # ---------------------------------------------------------------- views
    def _divergence(self, a: dict) -> str:
        paper, p, pe = a.get("paper"), a["position"], a["pending_entry"]
        if paper and p and paper["direction"] == p["side"]:
            return "in sync with the paper position"
        if paper and not p:
            if pe:
                return f"paper is {paper['direction']}, the shadow entry order is still working"
            if a["missed"] and a["missed"][-1]["side"] == paper["direction"]:
                return f"paper is {paper['direction']}, the shadow MISSED it: {a['missed'][-1]['reason']}"
            return f"paper is {paper['direction']}; the shadow holds nothing (the position opened before the shadow account started, or no entry was seen)"
        if p and not paper:
            return "paper is flat, the shadow position is still open: our stop order has not filled yet"
        if p and paper:
            return f"paper is {paper['direction']}, the shadow is still {p['side']}: waiting for the position to close before reversing"
        return "both flat"

    def view(self, owner=None, paper_exits: list | None = None, everyone: bool = False) -> dict:
        cfg = self.cfg
        out = []
        for a in self.accts.values():
            if not everyone and a["owner"] != owner:
                continue
            paper_net = None
            if paper_exits is not None:
                paper_net = round(sum(x["pnl"] for x in paper_exits if x["instrument_id"] == a["instrument_id"] and x["at"] >= a["started"][:16]), 2)
            p, stop = a["position"], next(iter(self._orders(a, "stop")), None)
            flags = []
            if p and not stop:
                flags.append("UNPROTECTED: no stop order is resting on the exchange")
            if a["stop_alarm"] and p:
                flags.append("STOP NOT FILLED: price gapped past the stop's limit, the position is still open")
            if a.get("exit_attempts", 0) > 3 and p:
                flags.append("EXIT NOT FILLED after several attempts")
            net = round(self._net(a), 2)
            q = self.last.get(p["token"]) if p else None
            unrealised = round((q[0] - p["entry_price"]) * p["qty"] * (1 if p["side"] == "BUY" else -1), 2) if p and q else None   # at the last received price, before exit charges
            out.append({"instrument_id": a["instrument_id"], "symbol": a["symbol"], "started": a["started"], "status": "FLAT" if not p else "LONG" if p["side"] == "BUY" else "SHORT",
                        "position": p, "stop": ({k: stop[k] for k in ("id", "trigger", "price", "state")} if stop else None), "paper": a["paper"], "divergence": self._divergence(a),
                        "flags": flags, "pending_entry": ({k: a["pending_entry"][k] for k in ("side", "strategy_price", "attempts")} if a["pending_entry"] else None),
                        "summary": {"gross": round(a["gross"], 2), "charges": round(a["charges"], 2), "net": net, "closed_trades": a["closed"], "capital_now": round(cfg.capital + net, 2),
                                    "paper_net": paper_net, "missed_entries": len(a["missed"]), "unrealised": unrealised},
                        "orders": list(reversed(a["orders"]))[:80], "trades": list(reversed(a["trades"]))[:50], "missed": list(reversed(a["missed"]))[:30], "events": list(reversed(a["events"]))[:80]})
        return {"enabled": True, "symbols": list(cfg.symbols), "accounts": out,
                "config": {"capital": cfg.capital, "margin_per_lot": cfg.margin_per_lot, "margin_check": cfg.margin_per_lot is not None, "latency_s": cfg.latency_s,
                           "max_entry_slippage": cfg.max_entry_slippage, "entry_buffer_points": dryrun.ENTRY_BUFFER_POINTS, "stop_limit_points": dryrun.STOP_LIMIT_POINTS,
                           "retry_after_s": cfg.retry_after_s, "max_attempts": cfg.max_attempts, "stop_grace_s": cfg.stop_grace_s, "through_tick": cfg.through_tick,
                           "stop_basis": cfg.stop_basis, "freeze_qty_checked": bool(cfg.freeze_qty), "trigger_limit_range_checked": bool(cfg.max_trigger_limit_points),
                           "charges_as_of": cfg.charges["as_of"], "charges_verified": not cfg.charges.get("verify", True),
                           "assumptions": ["No exchange order book: a LIMIT fills only when a tick trades through its price, at the tick's price, never better.",
                                           "No traded quantity per tick: there are no partial fills.",
                                           "A DAY stop order expires at the close and is placed again at the next open.",
                                           "Brokerage / STT / exchange charges are rates to VERIFY, not confirmed."]}}
