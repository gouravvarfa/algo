"""Read-only Options endpoints: live option chain (Angel One) and loaded NSE Bhavcopy EOD data. No orders."""
from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3

from fastapi import APIRouter, HTTPException, Request

from app.options import bhavcopy, chain as oc, master
from app.providers.angelone_client import get_margin
from app.ops import flags as ops_flags
from app.ops.stats import tracked

router = APIRouter(prefix="/api/options")
provider = None  # set by app.main lifespan


_CACHE: dict = {}
CACHE_TTL = 600.0      # seconds: loaded history changes only when a loader runs; a restart or this TTL refreshes it


async def _cached(key, fn):
    """Run a slow read-only DB query in a worker thread (it must not block the event loop) and keep the answer briefly."""
    import time as _t
    hit = _CACHE.get(key)
    if hit and _t.time() - hit[0] < CACHE_TTL:
        return hit[1]
    val = await asyncio.to_thread(fn)
    _CACHE[key] = (_t.time(), val)
    return val


async def warm() -> None:
    """Fill the slow read-only caches in the background at server start so the first page load is instant."""
    for call in (lambda: bhav_summary(), lambda: sim_dates("NIFTY"), lambda: sim_dates("BANKNIFTY"), lambda: intraday_days("NIFTY"), lambda: intraday_days("BANKNIFTY")):
        try:
            await call()
        except Exception:
            pass


def _leg(leg: oc.Leg | None) -> dict | None:
    if leg is None:
        return None
    return {"symbol": leg.contract.symbol, "ltp": leg.ltp, "bid": leg.bid, "ask": leg.ask, "volume": leg.volume, "oi": leg.oi,
            "iv": leg.iv, "delta": leg.delta, "gamma": leg.gamma, "theta": leg.theta, "vega": leg.vega,
            "greeks_source": leg.greeks_source, "flags": leg.flags, "liquid": oc.liquid(leg)}


@router.get("/expiries")
async def expiries(symbol: str = "BANKNIFTY"):
    try:
        info = await asyncio.to_thread(master.load_underlying, symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"symbol": info.name, "expiries": [e.strftime("%d%b%Y").upper() for e in master.expiries(info)]}


@router.get("/chain")
async def chain(symbol: str = "BANKNIFTY", expiry: str = "nearest", strikes: int = 10, min_oi: int = 1000,
                min_volume: int = 100, max_spread_pct: float = 10.0):
    if provider is None:
        raise HTTPException(503, "broker provider not ready")
    try:
        info = await asyncio.to_thread(master.load_underlying, symbol)
        exp = master.pick_expiry(info, expiry)
        session = await provider.get_session()
        ch = await oc.fetch_chain(session, info, exp, max(1, min(strikes, 30)), min_oi=min_oi, min_volume=min_volume,
                                  max_spread_pct=max_spread_pct)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # broker/network failure: report, never fabricate
        raise HTTPException(502, f"chain fetch failed: {exc}")
    ce_oi = sum((r["CE"].oi or 0) for r in ch.rows.values() if r.get("CE"))
    pe_oi = sum((r["PE"].oi or 0) for r in ch.rows.values() if r.get("PE"))
    helpers = {}
    for side in ("CE", "PE"):
        helpers[side] = {"otm_100": oc.otm_by_points(ch, side, 100), "delta_30": oc.by_delta(ch, side, 0.30),
                         "premium_100": oc.nearest_premium(ch, side, 100)}
    lot = info.contracts[0].lot_size if info.contracts else None
    return {"symbol": ch.underlying, "expiry": ch.expiry.strftime("%d%b%Y").upper(), "spot": ch.spot, "vix": ch.vix,
            "atm": oc.atm_strike(ch), "lot_size": lot, "market_open": ch.market_open, "pcr": round(pe_oi / ce_oi, 3) if ce_oi else None,
            "fetched_at": ch.fetched_at.isoformat(timespec="seconds"), "violations": ch.violations, "stale": ch.stale,
            "warnings": ch.warnings, "helpers": helpers,
            "rows": [{"strike": k, "CE": _leg(v.get("CE")), "PE": _leg(v.get("PE"))} for k, v in sorted(ch.rows.items())]}


def _db() -> sqlite3.Connection:
    return bhavcopy._conn()


@router.get("/bhavcopy/summary")
async def bhav_summary():
    def q():
        with _db() as c:
            days = c.execute("SELECT trade_date, status, rows, rejected FROM load_log ORDER BY trade_date").fetchall()
            syms = c.execute("SELECT symbol, COUNT(*), COUNT(DISTINCT trade_date), COUNT(DISTINCT expiry), MIN(trade_date), MAX(trade_date) "
                             "FROM options_eod GROUP BY symbol").fetchall()
        return {"days": [{"date": d, "status": s, "rows": r, "rejected": j} for d, s, r, j in days],
                "symbols": [{"symbol": s, "rows": n, "days": d, "expiries": e, "from": a, "to": b} for s, n, d, e, a, b in syms]}
    return await _cached("bhav_summary", q)


@router.get("/bhavcopy/expiries")
async def bhav_expiries(symbol: str, date: str):
    with _db() as c:
        rows = c.execute("SELECT DISTINCT expiry FROM options_eod WHERE symbol=? AND trade_date=? ORDER BY expiry",
                         (symbol.upper(), date)).fetchall()
    return {"expiries": [r[0] for r in rows]}


@router.get("/bhavcopy/chain")
async def bhav_chain(symbol: str, date: str, expiry: str):
    rows = bhavcopy.chain_on(dt.date.fromisoformat(date), symbol.upper(), expiry)
    if not rows:
        raise HTTPException(404, "no Bhavcopy rows for that date/expiry (not loaded, or holiday)")
    by: dict[float, dict] = {}
    for strike, opt, close, settle, oi, vol, lot, und in rows:
        by.setdefault(strike, {})[opt] = {"close": close, "settle": settle, "oi": oi, "volume": vol}
    return {"underlying": rows[0][7], "lot_size": rows[0][6],
            "rows": [{"strike": k, "CE": v.get("CE"), "PE": v.get("PE")} for k, v in sorted(by.items())]}


