"""Strategy JSON schema + validator for the EOD options backtester (NIFTY first).

Example (short straddle, 25% leg SL):
{
  "name": "Short straddle", "symbol": "NIFTY", "start": "2026-01-01", "end": "2026-10-16",
  "expiry": "weekly",                       # weekly = every expiry, monthly = last expiry of each month
  "entry_days_before_expiry": 2,           # trading days before expiry; position is opened at that day's CLOSE
  "exit_days_before_expiry": 0,            # 0 = hold to expiry settlement
  "legs": [
    {"side": "SELL", "opt": "CE", "lots": 1, "strike": {"mode": "ATM", "offset": 0}, "sl": {"type": "pct", "value": 25}},
    {"side": "SELL", "opt": "PE", "lots": 1, "strike": {"mode": "ATM", "offset": 0}, "sl": {"type": "pct", "value": 25}}
  ],
  "overall_sl": 5000, "overall_target": null,      # rupees, checked on daily CLOSE mark-to-market
  "slippage_pct": 0.5, "skip_untraded": true
}

Strike modes (offset is counted in listed strikes, positive = further OUT of the money, negative = in the money):
  ATM {offset}          strike closest to the underlying close, shifted by offset
  PCT_OTM {value}       listed strike closest to underlying * (1 +/- value%) on the OTM side
  PREMIUM {value}       strike whose close is closest to value rupees (traded contracts only)
"""
from __future__ import annotations

import datetime as dt

MAX_LEGS = 10
SYMBOLS = ("NIFTY", "BANKNIFTY")
STRIKE_MODES = ("ATM", "PCT_OTM", "PREMIUM", "PREMIUM_RANGE", "PREMIUM_GTE", "PREMIUM_LTE", "STRADDLE_WIDTH", "DELTA")
LEG_EXPIRIES = ("cycle", "next_weekly", "monthly", "next_monthly")

DEFAULT_CHARGES = {           # configurable; rates as fractions of turnover unless stated
    "brokerage_per_order": 20.0,
    "stt_sell": 0.001,         # options sell-side premium
    "stt_exercise": 0.00125,   # ITM long settled at expiry, on intrinsic value
    "exchange": 0.0003503,     # NSE options transaction charge on premium
    "sebi": 0.000001,          # Rs 10 per crore
    "stamp_buy": 0.00003,      # buy side
    "gst": 0.18,               # on brokerage + exchange + SEBI
}


def _sl(x, path: str, errs: list[str]) -> None:
    if x is None:
        return
    if not isinstance(x, dict) or x.get("type") not in ("pct", "pts") or not isinstance(x.get("value"), (int, float)) or x["value"] <= 0:
        errs.append(f"{path}: must be {{type: pct|pts, value: > 0}}")


