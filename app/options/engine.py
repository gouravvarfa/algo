"""EOD options backtest engine on NSE Bhavcopy data (real data only, nothing is filled in).

Fill model (documented, deterministic):
  * entry  : CLOSE of the entry day, slippage applied against us (sell lower / buy higher)
  * leg SL / target : checked from the DAY AFTER entry using that day's HIGH/LOW; fill at the trigger price, or at the
                      OPEN when the day gapped past it. If SL and target are both inside one day's range, SL is assumed
                      (conservative) and the trade is flagged AMBIGUOUS - EOD data cannot tell which came first.
  * trailing SL     : moved once per day from that day's CLOSE (after the day's SL check)
  * overall SL/target : checked on the daily CLOSE mark-to-market only (intraday breaches are invisible in EOD data)
  * exit   : CLOSE of the exit day, or expiry settlement = intrinsic value at the index close on expiry day
  * a contract with no trades that day (volume 0) has no real high/low -> no SL check that day, flagged
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from collections import defaultdict
from pathlib import Path

from app.options import bhavcopy, bs

EOD_NOTE = ("End-of-day data: entries/exits are at the daily close, stop-losses use the daily high/low, and the overall "
            "SL/target sees only the closing mark-to-market. Intraday order of events is unknown.")


# ---------------------------------------------------------------- data access
class Data:
    def __init__(self, symbol: str, db: Path = bhavcopy.DB):
        self.symbol, self.c = symbol, bhavcopy._conn(db)
        self.days = [r[0] for r in self.c.execute("SELECT DISTINCT trade_date FROM options_eod WHERE symbol=? ORDER BY 1", (symbol,))]
        try:
            self.vix = dict(self.c.execute("SELECT trade_date, close FROM vix_close"))
        except sqlite3.OperationalError:
            self.vix = {}                      # VIX not loaded: a VIX filter then skips every cycle with a clear reason
        self.log, self.notes = {}, {}
        for d, st, note in self.c.execute("SELECT trade_date, status, note FROM load_log"):
            self.log[d], self.notes[d] = st, note or ""

    def expiries(self, start: str, end: str) -> list[str]:
        return [r[0] for r in self.c.execute("SELECT DISTINCT expiry FROM options_eod WHERE symbol=? AND expiry BETWEEN ? AND ? ORDER BY 1",
                                             (self.symbol, start, end))]

    def listed_expiries(self, day: str) -> list[str]:
        return [r[0] for r in self.c.execute("SELECT DISTINCT expiry FROM options_eod WHERE symbol=? AND trade_date=? ORDER BY 1",
                                             (self.symbol, day))]

    def rows(self, expiry: str, d0: str, d1: str) -> dict:
        """{date: {(strike, opt): row}} with row = dict(open, high, low, close, volume, oi, lot, und)."""
        out: dict = defaultdict(dict)
        for d, k, o, op, hi, lo, cl, vol, oi, lot, und in self.c.execute(
                "SELECT trade_date, strike, opt, open, high, low, close, volume, oi, lot_size, underlying FROM options_eod "
                "WHERE symbol=? AND expiry=? AND trade_date BETWEEN ? AND ?", (self.symbol, expiry, d0, d1)):
            out[d][(k, o)] = {"open": op or 0.0, "high": hi or 0.0, "low": lo or 0.0, "close": cl, "volume": vol, "oi": oi, "lot": lot, "und": und}
        return out

    def unloaded_weekdays(self, d0: str, d1: str) -> list[str]:
        a, b, miss = dt.date.fromisoformat(d0), dt.date.fromisoformat(d1), []
        while a <= b:
            if a.weekday() < 5 and a.isoformat() not in self.log:
                miss.append(a.isoformat())
            a += dt.timedelta(days=1)
        return miss


# ---------------------------------------------------------------- per-leg expiry
def leg_expiry(rule: str, cycle_expiry: str, listed: list[str]) -> str | None:
    """Expiry a leg trades, chosen from the expiries LISTED on the entry day.
    cycle        the strategy's cycle expiry (default, original behaviour)
    next_weekly  the first listed expiry after the cycle expiry
    monthly      the last listed expiry in the cycle expiry's calendar month
    next_monthly the last listed expiry in the following calendar month"""
    if rule in (None, "cycle"):
        return cycle_expiry if cycle_expiry in listed else None
    later = [e for e in listed if e > cycle_expiry]
    if rule == "next_weekly":
        return later[0] if later else None
    month = cycle_expiry[:7]
    if rule == "next_monthly":
        y, m = int(month[:4]), int(month[5:7])
        month = f"{y + (m == 12)}-{1 if m == 12 else m + 1:02d}"
    same = [e for e in listed if e[:7] == month and e >= cycle_expiry]
    return same[-1] if same else None


# ---------------------------------------------------------------- strike selection
def _traded(day_rows: dict, k: float, opt: str) -> bool:
    r = day_rows.get((k, opt))
    return bool(r and r["volume"] and r["close"] and r["close"] > 0)


def select_strike(day_rows: dict, opt: str, und: float, spec: dict, t_years: float | None = None) -> float | None:
    """Strike for one leg from that day's real rows of the leg's expiry. Returns None when the rule cannot be met
    (the cycle is then skipped with the reason; nothing is approximated).
    ATM {offset}            listed strike nearest the underlying, shifted `offset` strikes (+ = OTM)
    PCT_OTM {value}         listed strike nearest underlying*(1 +/- value%) on the OTM side
    PREMIUM {value}         traded strike whose close is nearest `value`
    PREMIUM_RANGE {low,high} traded strikes with low <= close <= high; the one nearest the middle of the range
    PREMIUM_GTE {value}     traded strike with the smallest close that is >= value
    PREMIUM_LTE {value}     traded strike with the largest close that is <= value
    STRADDLE_WIDTH {value}  ATM +/- value * (ATM CE close + ATM PE close), OTM side for +, ITM for -; nearest listed strike
    DELTA {value}           traded strike whose |delta| (Black-Scholes from its own close) is nearest value/100
    Ties go to the lower strike (deterministic)."""
    strikes = sorted({k for (k, o) in day_rows if o == opt})
    if not strikes:
        return None
    mode = spec["mode"]
    if mode == "ATM":
        i = min(range(len(strikes)), key=lambda j: (abs(strikes[j] - und), strikes[j]))
        i += spec.get("offset", 0) * (1 if opt == "CE" else -1)
        return strikes[i] if 0 <= i < len(strikes) else None
    if mode == "PCT_OTM":
        tgt = und * (1 + spec["value"] / 100.0) if opt == "CE" else und * (1 - spec["value"] / 100.0)
        return min(strikes, key=lambda k: (abs(k - tgt), k))
    traded = [k for k in strikes if _traded(day_rows, k, opt)]
    px = lambda k: day_rows[(k, opt)]["close"]
    if mode == "PREMIUM":
        return min(traded, key=lambda k: (abs(px(k) - spec["value"]), k)) if traded else None
    if mode == "PREMIUM_RANGE":
        lo, hi = spec["low"], spec["high"]
        inside = [k for k in traded if lo <= px(k) <= hi]
        return min(inside, key=lambda k: (abs(px(k) - (lo + hi) / 2), k)) if inside else None
    if mode == "PREMIUM_GTE":
        ok = [k for k in traded if px(k) >= spec["value"]]
        return min(ok, key=lambda k: (px(k), k)) if ok else None
    if mode == "PREMIUM_LTE":
        ok = [k for k in traded if px(k) <= spec["value"]]
        return min(ok, key=lambda k: (-px(k), k)) if ok else None
    if mode == "STRADDLE_WIDTH":
        atm = min({k for (k, _o) in day_rows}, key=lambda k: (abs(k - und), k))
        if not (_traded(day_rows, atm, "CE") and _traded(day_rows, atm, "PE")):
            return None
        width = spec["value"] * (day_rows[(atm, "CE")]["close"] + day_rows[(atm, "PE")]["close"])
        tgt = atm + width if opt == "CE" else atm - width
        return min(strikes, key=lambda k: (abs(k - tgt), k))
    if mode == "DELTA":
        if not t_years:
            return None
        best = None
        for k in traded:
            iv = bs.implied_vol(px(k), und, k, t_years, 0.065, 0.0, opt == "CE")
            if not iv:
                continue
            d = abs(bs.greeks(und, k, t_years, 0.065, 0.0, iv / 100.0, opt == "CE")["delta"]) * 100
            cand = (abs(d - spec["value"]), k)
            best = cand if best is None or cand < best else best
        return best[1] if best else None
    return None


# ---------------------------------------------------------------- charges
def order_charges(price: float, qty: int, is_buy: bool, ch: dict) -> float:
    turn = price * qty
    brok, exch, sebi = ch["brokerage_per_order"], turn * ch["exchange"], turn * ch["sebi"]
    stt = 0.0 if is_buy else turn * ch["stt_sell"]
    stamp = turn * ch["stamp_buy"] if is_buy else 0.0
    return brok + exch + sebi + stt + stamp + (brok + exch + sebi) * ch["gst"]


def settlement_charges(intrinsic: float, qty: int, is_long: bool, ch: dict) -> float:
    return intrinsic * qty * ch["stt_exercise"] if is_long and intrinsic > 0 else 0.0


# ---------------------------------------------------------------- engine
RULES_NOTE = (
    "Daily order of evaluation (EOD data cannot show intraday order, so these assumptions are fixed and documented): "
    "1) each open leg entered BEFORE today is checked against today's high/low for SL then target (both in range = SL, flagged AMBIGUOUS); "
    "2) complete square-off: if any leg hit SL/target today, every other open leg exits at today's close; "
    "3) trailing SL moves from today's close; "
    "4) SL-to-cost: after a leg SL today, the other legs' SL moves to their entry price, effective from the NEXT day; "
    "5) re-entry: a leg stopped out today re-enters at today's close (strike rule re-applied to today's data), checked from the next day; "
    "6) overall SL/target, then lock-profit, are checked on today's closing mark-to-market; "
    "7) on the exit day remaining legs settle (expiry) or exit at the close. No re-entry on the exit day.")


def _pnl(leg: dict, px: float) -> float:
    return (px - leg["entry"]) * leg["qty"] * (1 if leg["side"] == "BUY" else -1)


def _slip(px: float, buying: bool, pct: float) -> float:
    return round(px * (1 + pct / 100.0) if buying else px * (1 - pct / 100.0), 2)


def _levels(leg: dict, L: dict) -> None:
    e, buy = leg["entry"], leg["side"] == "BUY"
    if L["sl"]:
        v = L["sl"]["value"]
        leg["sl"] = (e * (1 - v / 100) if L["sl"]["type"] == "pct" else e - v) if buy else (e * (1 + v / 100) if L["sl"]["type"] == "pct" else e + v)
    if L["target"]:
        v = L["target"]["value"]
        t = (e * (1 + v / 100) if L["target"]["type"] == "pct" else e + v) if buy else (e * (1 - v / 100) if L["target"]["type"] == "pct" else e - v)
        leg["tgt"] = max(t, 0.05)


def _years(day: str, expiry: str) -> float:
    return max((dt.date.fromisoformat(expiry) - dt.date.fromisoformat(day)).days + 0.25, 0.25) / 365.0


def run_cycle(s: dict, expiry: str, entry_day: str, exit_day: str, data: dict, listed: list[str] | None = None) -> dict:
    """data: {expiry: {date: {(strike, opt): row}}} for every expiry the legs may use (the cycle expiry included)."""
    flags: list[str] = []
    cyc = data.get(expiry, {})
    day0 = cyc.get(entry_day, {})
    if not day0:
        return {"skipped": "no rows on entry day"}
    und0 = next(iter(day0.values()))["und"]
    listed = listed or sorted(data)

    def open_leg(i: int, L: dict, day: str, gen: int):
        exp = leg_expiry(L.get("expiry", "cycle"), expiry, listed)
        if exp is None:
            return None, f"leg {i + 1}: expiry rule '{L.get('expiry')}' has no listed expiry on {day}"
        rows = data.get(exp, {}).get(day, {})
        if not rows:
            return None, f"leg {i + 1}: no rows for expiry {exp} on {day}"
        und = next(iter(rows.values()))["und"]
        k = select_strike(rows, L["opt"], und, L["strike"], _years(day, exp))
        if k is None:
            return None, f"leg {i + 1}: no strike satisfies {L['strike']} on {day}"
        r = rows[(k, L["opt"])]
        if r["close"] is None or r["close"] <= 0:
            return None, f"leg {i + 1} {int(k)}{L['opt']}: no valid close on {day}"
        if r["volume"] == 0:
            if s["skip_untraded"]:
                return None, f"leg {i + 1} {int(k)}{L['opt']}: not traded on {day}"
            flags.append(f"{day}: leg {i + 1} entered on an untraded close")
        e = _slip(r["close"], L["side"] == "BUY", s["slippage_pct"])
        leg = {"n": i + 1 if gen == 0 else f"{i + 1}.R{gen}", "base": i, "gen": gen, "side": L["side"], "opt": L["opt"], "strike": k,
               "expiry": exp, "lots": L["lots"], "lot_size": r["lot"], "qty": L["lots"] * r["lot"], "entry": e, "entry_raw": r["close"],
               "entry_day": day, "exit": None, "exit_day": None, "reason": None, "spec": L}
        _levels(leg, L)
        return leg, None

    legs = []
    for i, L in enumerate(s["legs"]):
        leg, why = open_leg(i, L, entry_day, 0)
        if leg is None:
            return {"skipped": why}
        legs.append(leg)

    days = sorted(d for d in cyc if entry_day < d <= exit_day)
    if not days or days[-1] != exit_day:
        return {"skipped": f"no data on exit day {exit_day}"}
    settle_cycle = exit_day == expiry and s["exit_days_before_expiry"] == 0
    realized, lock_floor, lock_peak = 0.0, None, None
    used_reentries = defaultdict(int)

    def row(leg, d):
        return data.get(leg["expiry"], {}).get(d, {}).get((leg["strike"], leg["opt"]))

    def close_leg(leg, px, day, reason, slip=True):
        nonlocal realized
        leg["exit"] = _slip(px, leg["side"] == "SELL", s["slippage_pct"]) if slip else round(px, 2)
        leg["exit_day"], leg["reason"] = day, reason
        realized += _pnl(leg, leg["exit"])

    for d in days:
        und_d = next(iter(cyc[d].values()))["und"] if cyc.get(d) else None
        hit_today: list[tuple[dict, str]] = []
        # 1) leg SL / target on today's range (only legs entered before today)
        for leg in [x for x in legs if x["exit"] is None and x["entry_day"] < d]:
            r = row(leg, d)
            if r is None:
                flags.append(f"{d}: leg {leg['n']} contract missing from Bhavcopy")
                continue
            if r["volume"] == 0 or r["high"] <= 0:
                if "sl" in leg or "tgt" in leg:
                    flags.append(f"{d}: leg {leg['n']} not traded - no SL/target check")
                continue
            buy = leg["side"] == "BUY"
            hit_sl = "sl" in leg and (r["low"] <= leg["sl"] if buy else r["high"] >= leg["sl"])
            hit_t = "tgt" in leg and (r["high"] >= leg["tgt"] if buy else r["low"] <= leg["tgt"])
            if hit_sl and hit_t:
                flags.append(f"{d}: leg {leg['n']} SL and target both inside the day's range - SL assumed (AMBIGUOUS)")
            if hit_sl:
                gap = r["open"] > 0 and (r["open"] <= leg["sl"] if buy else r["open"] >= leg["sl"])
                close_leg(leg, r["open"] if gap else leg["sl"], d, ("SL (gap open)" if gap else "SL") + (" @cost" if leg.get("at_cost") else ""))
                hit_today.append((leg, "SL"))
            elif hit_t:
                gap = r["open"] > 0 and (r["open"] >= leg["tgt"] if buy else r["open"] <= leg["tgt"])
                close_leg(leg, r["open"] if gap else leg["tgt"], d, "TARGET (gap open)" if gap else "TARGET")
                hit_today.append((leg, "TARGET"))
        # 2) complete square-off
        if hit_today and s["on_leg_exit"] == "complete":
            first = hit_today[0]
            for x in [x for x in legs if x["exit"] is None]:
                r = row(x, d)
                if r is None or not r["close"]:
                    flags.append(f"{d}: leg {x['n']} has no close for the complete square-off - kept open")
                    continue
                close_leg(x, r["close"], d, f"SQUARE-OFF (leg {first[0]['n']} {first[1]})")
        # 3) trailing SL from today's close
        for leg in [x for x in legs if x["exit"] is None and x["entry_day"] < d and x["spec"]["trail_pct"]]:
            r = row(leg, d)
            if r and r["close"]:
                tp = leg["spec"]["trail_pct"] / 100
                leg["sl"] = max(leg["sl"], r["close"] * (1 - tp)) if leg["side"] == "BUY" else min(leg["sl"], r["close"] * (1 + tp))
        sl_hits = [x for x, why in hit_today if why == "SL"]
        # 4) SL to cost for the other legs, effective tomorrow
        if sl_hits and s["sl_to_cost"] != "off" and d != exit_day:
            for x in [x for x in legs if x["exit"] is None]:
                if s["sl_to_cost"] == "sl_legs" and "sl" not in x:
                    continue
                x["sl"] = min(x.get("sl", x["entry"]), x["entry"]) if x["side"] == "SELL" else max(x.get("sl", x["entry"]), x["entry"])
                if not x.get("at_cost"):
                    x["at_cost"] = True
                    flags.append(f"{d}: leg {x['n']} SL moved to cost {x['entry']} (after leg SL)")
        # 5) re-entry after SL at today's close
        if d != exit_day and s["on_leg_exit"] == "partial":
            for x in sl_hits:
                L = x["spec"]
                mx = (L.get("reentry") or {}).get("max", 0)
                if used_reentries[x["base"]] >= mx:
                    continue
                leg, why = open_leg(x["base"], L, d, used_reentries[x["base"]] + 1)
                if leg is None:
                    flags.append(f"{d}: re-entry of leg {x['base'] + 1} skipped - {why}")
                    continue
                used_reentries[x["base"]] += 1
                legs.append(leg)
                flags.append(f"{d}: leg {leg['n']} re-entered at close {leg['entry']} ({int(leg['strike'])}{leg['opt']})")
        # 6) overall SL / target, then lock profit, on today's closing MTM
        open_legs = [x for x in legs if x["exit"] is None]
        if open_legs and d != exit_day and (s["overall_sl"] or s["overall_target"] or s["lock_profit"]):
            marks = [(x, row(x, d)) for x in open_legs]
            if all(r and r["close"] is not None for _, r in marks):
                mtm = realized + sum(_pnl(x, r["close"]) for x, r in marks)
                why = "OVERALL SL" if s["overall_sl"] and mtm <= -s["overall_sl"] else \
                      "OVERALL TARGET" if s["overall_target"] and mtm >= s["overall_target"] else None
                lp = s["lock_profit"]
                if not why and lp:
                    if lock_floor is not None and mtm <= lock_floor:
                        why = f"LOCK PROFIT (floor ₹{round(lock_floor)})"
                    elif mtm >= lp["trigger"]:
                        lock_peak = mtm if lock_peak is None else max(lock_peak, mtm)
                        floor = lp["lock"]
                        if lp.get("trail_every") and lp.get("trail_by"):
                            floor += ((lock_peak - lp["trigger"]) // lp["trail_every"]) * lp["trail_by"]
                        if lock_floor is None:
                            flags.append(f"{d}: lock-profit armed at close MTM ₹{round(mtm)} (floor ₹{round(floor)}, checked from the next day)")
                        lock_floor = floor if lock_floor is None else max(lock_floor, floor)
                if why:
                    for x, r in marks:
                        close_leg(x, r["close"], d, why + (" (at close)" if not why.startswith("LOCK") else " at close"))
            else:
                flags.append(f"{d}: overall SL/target/lock not checked - a leg's price is missing")
        # 7) exit day
        if d == exit_day:
            for x in [x for x in legs if x["exit"] is None]:
                if settle_cycle and x["expiry"] == expiry:
                    iv = max(0.0, und_d - x["strike"]) if x["opt"] == "CE" else max(0.0, x["strike"] - und_d)
                    close_leg(x, iv, d, "EXPIRY SETTLEMENT", slip=False)
                else:
                    r = row(x, d)
                    if r is None:
                        return {"skipped": f"leg {x['n']} has no price on exit day {d}"}
                    close_leg(x, r["close"], d, "EXIT TIME")
    ch = s["charges"]
    charges = 0.0
    for x in legs:
        charges += order_charges(x["entry"], x["qty"], x["side"] == "BUY", ch)
        if x["reason"] == "EXPIRY SETTLEMENT":
            charges += settlement_charges(x["exit"], x["qty"], x["side"] == "BUY", ch)
        else:
            charges += order_charges(x["exit"], x["qty"], x["side"] == "SELL", ch)
        x["pnl"] = round(_pnl(x, x["exit"]), 2)
        x["strike"] = int(x["strike"]) if x["strike"] == int(x["strike"]) else x["strike"]
        for k in ("spec", "base", "gen", "at_cost"):
            x.pop(k, None)
        for k in ("sl", "tgt"):
            if k in x:
                x[k] = round(x[k], 2)
    gross = round(sum(x["pnl"] for x in legs), 2)
    exit_und = next(iter(cyc[exit_day].values()))["und"]
    return {"expiry": expiry, "entry_day": entry_day, "exit_day": max(x["exit_day"] for x in legs), "und_entry": und0, "und_exit": exit_und,
            "legs": legs, "gross": gross, "charges": round(charges, 2), "net": round(gross - charges, 2), "flags": flags,
            "reentries": sum(used_reentries.values())}


def run(s: dict, db: Path = bhavcopy.DB) -> dict:
    """`s` must already be validated (strategy.validate). Every expiry is simulated independently."""
    D = Data(s["symbol"], db)
    start, end = s["start"].isoformat(), s["end"].isoformat()
    exps = D.expiries(start, end)
    if s["expiry"] == "monthly":
        last: dict[str, str] = {}
        for e in exps:
            last[e[:7]] = e
        exps = sorted(last.values())
    trades, skipped = [], []
    for e in exps:
        if e not in D.days:
            skipped.append({"expiry": e, "reason": "expiry-day Bhavcopy not loaded"})
            continue
        i = D.days.index(e)
        ie, ix = i - s["entry_days_before_expiry"], i - s["exit_days_before_expiry"]
        if ie < 0 or D.days[ie] < start:
            skipped.append({"expiry": e, "reason": "entry day before loaded data / start date"})
            continue
        gaps = D.unloaded_weekdays(D.days[ie], D.days[ix])
        if gaps:
            skipped.append({"expiry": e, "reason": f"Bhavcopy not loaded for {gaps}"})
            continue
        vf = s.get("vix_filter")
        if vf:
            v = D.vix.get(D.days[ie])
            if v is None:
                skipped.append({"expiry": e, "reason": f"India VIX not available for entry day {D.days[ie]} (VIX filter on)"})
                continue
            if (vf.get("min") is not None and v < vf["min"]) or (vf.get("max") is not None and v > vf["max"]):
                skipped.append({"expiry": e, "reason": f"VIX filter: India VIX {v:.2f} on {D.days[ie]} outside {vf.get('min', '-')}..{vf.get('max', '-')}"})
                continue
        listed = D.listed_expiries(D.days[ie])
        need = {e} | {x for L in s["legs"] for x in [leg_expiry(L.get("expiry", "cycle"), e, listed)] if x}
        data = {x: D.rows(x, D.days[ie], D.days[ix]) for x in sorted(need)}
        res = run_cycle(s, e, D.days[ie], D.days[ix], data, listed)
        if "skipped" in res:
            skipped.append({"expiry": e, "reason": res["skipped"]})
        else:
            note = D.notes.get(D.days[ie], "")
            if note.startswith("legacy"):        # old-format Bhavcopy: say where lot size and underlying came from
                part = next((x.strip() for x in note.split("|") if x.strip().replace("legacy format; ", "").startswith(s["symbol"] + ":")), "")
                res["flags"].append(f"{D.days[ie]}: legacy NSE Bhavcopy - {part.replace('legacy format; ', '') or 'lot size derived from turnover'}")
            res["vix_entry"] = D.vix.get(D.days[ie])
            trades.append(res)
    return {"strategy": {**s, "start": start, "end": end}, "trades": trades, "skipped": skipped, "metrics": metrics(trades),
            "note": EOD_NOTE + " " + RULES_NOTE, "data_range": [D.days[0], D.days[-1]] if D.days else None}


# ---------------------------------------------------------------- reports
def metrics(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    nets = [t["net"] for t in trades]
    wins, losses = [x for x in nets if x > 0], [x for x in nets if x <= 0]
    eq, peak, dd, curve = 0.0, 0.0, 0.0, []
    for t in sorted(trades, key=lambda t: (t["exit_day"], t["expiry"])):
        eq += t["net"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
        curve.append({"date": t["exit_day"], "equity": round(eq, 2), "drawdown": round(eq - peak, 2)})
    monthly, weekday, reasons = defaultdict(float), defaultdict(lambda: [0, 0.0]), defaultdict(int)
    for t in trades:
        for leg in t["legs"]:
            reasons[leg["reason"]] += 1
    for t in trades:
        monthly[t["exit_day"][:7]] += t["net"]
        w = weekday[dt.date.fromisoformat(t["entry_day"]).strftime("%a")]
        w[0] += 1
        w[1] += t["net"]
    return {
        "trades": len(trades), "total": round(sum(nets), 2), "gross": round(sum(t["gross"] for t in trades), 2),
        "charges": round(sum(t["charges"] for t in trades), 2), "win_rate": round(100 * len(wins) / len(nets), 1),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0, "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "max_drawdown": round(dd, 2), "best": max(nets), "worst": min(nets),
        "equity": curve, "monthly": {k: round(v, 2) for k, v in sorted(monthly.items())},
        "weekday": {k: {"trades": v[0], "pnl": round(v[1], 2)} for k, v in weekday.items()},
        # additive fields (older saved runs are recomputed from their trades by the compare endpoint)
        "wins": len(wins), "losses": len(losses), "avg_trade": round(sum(nets) / len(nets), 2),
        "per_trade": [{"expiry": t["expiry"], "exit_day": t["exit_day"], "net": t["net"]}
                      for t in sorted(trades, key=lambda t: (t["exit_day"], t["expiry"]))],
        "exit_reasons": dict(sorted(reasons.items())),
        # P4 (additive). expectancy = win% * avg win + loss% * avg loss = mean net per trade (rupees);
        # expectancy_ratio = expectancy / |avg loss| (None when there are no losing trades)
        "expectancy": round(sum(nets) / len(nets), 2),
        "expectancy_ratio": round((sum(nets) / len(nets)) / abs(sum(losses) / len(losses)), 3) if losses and sum(losses) < 0 else None,
        **_streaks([t["net"] for t in sorted(trades, key=lambda t: (t["exit_day"], t["expiry"]))]),
        "daily": _table(trades, lambda t: t["exit_day"]),
        "yearly": _table(trades, lambda t: t["exit_day"][:4]),
    }


def _streaks(nets: list[float]) -> dict:
    """Longest runs of consecutive winning (net > 0) and losing (net <= 0) trades, in exit order."""
    best = {"max_consecutive_wins": 0, "max_consecutive_losses": 0}
    run, kind = 0, None
    for v in nets:
        k = "max_consecutive_wins" if v > 0 else "max_consecutive_losses"
        run = run + 1 if k == kind else 1
        kind = k
        best[k] = max(best[k], run)
    return best


def _table(trades: list[dict], key) -> list[dict]:
    """Net P&L grouped by `key` (exit date / exit year): trades, wins, gross, charges, net."""
    g: dict = {}
    for t in trades:
        r = g.setdefault(key(t), {"period": key(t), "trades": 0, "wins": 0, "gross": 0.0, "charges": 0.0, "net": 0.0})
        r["trades"] += 1
        r["wins"] += t["net"] > 0
        for f in ("gross", "charges", "net"):
            r[f] += t[f]
    return [{**r, **{f: round(r[f], 2) for f in ("gross", "charges", "net")}} for _, r in sorted(g.items())]


# ---------------------------------------------------------------- simulator payoff (expiry payoff of manual positions)
def payoff(positions: list[dict], spot: float, width_pct: float = 6.0, n: int = 241) -> dict:
    """positions: [{side, opt, strike, entry, qty}]. Expiry P&L curve + exact max profit/loss and breakevens."""
    def at(x):
        tot = 0.0
        for p in positions:
            iv = max(0.0, x - p["strike"]) if p["opt"] == "CE" else max(0.0, p["strike"] - x)
            tot += (iv - p["entry"]) * p["qty"] * (1 if p["side"] == "BUY" else -1)
        return tot
    lo, hi = spot * (1 - width_pct / 100), spot * (1 + width_pct / 100)
    xs = [lo + (hi - lo) * j / (n - 1) for j in range(n)]
    kinks = sorted({0.0, *(p["strike"] for p in positions)})
    vals = [at(k) for k in kinks]
    far = max(kinks) * 10 + 1
    up_slope = at(far + 1) - at(far)                      # P&L slope beyond the last strike
    max_p = float("inf") if up_slope > 1e-9 else max(vals + [at(far)])
    max_l = float("-inf") if up_slope < -1e-9 else min(vals + [at(far)])
    be, pts = [], sorted(set(kinks + [far]))
    for a, b in zip(pts, pts[1:]):
        fa, fb = at(a), at(b)
        if fa == 0:
            be.append(a)
        elif fa * fb < 0:
            be.append(a + (b - a) * (-fa) / (fb - fa))
    net = sum(p["entry"] * p["qty"] * (1 if p["side"] == "SELL" else -1) for p in positions)
    return {"curve": [[round(x, 1), round(at(x), 2)] for x in xs], "max_profit": None if max_p == float("inf") else round(max_p, 2),
            "max_loss": None if max_l == float("-inf") else round(max_l, 2), "breakevens": [round(b, 1) for b in be if b > 0],
            "net_credit": round(net, 2)}