@router.post("/bhavcopy/load")
async def bhav_load(start: str, end: str, symbols: str = "BANKNIFTY,NIFTY"):
    s, e = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    if e < s or (e - s).days > 120:
        raise HTTPException(400, "range must be 0-120 days")
    return await asyncio.to_thread(bhavcopy.load_range, s, e, tuple(symbols.split(",")), 1.0, bhavcopy.DB, lambda *_: None)


# ---------------------------------------------------------------- simulator (StockMock-style, EOD) + backtester
import json  # noqa: E402
import uuid  # noqa: E402

from pydantic import BaseModel  # noqa: E402

from app.options import bs, engine, strategy as strat, whatif  # noqa: E402

RATE = 0.065
SAVE_DIR = bhavcopy.ROOT.parent / "backtests"


@router.get("/sim/dates")
async def sim_dates(symbol: str = "NIFTY"):
    sym = symbol.upper()

    def q():
        with _db() as c:
            return {"dates": [r[0] for r in c.execute("SELECT DISTINCT trade_date FROM options_eod WHERE symbol=? ORDER BY 1", (sym,))]}
    return await _cached(("sim_dates", sym), q)


@router.get("/sim/chain")
async def sim_chain(date: str, symbol: str = "NIFTY", expiry: str | None = None):
    sym = symbol.upper()
    with _db() as c:
        exps = [r[0] for r in c.execute("SELECT DISTINCT expiry FROM options_eod WHERE symbol=? AND trade_date=? ORDER BY 1", (sym, date))]
        if not exps:
            raise HTTPException(404, f"no {sym} Bhavcopy rows on {date}")
        expiry = expiry if expiry in exps else exps[0]
        rows = c.execute("SELECT strike, opt, close, oi, change_oi, volume, lot_size, underlying FROM options_eod "
                         "WHERE symbol=? AND trade_date=? AND expiry=? ORDER BY strike", (sym, date, expiry)).fetchall()
    und, lot = rows[0][7], rows[0][6]
    d0, de = dt.date.fromisoformat(date), dt.date.fromisoformat(expiry)
    t = max(((de - d0).days + 0.25), 0.25) / 365.0          # close-to-15:30-expiry, at least a quarter day
    by: dict[float, dict] = {}
    for k, o, cl, oi, coi, vol, _, _ in rows:
        iv = bs.implied_vol(cl, und, k, t, RATE, 0.0, o == "CE") if cl and vol else None
        delta = None
        if iv:
            delta = round(bs.greeks(und, k, t, RATE, 0.0, iv / 100, o == "CE")["delta"], 2)
        by.setdefault(k, {})[o] = {"ltp": cl, "oi": oi, "chg_oi": coi, "volume": vol, "iv": round(iv, 1) if iv else None, "delta": delta}
    strikes = sorted(by)
    atm = min(strikes, key=lambda k: abs(k - und))
    ce_oi = sum((v.get("CE") or {}).get("oi") or 0 for v in by.values())
    pe_oi = sum((v.get("PE") or {}).get("oi") or 0 for v in by.values())
    pain = min(strikes, key=lambda x: sum(((v.get("CE") or {}).get("oi") or 0) * max(0, x - k) + ((v.get("PE") or {}).get("oi") or 0) * max(0, k - x)
                                          for k, v in by.items()))
    a = by[atm]
    return {"date": date, "expiry": expiry, "expiries": exps, "underlying": und, "lot_size": lot, "atm": atm,
            "dte": (de - d0).days, "pcr": round(pe_oi / ce_oi, 2) if ce_oi else None, "max_pain": pain,
            "straddle": round((a.get("CE") or {}).get("ltp", 0) + (a.get("PE") or {}).get("ltp", 0), 2),
            "atm_iv": (lambda v: round(sum(v) / len(v), 1) if v else None)([x for x in ((a.get("CE") or {}).get("iv"), (a.get("PE") or {}).get("iv")) if x]), "ce_oi": ce_oi, "pe_oi": pe_oi,
            "iv_note": f"IV/delta computed by Black-Scholes from the EOD close (r={RATE}, q=0); blank when the contract did not trade",
            "rows": [{"strike": k, "CE": by[k].get("CE"), "PE": by[k].get("PE")} for k in strikes]}


class Position(BaseModel):
    side: str
    opt: str
    strike: float
    expiry: str
    lots: int = 1
    lot_size: int
    entry: float
    entry_date: str


class MarkReq(BaseModel):
    date: str
    symbol: str = "NIFTY"
    positions: list[Position]


@router.post("/sim/mark")
async def sim_mark(req: MarkReq):
    """Value positions on `date` with that day's real close; an expired contract is valued at expiry settlement."""
    sym, out, pay = req.symbol.upper(), [], []
    with _db() as c:
        und_row = c.execute("SELECT underlying FROM options_eod WHERE symbol=? AND trade_date=? LIMIT 1", (sym, req.date)).fetchone()
        if not und_row:
            raise HTTPException(404, f"no data on {req.date}")
        und = und_row[0]
        for p in req.positions:
            qty, sign = p.lots * p.lot_size, (1 if p.side == "BUY" else -1)
            if p.expiry < req.date:
                r = c.execute("SELECT underlying FROM options_eod WHERE symbol=? AND trade_date=? LIMIT 1", (sym, p.expiry)).fetchone()
                if not r:
                    ltp, note = None, "expiry-day data not loaded"
                else:
                    ltp, note = (max(0.0, r[0] - p.strike) if p.opt == "CE" else max(0.0, p.strike - r[0])), "settled at expiry"
            else:
                r = c.execute("SELECT close, volume FROM options_eod WHERE symbol=? AND trade_date=? AND expiry=? AND strike=? AND opt=?",
                              (sym, req.date, p.expiry, p.strike, p.opt)).fetchone()
                ltp, note = (r[0], "" if r[1] else "not traded today (NSE close)") if r else (None, "contract missing in Bhavcopy")
            out.append({"ltp": ltp, "pnl": round((ltp - p.entry) * qty * sign, 2) if ltp is not None else None, "note": note})
            pay.append({"side": p.side, "opt": p.opt, "strike": p.strike, "entry": p.entry, "qty": qty})
    total = sum(x["pnl"] for x in out if x["pnl"] is not None)
    return {"underlying": und, "positions": out, "total": round(total, 2), "payoff": engine.payoff(pay, und) if pay else None}


