"""Fill the IV / delta holes of an option chain row list, so a strike never shows a blank IV when the market says what it must be.

An IV is blank when the leg did not trade, has no price (outside the downloaded window) or sits below intrinsic value (a deep in-the-money leg that traded stale). Three steps, each flagged:
  1. the other side of the same strike (put-call parity: a call and a put of one strike and expiry carry the same IV)        -> iv_from_other
  2. no price on either side: straight line between the nearest strikes that do have an IV (the nearest one's value beyond the ends) -> iv_est
Real IVs (from the leg's own price) are never changed.
"""
from __future__ import annotations

from app.options import bs

SIDES = (("CE", "PE"), ("PE", "CE"))


def _delta(spot: float, k: float, t: float, rate: float, iv: float, call: bool) -> float:
    return round(bs.greeks(spot, k, t, rate, 0.0, iv / 100, call)["delta"], 2)


def fill(rows: list[dict], spot: float, t: float, rate: float) -> list[dict]:
    """`rows`: [{"strike", "CE": leg|None, "PE": leg|None}], legs are dicts with "iv"/"delta". Edited in place and returned."""
    for r in rows:
        for o, other in SIDES:
            a, b = r.get(o), r.get(other)
            if a is not None and a.get("iv") is None and b is not None and b.get("iv"):
                a["iv"], a["iv_from_other"] = b["iv"], True
                a["delta"] = _delta(spot, r["strike"], t, rate, b["iv"], o == "CE")
    known = sorted((r["strike"], (r.get("CE") or {}).get("iv") or (r.get("PE") or {}).get("iv")) for r in rows
                   if (r.get("CE") or {}).get("iv") or (r.get("PE") or {}).get("iv"))
    if not known:
        return rows
    for r in rows:
        k = r["strike"]
        legs = [(o, r.get(o)) for o in ("CE", "PE") if r.get(o) is not None and r[o].get("iv") is None]
        if not legs:
            continue
        lo = [x for x in known if x[0] < k]
        hi = [x for x in known if x[0] > k]
        if lo and hi:
            (k0, v0), (k1, v1) = lo[-1], hi[0]
            iv = v0 + (v1 - v0) * (k - k0) / (k1 - k0)
        else:
            iv = (lo[-1] if lo else hi[0])[1]
        iv = round(iv, 1)
        for o, leg in legs:
            leg["iv"], leg["iv_est"] = iv, True
            leg["delta"] = _delta(spot, k, t, rate, iv, o == "CE")
    return rows
