"""Black-Scholes (European, continuous dividend yield q) price, Greeks and implied volatility.

Used ONLY as a fallback when the broker does not return Greeks/IV for a strike. Index options on NSE are
European and cash-settled, so there is no early-assignment term. Inputs the caller must state explicitly:
risk-free rate `r` and dividend yield `q` (both annual, continuous), time `t` in years. Output Greeks use the
usual market conventions: theta per CALENDAR DAY, vega per 1 volatility POINT (1%), IV as a percentage.
"""
from __future__ import annotations

import math

SQRT2 = math.sqrt(2.0)


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def price(s: float, k: float, t: float, r: float, q: float, sigma: float, is_call: bool) -> float:
    if t <= 0 or sigma <= 0:
        return max(0.0, (s - k) if is_call else (k - s))
    d1 = (math.log(s / k) + (r - q + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if is_call:
        return s * math.exp(-q * t) * _cdf(d1) - k * math.exp(-r * t) * _cdf(d2)
    return k * math.exp(-r * t) * _cdf(-d2) - s * math.exp(-q * t) * _cdf(-d1)


def greeks(s: float, k: float, t: float, r: float, q: float, sigma: float, is_call: bool) -> dict:
    if t <= 0 or sigma <= 0:
        return {"delta": (1.0 if s > k else 0.0) if is_call else (-1.0 if s < k else 0.0), "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    sq = sigma * math.sqrt(t)
    d1 = (math.log(s / k) + (r - q + 0.5 * sigma * sigma) * t) / sq
    d2 = d1 - sq
    eq, er = math.exp(-q * t), math.exp(-r * t)
    delta = eq * _cdf(d1) if is_call else -eq * _cdf(-d1)
    gamma = eq * _pdf(d1) / (s * sq)
    vega = s * eq * _pdf(d1) * math.sqrt(t) / 100.0
    common = -s * eq * _pdf(d1) * sigma / (2.0 * math.sqrt(t))
    theta = (common - r * k * er * _cdf(d2) + q * s * eq * _cdf(d1)) if is_call else (common + r * k * er * _cdf(-d2) - q * s * eq * _cdf(-d1))
    return {"delta": delta, "gamma": gamma, "theta": theta / 365.0, "vega": vega}


def implied_vol(target: float, s: float, k: float, t: float, r: float, q: float, is_call: bool) -> float | None:
    """IV in percent by bisection, or None when the quote is below intrinsic / above the no-arbitrage bound."""
    if t <= 0 or target <= 0 or s <= 0 or k <= 0:
        return None
    lo, hi = 1e-4, 5.0
    if target < price(s, k, t, r, q, lo, is_call) - 1e-9 or target > price(s, k, t, r, q, hi, is_call):
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if price(s, k, t, r, q, mid, is_call) > target:
            hi = mid
        else:
            lo = mid
    return round(0.5 * (lo + hi) * 100.0, 4)