class MarginReq(BaseModel):
    symbol: str = "NIFTY"
    positions: list[Position]          # open legs only; lots already include the simulator's multiplier


@router.post("/sim/margin")
@tracked("margin.calculate")
async def sim_margin(req: MarginReq):
    """Capital needed by the open legs. BUY legs: the premium paid (exact). SELL legs: Angel One's own margin calculator for the
    whole basket (SPAN + exposure with the hedge benefit), which only knows contracts that are still listed and gives TODAY's
    margin. Nothing is estimated: legs Angel cannot price are reported, and the margin is then left out."""
    sym = req.symbol.upper()
    if not ops_flags.get("feature.margin"):
        return {"premium_paid": 0, "premium_received": 0, "has_sell": False, "margin": None, "span": None, "exposure": None, "hedge_benefit": None,
                "source": None, "note": "the margin calculator is switched off by a developer", "unlisted": []}
    legs = [p for p in req.positions if p.lots > 0]
    premium_paid = round(sum(p.entry * p.lots * p.lot_size for p in legs if p.side == "BUY"), 2)
    premium_recv = round(sum(p.entry * p.lots * p.lot_size for p in legs if p.side == "SELL"), 2)
    out = {"premium_paid": premium_paid, "premium_received": premium_recv, "has_sell": any(p.side == "SELL" for p in legs),
           "margin": None, "span": None, "exposure": None, "hedge_benefit": None, "source": None, "note": None, "unlisted": []}
    if not legs:
        return out
    if not out["has_sell"]:
        out.update(margin=premium_paid, source="premium paid (buy-only basket)")
        return out
    if provider is None:
        out["note"] = "broker provider not ready"
        return out
    try:
        info = await asyncio.to_thread(master.load_underlying, sym)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    book = {(c.expiry.isoformat(), round(c.strike, 2), c.option_type): c for c in info.contracts}
    basket = []
    for p in legs:
        c = book.get((p.expiry, round(p.strike, 2), p.opt))
        if c is None:
            out["unlisted"].append(f"{p.opt} {p.strike:g} {p.expiry}")
            continue
        basket.append({"exchange": "NFO", "qty": p.lots * p.lot_size, "price": 0, "productType": "CARRYFORWARD",
                       "token": c.token, "tradeType": p.side, "orderType": "MARKET"})
    if out["unlisted"]:
        out["note"] = "Angel One's margin calculator only knows contracts that are still listed; expired legs cannot be margined, so no margin is shown"
        return out
    try:
        session = await provider.get_session()
        d = await get_margin(session, basket)
    except Exception as exc:  # broker/network failure: report, never fabricate
        out["note"] = f"margin calculator unavailable: {str(exc)[:120]}"
        return out
    comp = d.get("marginComponents") or {}
    out.update(margin=d.get("totalMarginRequired"), span=comp.get("spanMargin"), exposure=comp.get("exposureMargin") if "exposureMargin" in comp else None,
               hedge_benefit=comp.get("marginBenefit"), source="Angel One margin calculator (today's margin)", raw_components=comp)
    return out


class WhatIfReq(BaseModel):
    date: str
    symbol: str = "NIFTY"
    positions: list[Position]
    target_date: str | None = None      # default: the selected date
    iv_shift: float = 0.0               # volatility points added to every leg's IV
    spot_shift_pct: float = 0.0         # what-if spot = close * (1 + pct/100)


