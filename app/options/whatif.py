"""Simulator what-if analysis: target-date P&L curve and net Greeks for simulated positions.

Everything here is a THEORETICAL Black-Scholes ESTIMATE built on top of real EOD inputs:
  * spot      = the selected date's index close (optionally shifted by `spot_shift_pct` for what-if)
  * IV per leg = implied from that leg's own close on the selected date; if that is impossible (contract not traded,
                 price outside no-arbitrage bounds, missing row) the ATM IV of the same expiry on that date is used and
                 the leg is flagged `atm_fallback`; if neither exists the leg is reported as `no_iv` and its value is
                 held at its last real price (it never gets an invented IV)
  * IV shift  = added to every leg's IV in volatility points (`iv_shift`)
  * time      = calendar days from the target date's close to the 15:30 expiry (+0.25 day, same convention as the chain)
A leg whose expiry is before the selected date is already settled: it contributes its real settlement value, flat.
Greeks units: delta in rupees per 1 index point, gamma in rupee-delta per point, theta in rupees per calendar day,
vega in rupees per 1 volatility point. All are position totals (sign and quantity applied).
"""
from __future__ import annotations

import datetime as dt

from app.options import bs

RATE, DIV = 0.065, 0.0
DAY_FRACTION = 0.25          # chain convention: close -> 15:30 on expiry day counted as a quarter day


def years_to_expiry(on: dt.date, expiry: dt.date) -> float:
    return max((expiry - on).days + DAY_FRACTION, DAY_FRACTION) / 365.0


def leg_iv(close: float | None, volume: int | None, spot: float, strike: float, t: float, is_call: bool,
           atm_iv: float | None) -> tuple[float | None, str]:
    """(IV in %, source). source: 'own' | 'atm_fallback' | 'none'."""
    if close and close > 0 and volume:
        iv = bs.implied_vol(close, spot, strike, t, RATE, DIV, is_call)
        if iv:
            return iv, "own"
    if atm_iv:
        return atm_iv, "atm_fallback"
    return None, "none"


def analyse(legs: list[dict], spot: float, on: dt.date, target: dt.date, xs: list[float],
            iv_shift: float = 0.0, spot_shift_pct: float = 0.0) -> dict:
    """legs: [{side, opt, strike, expiry(date), qty, entry, iv (%) or None, iv_source, settled (value) or None, last (price) or None}]."""
    sign = lambda l: 1 if l["side"] == "BUY" else -1
    what_spot = spot * (1 + spot_shift_pct / 100.0)
    live = [l for l in legs if l.get("settled") is None]

    def value(l: dict, x: float) -> float:
        if l.get("settled") is not None:
            return l["settled"]
        if l["iv"] is None:
            return l["last"] if l["last"] is not None else l["entry"]
        sig = max((l["iv"] + iv_shift) / 100.0, 0.0001)
        return bs.price(x, l["strike"], years_to_expiry(target, l["expiry"]), RATE, DIV, sig, l["opt"] == "CE")

    def pnl(x: float) -> float:
        return sum((value(l, x) - l["entry"]) * l["qty"] * sign(l) for l in legs)

    g = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for l in live:
        if l["iv"] is None:
            continue
        sig = max((l["iv"] + iv_shift) / 100.0, 0.0001)
        lg = bs.greeks(what_spot, l["strike"], years_to_expiry(target, l["expiry"]), RATE, DIV, sig, l["opt"] == "CE")
        for k in g:
            g[k] += lg[k] * l["qty"] * sign(l)
    return {
        "curve": [[round(x, 1), round(pnl(x), 2)] for x in xs],
        "what_spot": round(what_spot, 2), "pnl_at_what_spot": round(pnl(what_spot), 2),
        "greeks": {k: round(v, 4 if k == "gamma" else 2) for k, v in g.items()},
        "legs": [{"iv": None if l["iv"] is None else round(l["iv"], 2), "iv_used": None if l["iv"] is None else round(l["iv"] + iv_shift, 2),
                  "iv_source": "settled" if l.get("settled") is not None else l["iv_source"]} for l in legs],
    }