def validate(s: dict) -> tuple[dict, list[str]]:
    """Returns (normalised strategy, errors). Never runs anything when errors is non-empty."""
    errs: list[str] = []
    out = dict(s)
    out["symbol"] = str(s.get("symbol", "NIFTY")).upper()
    if out["symbol"] not in SYMBOLS:
        errs.append(f"symbol: only {SYMBOLS} supported for now")
    try:
        out["start"], out["end"] = dt.date.fromisoformat(s["start"]), dt.date.fromisoformat(s["end"])
        if out["end"] < out["start"]:
            errs.append("end is before start")
    except (KeyError, TypeError, ValueError):
        errs.append("start/end: required as YYYY-MM-DD")
    out["expiry"] = s.get("expiry", "weekly")
    if out["expiry"] not in ("weekly", "monthly"):
        errs.append("expiry: weekly | monthly")
    for k, d in (("entry_days_before_expiry", 0), ("exit_days_before_expiry", 0)):
        v = s.get(k, d)
        if not isinstance(v, int) or v < 0 or v > 30:
            errs.append(f"{k}: integer 0-30")
        out[k] = v
    if isinstance(out["entry_days_before_expiry"], int) and isinstance(out["exit_days_before_expiry"], int) \
            and out["exit_days_before_expiry"] > out["entry_days_before_expiry"]:
        errs.append("exit_days_before_expiry must be <= entry_days_before_expiry")
    legs = s.get("legs") or []
    if not 1 <= len(legs) <= MAX_LEGS:
        errs.append(f"legs: 1-{MAX_LEGS} required")
    out["legs"] = []
    for i, leg in enumerate(legs[:MAX_LEGS]):
        p = f"legs[{i}]"
        side, opt, lots = str(leg.get("side", "")).upper(), str(leg.get("opt", "")).upper(), leg.get("lots", 1)
        if side not in ("BUY", "SELL"):
            errs.append(f"{p}.side: BUY | SELL")
        if opt not in ("CE", "PE"):
            errs.append(f"{p}.opt: CE | PE")
        if not isinstance(lots, int) or not 1 <= lots <= 100:
            errs.append(f"{p}.lots: integer 1-100")
        st = leg.get("strike") or {"mode": "ATM", "offset": 0}
        mode = str(st.get("mode", "")).upper()
        if mode not in STRIKE_MODES:
            errs.append(f"{p}.strike.mode: one of {STRIKE_MODES}")
        elif mode == "ATM" and not isinstance(st.get("offset", 0), int):
            errs.append(f"{p}.strike.offset: integer (strikes, + = OTM)")
        elif mode in ("PCT_OTM", "PREMIUM", "PREMIUM_GTE", "PREMIUM_LTE") and (not _num(st.get("value")) or st["value"] <= 0):
            errs.append(f"{p}.strike.value: number > 0")
        elif mode == "PREMIUM_RANGE" and not (_num(st.get("low")) and _num(st.get("high")) and 0 < st["low"] < st["high"]):
            errs.append(f"{p}.strike: PREMIUM_RANGE needs 0 < low < high")
        elif mode == "STRADDLE_WIDTH" and not (_num(st.get("value")) and st["value"] != 0 and -5 <= st["value"] <= 5):
            errs.append(f"{p}.strike.value: straddle multiplier between -5 and 5, not 0 (+ = OTM, - = ITM)")
        elif mode == "DELTA" and not (_num(st.get("value")) and 1 <= st["value"] <= 99):
            errs.append(f"{p}.strike.value: target delta 1-99")
        lexp = leg.get("expiry") or "cycle"
        if lexp not in LEG_EXPIRIES:
            errs.append(f"{p}.expiry: one of {LEG_EXPIRIES}")
        re_ = leg.get("reentry")
        if re_ is not None:
            if not isinstance(re_, dict) or not isinstance(re_.get("max"), int) or not 1 <= re_["max"] <= 5:
                errs.append(f"{p}.reentry: {{max: 1-5}} or null")
            elif not leg.get("sl"):
                errs.append(f"{p}.reentry needs a leg SL (re-entry happens only after a stop-loss)")
        _sl(leg.get("sl"), f"{p}.sl", errs)
        _sl(leg.get("target"), f"{p}.target", errs)
        tr = leg.get("trail_pct")
        if tr is not None and (not isinstance(tr, (int, float)) or tr <= 0):
            errs.append(f"{p}.trail_pct: number > 0")
        if tr is not None and not leg.get("sl"):
            errs.append(f"{p}.trail_pct needs an sl")
        out["legs"].append({"side": side, "opt": opt, "lots": lots, "strike": {**st, "mode": mode},
                            "sl": leg.get("sl"), "target": leg.get("target"), "trail_pct": tr, "expiry": lexp, "reentry": re_})
    for k in ("overall_sl", "overall_target"):
        v = s.get(k)
        if v is not None and (not isinstance(v, (int, float)) or v <= 0):
            errs.append(f"{k}: rupees > 0 or null")
        out[k] = v
    sl = s.get("slippage_pct", 0.0)
    if not isinstance(sl, (int, float)) or not 0 <= sl <= 10:
        errs.append("slippage_pct: 0-10")
    out["slippage_pct"] = float(sl) if isinstance(sl, (int, float)) else 0.0
    out["skip_untraded"] = bool(s.get("skip_untraded", True))
    out["charges"] = {**DEFAULT_CHARGES, **(s.get("charges") or {})}
    out["name"] = s.get("name") or "Strategy"
    # ---- risk rules (P3). Defaults reproduce the original behaviour exactly.
    out["on_leg_exit"] = s.get("on_leg_exit") or "partial"
    if out["on_leg_exit"] not in ("partial", "complete"):
        errs.append("on_leg_exit: partial | complete")
    out["sl_to_cost"] = s.get("sl_to_cost") or "off"
    if out["sl_to_cost"] not in ("off", "sl_legs", "all_legs"):
        errs.append("sl_to_cost: off | sl_legs | all_legs")
    lp = s.get("lock_profit")
    if lp is not None:
        ok = isinstance(lp, dict) and _num(lp.get("trigger")) and lp["trigger"] > 0 and _num(lp.get("lock")) and lp["lock"] < lp["trigger"]
        te, tb = (lp or {}).get("trail_every"), (lp or {}).get("trail_by")
        if not ok:
            errs.append("lock_profit: {trigger > 0, lock < trigger} (rupees, closing MTM)")
        elif (te is None) != (tb is None) or (te is not None and not (_num(te) and _num(tb) and te > 0 and tb > 0)):
            errs.append("lock_profit: trail_every and trail_by must both be > 0, or both omitted")
    out["lock_profit"] = lp
    vf = s.get("vix_filter")
    if vf is not None:
        lo, hi = (vf or {}).get("min"), (vf or {}).get("max")
        if not isinstance(vf, dict) or (lo is None and hi is None) or any(x is not None and not (_num(x) and 0 < x <= 100) for x in (lo, hi))                 or (lo is not None and hi is not None and lo >= hi):
            errs.append("vix_filter: {min and/or max} India VIX between 0 and 100, min < max (checked on the entry day's VIX close)")
        else:
            vf = {k: v for k, v in (("min", lo), ("max", hi)) if v is not None}
    out["vix_filter"] = vf
    if out["on_leg_exit"] == "complete":
        if out["sl_to_cost"] != "off":
            errs.append("sl_to_cost needs on_leg_exit = partial (complete square-off leaves no legs to move)")
        if any(L.get("reentry") for L in out["legs"]):
            errs.append("leg re-entry needs on_leg_exit = partial")
    return out, errs


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)