@router.post("/sim/whatif")
async def sim_whatif(req: WhatIfReq):
    """Target-date P&L curve + net Greeks. A Black-Scholes ESTIMATE on real EOD inputs (see app/options/whatif.py)."""
    sym = req.symbol.upper()
    try:
        on = dt.date.fromisoformat(req.date)
        target = dt.date.fromisoformat(req.target_date) if req.target_date else on
    except ValueError:
        raise HTTPException(422, "dates must be YYYY-MM-DD")
    if not -50 <= req.iv_shift <= 50 or not -20 <= req.spot_shift_pct <= 20:
        raise HTTPException(422, "iv_shift must be within ±50 vol points and spot_shift_pct within ±20%")
    if not req.positions:
        raise HTTPException(422, "no positions")
    live_exp = [dt.date.fromisoformat(p.expiry) for p in req.positions if p.expiry >= req.date]
    if target < on:
        raise HTTPException(422, "target date cannot be before the selected date")
    if live_exp and target > min(live_exp):
        raise HTTPException(422, f"target date must be on or before the nearest open expiry ({min(live_exp)})")
    with _db() as c:
        r = c.execute("SELECT underlying FROM options_eod WHERE symbol=? AND trade_date=? LIMIT 1", (sym, req.date)).fetchone()
        if not r:
            raise HTTPException(404, f"no data on {req.date}")
        spot, atm_cache, legs, warnings = r[0], {}, [], []

        def atm_iv(expiry: str) -> float | None:
            if expiry not in atm_cache:
                rows = c.execute("SELECT strike, opt, close, volume FROM options_eod WHERE symbol=? AND trade_date=? AND expiry=?",
                                 (sym, req.date, expiry)).fetchall()
                ivs = []
                if rows:
                    k_atm = min({x[0] for x in rows}, key=lambda k: abs(k - spot))
                    t = whatif.years_to_expiry(on, dt.date.fromisoformat(expiry))
                    for k, o, cl, vol in rows:
                        if k == k_atm and cl and vol:
                            iv = bs.implied_vol(cl, spot, k, t, whatif.RATE, whatif.DIV, o == "CE")
                            if iv:
                                ivs.append(iv)
                atm_cache[expiry] = sum(ivs) / len(ivs) if ivs else None
            return atm_cache[expiry]

        for p in req.positions:
            qty, exp = p.lots * p.lot_size, dt.date.fromisoformat(p.expiry)
            leg = {"side": p.side, "opt": p.opt, "strike": p.strike, "expiry": exp, "qty": qty, "entry": p.entry,
                   "iv": None, "iv_source": "none", "settled": None, "last": None}
            if p.expiry < req.date:
                u = c.execute("SELECT underlying FROM options_eod WHERE symbol=? AND trade_date=? LIMIT 1", (sym, p.expiry)).fetchone()
                if u:
                    leg["settled"] = max(0.0, u[0] - p.strike) if p.opt == "CE" else max(0.0, p.strike - u[0])
                else:
                    leg["last"] = None
                    warnings.append(f"{int(p.strike)}{p.opt}: expiry-day data not loaded; held at entry price")
            else:
                row = c.execute("SELECT close, volume FROM options_eod WHERE symbol=? AND trade_date=? AND expiry=? AND strike=? AND opt=?",
                                (sym, req.date, p.expiry, p.strike, p.opt)).fetchone()
                close, vol = (row[0], row[1]) if row else (None, None)
                leg["last"] = close
                leg["iv"], leg["iv_source"] = whatif.leg_iv(close, vol, spot, p.strike, whatif.years_to_expiry(on, exp), p.opt == "CE", atm_iv(p.expiry))
                if leg["iv_source"] == "atm_fallback":
                    warnings.append(f"{int(p.strike)}{p.opt}: own IV unavailable (not traded or out of bounds); ATM IV of that expiry used")
                elif leg["iv_source"] == "none":
                    warnings.append(f"{int(p.strike)}{p.opt}: no IV available; value held at its last real price, excluded from Greeks")
            legs.append(leg)
    n, lo, hi = 241, spot * 0.94, spot * 1.06               # same grid as engine.payoff, so both lines share the x axis
    xs = [lo + (hi - lo) * j / (n - 1) for j in range(n)]
    out = whatif.analyse(legs, spot, on, target, xs, req.iv_shift, req.spot_shift_pct)
    return {**out, "spot": spot, "date": req.date, "target_date": target.isoformat(), "iv_shift": req.iv_shift,
            "spot_shift_pct": req.spot_shift_pct, "warnings": warnings,
            "note": f"Theoretical Black-Scholes estimate (r={whatif.RATE}, q={whatif.DIV}) on the {req.date} close. Not market data."}


# ---------------------------------------------------------------- intraday 1-minute time travel (real Angel One candles)
from app.options import intraday  # noqa: E402

INTRADAY_DB = None   # None = intraday.DB; tests point this at a temp file


def _idb():
    return intraday.connect(INTRADAY_DB)


def _hm(t: str) -> str:
    try:
        return dt.datetime.strptime(t, "%H:%M").strftime("%H:%M")
    except ValueError:
        raise HTTPException(422, "time must be HH:MM")


@router.get("/intraday/days")
async def intraday_days(symbol: str = "NIFTY"):
    sym = symbol.upper()

    def q():
        with _idb() as c:
            return {"days": intraday.days(c, sym),
                    "note": "Real 1-minute candles from Angel One. Only contracts still listed at fetch time are available; no OI in 1-minute data."}
    return await _cached(("intraday_days", sym), q)


@router.get("/intraday/minutes")
async def intraday_minutes(date: str, symbol: str = "NIFTY"):
    with _idb() as c:
        m = intraday.minutes(c, symbol.upper(), date)
    if not m:
        raise HTTPException(404, f"no intraday index data on {date}")
    return {"date": date, "minutes": m}


def _prev_oi(sym: str, date: str, expiry: str) -> tuple[dict, str | None, int | None]:
    """OI per (strike, opt) from the previous trading day's Bhavcopy (1-minute candles carry no OI)."""
    with _db() as c:
        r = c.execute("SELECT MAX(trade_date) FROM options_eod WHERE symbol=? AND trade_date<? AND expiry=?", (sym, date, expiry)).fetchone()
        prev = r[0] if r else None
        if not prev:
            return {}, None, None
        rows = c.execute("SELECT strike, opt, oi, lot_size FROM options_eod WHERE symbol=? AND trade_date=? AND expiry=?", (sym, prev, expiry)).fetchall()
    return {(k, o): oi for k, o, oi, _ in rows}, prev, (rows[0][3] if rows else None)


@router.get("/intraday/chain")
async def intraday_chain(date: str, time: str, symbol: str = "NIFTY", expiry: str | None = None):
    sym, hm = symbol.upper(), _hm(time)
    with _idb() as c:
        sp = intraday.spot_at(c, sym, date, hm)
        if not sp:
            raise HTTPException(404, f"no index minute at or before {date} {hm}")
        exps = intraday.expiries_on(c, sym, date)
        if not exps:
            raise HTTPException(404, f"no option minutes on {date}")
        expiry = expiry if expiry in exps else exps[0]
        rows = intraday.chain_at(c, sym, date, hm, expiry)
    spot, spot_ts = sp
    oi, oi_day, lot = _prev_oi(sym, date, expiry)
    if lot is None:
        try:
            info = await asyncio.to_thread(master.load_underlying, sym)
            lot = next((k.lot_size for k in info.contracts if k.expiry.isoformat() == expiry), None)
        except Exception:
            lot = None
    now = dt.datetime.strptime(f"{date} {hm}", "%Y-%m-%d %H:%M")
    exp_close = dt.datetime.strptime(expiry, "%Y-%m-%d").replace(hour=15, minute=30)
    t = max((exp_close - now).total_seconds(), 60) / (365 * 86400)
    strikes = sorted({k for k, _ in rows} | {k for k, _ in oi})
    out = []
    for k in strikes:
        rec = {}
        for o in ("CE", "PE"):
            r = rows.get((k, o))
            if not r and (k, o) not in oi:
                rec[o] = None
                continue
            ltp = r["ltp"] if r else None
            iv = bs.implied_vol(ltp, spot, k, t, RATE, 0.0, o == "CE") if ltp else None
            age = int((now - dt.datetime.strptime(f"{date} {r['ts']}", "%Y-%m-%d %H:%M")).total_seconds() // 60) if r else None
            rec[o] = {"ltp": ltp, "ts": r["ts"] if r else None, "age_min": age, "volume": r["volume"] if r else 0,
                      "oi": oi.get((k, o)), "chg_oi": None, "iv": round(iv, 1) if iv else None,
                      "delta": round(bs.greeks(spot, k, t, RATE, 0.0, iv / 100, o == "CE")["delta"], 2) if iv else None}
        out.append({"strike": k, **rec})
    if not out:
        raise HTTPException(404, f"no strikes for {expiry} on {date}")
    atm = min(strikes, key=lambda k: abs(k - spot))
    a = next(r for r in out if r["strike"] == atm)
    ce_oi = sum((r.get("CE") or {}).get("oi") or 0 for r in out)
    pe_oi = sum((r.get("PE") or {}).get("oi") or 0 for r in out)
    pain = min(strikes, key=lambda x: sum(((r.get("CE") or {}).get("oi") or 0) * max(0, x - r["strike"]) +
                                          ((r.get("PE") or {}).get("oi") or 0) * max(0, r["strike"] - x) for r in out)) if ce_oi or pe_oi else None
    ivs = [x for x in ((a.get("CE") or {}).get("iv"), (a.get("PE") or {}).get("iv")) if x]
    straddle = ((a.get("CE") or {}).get("ltp") or 0) + ((a.get("PE") or {}).get("ltp") or 0) if (a.get("CE") or {}).get("ltp") and (a.get("PE") or {}).get("ltp") else None
    return {"mode": "intraday", "date": date, "time": hm, "spot_ts": spot_ts, "expiry": expiry, "expiries": exps, "underlying": spot,
            "lot_size": lot, "atm": atm, "dte": (dt.date.fromisoformat(expiry) - dt.date.fromisoformat(date)).days,
            "pcr": round(pe_oi / ce_oi, 2) if ce_oi else None, "max_pain": pain, "straddle": round(straddle, 2) if straddle else None,
            "atm_iv": round(sum(ivs) / len(ivs), 1) if ivs else None, "oi_day": oi_day,
            "oi_note": f"OI is the NSE Bhavcopy OI of {oi_day} (1-minute candles carry no OI)" if oi_day else "no previous-day OI available",
            "iv_note": "IV/delta: Black-Scholes estimate from the last 1-minute trade at or before this minute (r=0.065, q=0)",
            "rows": out}


class IntradayMarkReq(BaseModel):
    date: str
    time: str
    symbol: str = "NIFTY"
    positions: list[Position]


@router.post("/intraday/mark")
async def intraday_mark(req: IntradayMarkReq):
    """Value positions at a minute with the last real trade at or before it; expired contracts at settlement."""
    sym, hm = req.symbol.upper(), _hm(req.time)
    with _idb() as c:
        sp = intraday.spot_at(c, sym, req.date, hm)
        if not sp:
            raise HTTPException(404, f"no index minute at or before {req.date} {hm}")
        out, pay = [], []
        for p in req.positions:
            qty, sign = p.lots * p.lot_size, (1 if p.side == "BUY" else -1)
            if p.expiry < req.date:
                with _db() as b:
                    r = b.execute("SELECT underlying FROM options_eod WHERE symbol=? AND trade_date=? LIMIT 1", (sym, p.expiry)).fetchone()
                if r:
                    ltp, note = (max(0.0, r[0] - p.strike) if p.opt == "CE" else max(0.0, p.strike - r[0])), "settled at expiry"
                else:
                    ltp, note = None, "expiry-day data not loaded"
            else:
                r = intraday.price_at(c, sym, p.expiry, p.strike, p.opt, req.date, hm)
                if r:
                    age = int((dt.datetime.strptime(hm, "%H:%M") - dt.datetime.strptime(r[1], "%H:%M")).total_seconds() // 60)
                    ltp, note = r[0], (f"last trade {r[1]} ({age} min ago)" if age > 2 else "")
                else:
                    ltp, note = None, "no trade yet today at this minute"
            out.append({"ltp": ltp, "pnl": round((ltp - p.entry) * qty * sign, 2) if ltp is not None else None, "note": note})
            pay.append({"side": p.side, "opt": p.opt, "strike": p.strike, "entry": p.entry, "qty": qty})
    total = sum(x["pnl"] for x in out if x["pnl"] is not None)
    return {"underlying": sp[0], "spot_ts": sp[1], "positions": out, "total": round(total, 2), "payoff": engine.payoff(pay, sp[0]) if pay else None}


# ---------------------------------------------------------------- simulator stop-loss check (real data only)
class SLPos(BaseModel):
    id: str
    side: str
    opt: str
    strike: float
    expiry: str
    entry_date: str
    entry_time: str | None = None      # None = entered at that day's EOD close
    sl: float                          # stop price in rupees


class SLReq(BaseModel):
    symbol: str = "NIFTY"
    date: str
    time: str | None = None            # None = EOD view of `date` (the whole day counts)
    positions: list[SLPos]


def first_sl_hit(p: SLPos, upto_date: str, upto_time: str | None, bars_1m, bar_eod, days: list[str]) -> dict | None:
    """First real observation at which the stop was touched, scanning from just after entry up to the viewed moment.
    Per day: 1-minute candles of the contract when they exist (exact minute), otherwise the NSE Bhavcopy day high/low
    (the entry day itself is skipped when there are no minutes, because the entry was at that day's close).
    Fill = the stop price, or the bar's open when it gapped through the stop."""
    sell = p.side == "SELL"
    for d in days:
        if d < p.entry_date or d > upto_date or d > p.expiry:
            continue
        mins = bars_1m(d)
        if mins:
            for ts, o, h, l, c in mins:
                if d == p.entry_date and p.entry_time and ts <= p.entry_time:
                    continue
                if d == p.entry_date and not p.entry_time:
                    break                                   # entered at the close: nothing after it that day
                if d == upto_date and upto_time and ts > upto_time:
                    break
                if (h >= p.sl) if sell else (l <= p.sl):
                    gap = (o >= p.sl) if sell else (o <= p.sl)
                    return {"price": o if gap else p.sl, "date": d, "time": ts, "how": "SL (gap open)" if gap else "SL", "source": "1-min"}
            continue
        if d == p.entry_date or (d == upto_date and upto_time):
            continue                                        # no minutes: entry-day close / partial day cannot be split
        r = bar_eod(d)
        if not r or not r[1] or r[1] <= 0:
            continue                                        # missing or untraded day: nothing to check, nothing invented
        o, h, l = r
        if (h >= p.sl) if sell else (l <= p.sl):
            gap = o and ((o >= p.sl) if sell else (o <= p.sl))
            return {"price": o if gap else p.sl, "date": d, "time": None, "how": "SL (gap open)" if gap else "SL", "source": "EOD high/low"}
    return None


@router.post("/sim/sl_check")
async def sim_sl_check(req: SLReq):
    sym = req.symbol.upper()
    hm = _hm(req.time) if req.time else None
    out = {}
    with _db() as b, _idb() as ic:
        for p in req.positions:
            days = [r[0] for r in b.execute("SELECT DISTINCT trade_date FROM options_eod WHERE symbol=? AND expiry=? AND strike=? AND opt=? AND trade_date BETWEEN ? AND ? ORDER BY 1",
                                            (sym, p.expiry, p.strike, p.opt, p.entry_date, req.date))]
            days = sorted(set(days) | set(r[0] for r in ic.execute(
                "SELECT DISTINCT substr(ts,1,10) FROM candles WHERE symbol=? AND expiry=? AND strike=? AND opt=? AND ts BETWEEN ? AND ?",
                (sym, p.expiry, p.strike, p.opt, p.entry_date + " 00:00", req.date + " 23:59"))))
            bars_1m = lambda d, p=p: [(ts[11:], o, h, l, c) for ts, o, h, l, c in ic.execute(
                "SELECT ts, open, high, low, close FROM candles WHERE symbol=? AND expiry=? AND strike=? AND opt=? AND ts BETWEEN ? AND ? ORDER BY ts",
                (sym, p.expiry, p.strike, p.opt, d + " 00:00", d + " 23:59"))]
            bar_eod = lambda d, p=p: b.execute("SELECT open, high, low FROM options_eod WHERE symbol=? AND trade_date=? AND expiry=? AND strike=? AND opt=?",
                                               (sym, d, p.expiry, p.strike, p.opt)).fetchone()
            out[p.id] = first_sl_hit(p, req.date, hm, bars_1m, bar_eod, days)
    return {"hits": out, "note": "Stops are checked on real 1-minute candles where available, otherwise on the NSE day high/low. No slippage."}


# ---------------------------------------------------------------- saved strategies (P5)
from app.options import store  # noqa: E402

STORE_DB = None   # None = store.DB; tests point this at a temp file


def _store():
    return store.connect(STORE_DB)


def _legacy_owner(request: Request) -> bool:
    u = getattr(request.state, "user", None)
    return u is not None and u["role"] == "developer" and u.get("created_by") is None


def _uid(request: Request) -> str:
    """The signed-in user's id ('' with no login). Saved strategies and backtests are private to their owner; what was saved before accounts
    existed is adopted by the owner account the first time it asks."""
    u = getattr(request.state, "user", None)
    if u is None:
        return ""
    if _legacy_owner(request):
        with _store() as c:
            store.adopt(c, u["id"])
    return u["id"]


def _validate_for_save(name: str, strategy: dict) -> None:
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise HTTPException(422, {"errors": ["name: 1-80 characters"]})
    _, errs = strat.validate(strategy if isinstance(strategy, dict) else {})
    if errs:
        raise HTTPException(422, {"errors": errs})


class SaveReq(BaseModel):
    name: str
    state: dict
    strategy: dict
    overwrite: bool = False


@router.get("/strategies")
async def strategies_list(request: Request):
    uid = _uid(request)
    with _store() as c:
        return store.list_all(c, uid)


@router.get("/strategies/{sid}")
async def strategies_get(sid: str, request: Request):
    uid = _uid(request)
    with _store() as c:
        r = store.get(c, sid, uid)
    if not r:
        raise HTTPException(404, "strategy not found")
    return r


@router.post("/strategies")
async def strategies_save(req: SaveReq, request: Request):
    """Validates the strategy with the same validator the backtester uses, then saves it. Existing names need overwrite=true."""
    _validate_for_save(req.name, req.strategy)
    try:
        uid = _uid(request)
        with _store() as c:
            rec, created = store.save(c, req.name.strip(), req.state, req.strategy, req.overwrite, owner=uid)
    except KeyError:
        raise HTTPException(409, f"a strategy named '{req.name.strip()}' already exists")
    return {**rec, "created": created}


@router.delete("/strategies/{sid}")
async def strategies_delete(sid: str, request: Request):
    uid = _uid(request)
    with _store() as c:
        if not store.delete(c, sid, uid):
            raise HTTPException(404, "strategy not found")
    return {"deleted": sid}


class ImportReq(BaseModel):
    items: list[dict]


@router.post("/strategies/import")
async def strategies_import(req: ImportReq, request: Request):
    """Copy browser-saved strategies to the server. Never overwrites an existing name; invalid ones are reported, not saved."""
    done, skipped = [], []
    uid = _uid(request)
    with _store() as c:
        for it in req.items[:200]:
            name = str(it.get("name") or "").strip()
            try:
                _validate_for_save(name, it.get("strategy"))
            except HTTPException as e:
                skipped.append({"name": name or "(unnamed)", "reason": "; ".join(e.detail["errors"]) if isinstance(e.detail, dict) else str(e.detail)})
                continue
            try:
                store.save(c, name, it.get("state") or {}, it["strategy"], False, "browser-import", owner=uid)
                done.append(name)
            except KeyError:
                skipped.append({"name": name, "reason": "a server strategy with this name already exists (kept both: the browser copy is untouched)"})
    return {"imported": done, "skipped": skipped}


@router.post("/backtest")
async def backtest(body: dict, request: Request):
    s, errs = strat.validate(body)
    if errs:
        raise HTTPException(422, {"errors": errs})
    res = await asyncio.to_thread(engine.run, s)
    res["id"] = dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    res["input"] = body
    res["owner"] = _uid(request)
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    (SAVE_DIR / f"{res['id']}.json").write_text(json.dumps(res, default=str), encoding="utf-8")
    return json.loads(json.dumps(res, default=str))


@router.get("/backtests")
async def backtests(request: Request):
    out = []
    uid, legacy = _uid(request), _legacy_owner(request)
    for f in sorted(SAVE_DIR.glob("*.json"), reverse=True)[:600] if SAVE_DIR.exists() else []:
        r = json.loads(f.read_text(encoding="utf-8"))
        if not _mine(r, uid, legacy):
            continue
        if len(out) >= 50:
            break
        m = r["metrics"]
        out.append({"id": r["id"], "name": r["strategy"]["name"], "range": f"{r['strategy']['start']} → {r['strategy']['end']}",
                    "trades": m.get("trades", 0), "total": m.get("total"),
                    # additive: stored values only (created time is encoded in the run id)
                    "symbol": r["strategy"].get("symbol"), "max_drawdown": m.get("max_drawdown"), "win_rate": m.get("win_rate"),
                    "created": f"{r['id'][:4]}-{r['id'][4:6]}-{r['id'][6:8]} {r['id'][9:11]}:{r['id'][11:13]}:{r['id'][13:15]}"})
    return out


def _mine(run: dict, uid: str, legacy_owner: bool) -> bool:
    """A run is private to the user who made it; a run from before accounts existed (no owner field) belongs to the owner account."""
    return run.get("owner", "") == uid or (legacy_owner and "owner" not in run)


def _load_run(bid: str, uid: str = "", legacy_owner: bool = False) -> dict:
    f = SAVE_DIR / f"{bid}.json"
    if not bid.replace("-", "").isalnum() or not f.exists():
        raise HTTPException(404, f"saved backtest {bid} not found")
    run = json.loads(f.read_text(encoding="utf-8"))
    if not _mine(run, uid, legacy_owner):
        raise HTTPException(404, f"saved backtest {bid} not found")        # somebody else's run looks exactly like one that does not exist
    return run


COMPARE_KEYS = ("name", "symbol", "start", "end", "expiry", "entry_days_before_expiry", "exit_days_before_expiry",
                "overall_sl", "overall_target", "slippage_pct", "skip_untraded", "legs", "charges", "on_leg_exit", "sl_to_cost", "lock_profit", "vix_filter")


@router.get("/backtests/compare")
async def backtest_compare(ids: str, request: Request):
    """Read-only comparison of 2-4 saved runs. Metrics are recomputed from each run's stored trades with the same
    engine.metrics() definitions, so runs saved before a metric existed are compared like-for-like. Nothing is rerun."""
    want = [i for i in ids.split(",") if i]
    if not 2 <= len(want) <= 4 or len(set(want)) != len(want):
        raise HTTPException(400, "pick 2-4 different saved runs")
    uid, legacy = _uid(request), _legacy_owner(request)
    runs = [_load_run(i, uid, legacy) for i in want]
    out = [{"id": r["id"], "strategy": {k: r["strategy"].get(k) for k in COMPARE_KEYS}, "metrics": engine.metrics(r["trades"]),
            "skipped": len(r.get("skipped", []))} for r in runs]
    diff = [k for k in COMPARE_KEYS if len({json.dumps(o["strategy"].get(k), sort_keys=True) for o in out}) > 1]
    ranges = {(o["strategy"]["start"], o["strategy"]["end"]) for o in out}
    return {"runs": out, "differences": diff, "same_range": len(ranges) == 1}


@router.get("/backtests/{bid}")
async def backtest_get(bid: str, request: Request):
    return _load_run(bid, _uid(request), _legacy_owner(request))


# ---------------------------------------------------------------- live option chain (Angel One WebSocket ticks)
live_engine = None          # set by app.main: the PaperEngine whose TickFeed and index_quote are shared (one WebSocket for the whole app)
LIVE_STRIKES = 15           # ATM +/- this many strikes are subscribed per expiry
LIVE_MAX_TOKENS = 400       # safety cap well under Angel One's per-connection token limit
_LIVE_SUBS: set[str] = set()


def _live_ready():
    if live_engine is None:
        raise HTTPException(503, "live feed not ready")
    return live_engine.feed


def _sub(feed, token: str) -> None:
    if token in _LIVE_SUBS or len(_LIVE_SUBS) >= LIVE_MAX_TOKENS:
        return
    feed.subscribe("NFO", token)
    _LIVE_SUBS.add(token)


@router.get("/live/chain")
@tracked("options.live_chain")
async def live_chain(symbol: str = "NIFTY", expiry: str | None = None):
    """Today's chain from live ticks. A strike with no tick yet (just subscribed, or market closed) shows its last stored 1-minute
    close of today, labelled `stored`; nothing is filled or estimated. OI = previous day's Bhavcopy (ticks carry no OI)."""
    if not ops_flags.get("feature.live_chain"):
        raise HTTPException(503, "the live option chain is switched off by a developer")
    feed = _live_ready()
    sym = symbol.upper()
    from app.paper_trading.engine import INDEX_TOKENS, now_ist
    ex_i, tok_i = INDEX_TOKENS[sym]
    feed.subscribe(ex_i, tok_i)
    try:
        q = await live_engine.index_quote(sym)
    except Exception as exc:
        raise HTTPException(502, f"index quote unavailable: {exc}")
    spot = q["price"]
    if not spot:
        raise HTTPException(502, "no index price yet")
    info = await asyncio.to_thread(master.load_underlying, sym)
    now = now_ist()
    exps = [e.isoformat() for e in master.expiries(info)][:6]
    if not exps:
        raise HTTPException(404, "no listed expiries")
    expiry = expiry if expiry in exps else exps[0]
    cons = [c for c in info.contracts if c.expiry.isoformat() == expiry]
    strikes_all = sorted({c.strike for c in cons})
    atm = min(strikes_all, key=lambda k: abs(k - spot))
    ai = strikes_all.index(atm)
    strikes = strikes_all[max(0, ai - LIVE_STRIKES): ai + LIVE_STRIKES + 1]
    book = {(c.strike, c.option_type): c for c in cons if c.strike in strikes}
    for c in book.values():
        _sub(feed, c.token)
    today = now.date().isoformat()
    with _idb() as cdb:
        stored = intraday.chain_at(cdb, sym, today, "15:30", expiry)
    oi, oi_day, _lot = _prev_oi(sym, today, expiry)
    lot = next(iter(book.values())).lot_size if book else None
    exp_close = dt.datetime.combine(dt.date.fromisoformat(expiry), dt.time(15, 30))
    t = max((exp_close - now.replace(tzinfo=None)).total_seconds(), 60) / (365 * 86400)
    out, live_n = [], 0
    for k in strikes:
        rec = {}
        for o in ("CE", "PE"):
            c = book.get((k, o))
            if c is None:
                rec[o] = None
                continue
            tick = feed.latest.get(c.token)
            if tick is not None and tick.exch_ts.date() == now.date():
                ltp, src, age_s, ts = tick.ltp, "live", round(feed.age(c.token) or 0, 1), tick.exch_ts.strftime("%H:%M:%S")
                live_n += 1
            elif (k, o) in stored:
                r = stored[(k, o)]
                ltp, src, age_s, ts = r["ltp"], "stored", None, r["ts"]
            else:
                ltp, src, age_s, ts = None, None, None, None
            iv = bs.implied_vol(ltp, spot, k, t, RATE, 0.0, o == "CE") if ltp else None
            rec[o] = {"ltp": ltp, "src": src, "age_s": age_s, "ts": ts, "age_min": 0, "volume": None, "oi": oi.get((k, o)), "chg_oi": None,
                      "iv": round(iv, 1) if iv else None,
                      "delta": round(bs.greeks(spot, k, t, RATE, 0.0, iv / 100, o == "CE")["delta"], 2) if iv else None}
        out.append({"strike": k, **rec})
    a = next(r for r in out if r["strike"] == atm)
    ce_oi = sum((r.get("CE") or {}).get("oi") or 0 for r in out)
    pe_oi = sum((r.get("PE") or {}).get("oi") or 0 for r in out)
    ivs = [x for x in ((a.get("CE") or {}).get("iv"), (a.get("PE") or {}).get("iv")) if x]
    ca, pa = (a.get("CE") or {}).get("ltp"), (a.get("PE") or {}).get("ltp")
    return {"mode": "live", "date": today, "time": now.strftime("%H:%M:%S"), "expiry": expiry, "expiries": exps, "underlying": spot,
            "spot_live": q.get("live"), "spot_change": q.get("change"), "spot_pct": q.get("pct"), "prev_close": q.get("prev_close"),
            "lot_size": lot, "atm": atm, "dte": (dt.date.fromisoformat(expiry) - now.date()).days,
            "pcr": round(pe_oi / ce_oi, 2) if ce_oi else None, "max_pain": None, "straddle": round(ca + pa, 2) if ca and pa else None,
            "atm_iv": round(sum(ivs) / len(ivs), 1) if ivs else None, "oi_day": oi_day, "live_legs": live_n, "feed_connected": feed.connected,
            "oi_note": f"OI is the NSE Bhavcopy OI of {oi_day} (live ticks carry no OI)" if oi_day else "no previous-day OI available",
            "rows": out}


class LiveMarkReq(BaseModel):
    symbol: str = "NIFTY"
    positions: list[Position]


@router.post("/live/mark")
async def live_mark(req: LiveMarkReq):
    """Value open legs at their latest live tick (legs are subscribed on first sight). No tick yet = no price, never a guess."""
    feed = _live_ready()
    sym = req.symbol.upper()
    info = await asyncio.to_thread(master.load_underlying, sym)
    book = {(c.expiry.isoformat(), round(c.strike, 2), c.option_type): c for c in info.contracts}
    q = await live_engine.index_quote(sym)
    out, pay = [], []
    for p in req.positions:
        qty, sign = p.lots * p.lot_size, (1 if p.side == "BUY" else -1)
        c = book.get((p.expiry, round(p.strike, 2), p.opt))
        ltp, note = None, ""
        if c is None:
            note = "contract not listed any more"
        else:
            _sub(feed, c.token)
            tick = feed.latest.get(c.token)
            if tick is None:
                note = "waiting for the first live tick"
            else:
                ltp = tick.ltp
        out.append({"ltp": ltp, "pnl": round((ltp - p.entry) * qty * sign, 2) if ltp is not None else None, "note": note})
        pay.append({"side": p.side, "opt": p.opt, "strike": p.strike, "entry": p.entry, "qty": qty})
    total = sum(x["pnl"] for x in out if x["pnl"] is not None)
    und = q.get("price")
    return {"underlying": und, "positions": out, "total": round(total, 2), "payoff": engine.payoff(pay, und) if pay and und else None}
