"""
Option contracts, the live option chain and the margin calculator from UPSTOX, in the same shapes the Angel One versions give (app/options/master.py
UnderlyingInfo and app/options/chain.py Chain / Leg), so the Option Chain page, the simulator's LIVE mode and everything that reads them need no change.
Used only when the data source is Upstox (DATA_SOURCE=upstox). Read only: nothing here places an order.

Upstox gives a whole expiry in ONE call (option/chain): per strike the call and the put with ltp, volume, open interest, best bid / ask and its own Greeks
(delta, gamma, theta, vega, IV). The contract list (option/contract) gives the expiries, lot size and tick size. Differences from Angel One, stated:
- there is no feed time per strike, so the staleness check cannot be done (a warning says so while the market is open);
- the Greeks are Upstox's ("broker"); a strike with no IV from Upstox falls back to the local Black-Scholes calculation, flagged, exactly like the Angel One path.
"""
from __future__ import annotations

import datetime as dt
import time

from app.options import bs, chain as oc
from app.options.master import EXPIRY_TIME, IST, OptionContract, UnderlyingInfo
from app.providers import upstox_client

SPOT_KEYS = {"NIFTY": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank"}
VIX_KEY = "NSE_INDEX|India VIX"
CONTRACTS_TTL_S = 6 * 3600
_infos: dict[str, tuple[float, UnderlyingInfo]] = {}


def contract_symbol(name: str, expiry: dt.date, strike: float, option_type: str) -> str:
    """BANKNIFTY27OCT2654000CE: the shape Angel One's master uses, whatever the data source."""
    return f"{name}{expiry:%d%b%y}{strike:g}{option_type}".upper()


async def load_underlying(provider, name: str) -> UnderlyingInfo:
    """Every live option contract of one index (NIFTY or BANKNIFTY), kept for a few hours."""
    name = name.upper()
    key = SPOT_KEYS.get(name)
    if key is None:
        raise ValueError(f"index '{name}' is not available on Upstox here: use NIFTY or BANKNIFTY")
    hit = _infos.get(name)
    if hit and time.monotonic() - hit[0] < CONTRACTS_TTL_S:
        return hit[1]
    rows = await provider.call(lambda token: upstox_client.get_option_contracts(token, key))
    contracts: list[OptionContract] = []
    for r in rows:
        try:
            if r.get("instrument_type") not in ("CE", "PE") or (r.get("underlying_symbol") or "").upper() != name:
                continue
            expiry, strike, opt = dt.date.fromisoformat(r["expiry"]), float(r["strike_price"]), r["instrument_type"]
            contracts.append(OptionContract(symbol=contract_symbol(name, expiry, strike, opt), token=r["instrument_key"], name=name, strike=strike,
                                            expiry=expiry, option_type=opt, lot_size=int(r.get("lot_size") or 0), tick_size=float(r.get("tick_size") or 0) / 100.0))
        except (KeyError, ValueError, TypeError):
            continue                                      # one odd row must not lose the list
    if not contracts:
        raise ValueError(f"Upstox lists no option contracts for {name} right now")
    contracts.sort(key=lambda c: (c.expiry, c.strike, c.option_type))
    info = UnderlyingInfo(name, key, name, VIX_KEY, contracts, f"Upstox {dt.datetime.now(IST).date().isoformat()}")
    _infos[name] = (time.monotonic(), info)
    return info


def _int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _price(v) -> float | None:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


async def fetch_chain(provider, info: UnderlyingInfo, expiry: dt.date, n_strikes: int = 10, max_age: float = 5.0, min_oi: int = 1000, min_volume: int = 100,
                      max_spread_pct: float = 10.0, rate: float = 0.065, div: float = 0.0, now: dt.datetime | None = None) -> oc.Chain:
    """The same checks as chain.fetch_chain: HARD violations (zero price, crossed quote, a missing side), ILLIQ flags, Greeks with a calculated fallback."""
    now = now or dt.datetime.now(IST)
    by_key = {c.token: c for c in info.contracts if c.expiry == expiry}
    if not by_key:
        raise ValueError(f"no contracts for expiry {expiry}")
    rows = await provider.call(lambda token: upstox_client.get_option_chain(token, info.spot_token, expiry.isoformat()))
    if not rows:
        raise RuntimeError(f"Upstox sent no chain for {info.name} {expiry}")
    spot = next((float(r["underlying_spot_price"]) for r in rows if r.get("underlying_spot_price")), 0.0)
    if spot <= 0:
        raise RuntimeError(f"no spot price for {info.name}")
    vix = None
    if info.vix_token:
        try:
            vix = (await provider.call(lambda token: upstox_client.get_ltp(token, [info.vix_token]))).get(info.vix_token)
        except Exception:                                   # the VIX is a nicety: the chain does not wait for it
            vix = None
    strikes = oc.window_strikes([float(r["strike_price"]) for r in rows], spot, n_strikes)
    open_now = oc.market_open(now)
    expiry_dt = dt.datetime.combine(expiry, EXPIRY_TIME, tzinfo=IST)
    t_years = max((expiry_dt - now).total_seconds(), 0.0) / (365.0 * 86400.0)
    violations: list[str] = []
    warnings: list[str] = []
    table: dict[float, dict[str, oc.Leg]] = {}
    for r in rows:
        strike = float(r["strike_price"])
        if strike not in strikes:
            continue
        sides = table.setdefault(strike, {})
        for opt, field in (("CE", "call_options"), ("PE", "put_options")):
            side = r.get(field)
            if not side:
                violations.append(f"MISSING_STRIKE {info.name} {expiry:%d%b%Y} {strike:g}{opt}: Upstox sent no {opt} for it")
                continue
            c = by_key.get(side.get("instrument_key")) or OptionContract(contract_symbol(info.name, expiry, strike, opt), str(side.get("instrument_key")), info.name, strike, expiry, opt, 0, 0.0)
            leg = oc.Leg(contract=c)
            sides[opt] = leg
            md, gr = side.get("market_data") or {}, side.get("option_greeks") or {}
            leg.ltp = float(md.get("ltp") or 0)
            leg.bid, leg.ask = _price(md.get("bid_price")), _price(md.get("ask_price"))
            leg.volume, leg.oi = _int(md.get("volume")), _int(md.get("oi"))
            if leg.ltp <= 0:
                violations.append(f"BAD_PRICE {c.symbol}: LTP {leg.ltp}")
                leg.flags.append("BAD_PRICE")
            if leg.bid is not None and leg.ask is not None and leg.bid > leg.ask:
                violations.append(f"CROSSED {c.symbol}: bid {leg.bid} > ask {leg.ask}")
                leg.flags.append("CROSSED")
            illiq = []
            if leg.oi < min_oi:
                illiq.append("low OI")
            if leg.volume < min_volume:
                illiq.append("low volume")
            sp = leg.spread_pct
            if sp is None:
                illiq.append("no two-sided quote")
            elif sp > max_spread_pct:
                illiq.append(f"spread {sp}%")
            if illiq:
                leg.flags.append("ILLIQ(" + ", ".join(illiq) + ")")
            if gr.get("iv"):
                leg.iv = float(gr["iv"])
                leg.delta, leg.gamma, leg.theta, leg.vega = (float(gr.get(k) or 0.0) for k in ("delta", "gamma", "theta", "vega"))
                leg.greeks_source = "broker"
            else:                                           # no IV from Upstox for this strike: Black-Scholes from its own price, flagged as calculated
                ref = leg.mid or (leg.ltp if leg.ltp > 0 else None)
                iv = bs.implied_vol(ref, spot, strike, t_years, rate, div, opt == "CE") if ref else None
                if iv:
                    g = bs.greeks(spot, strike, t_years, rate, div, iv / 100.0, opt == "CE")
                    leg.iv, leg.delta, leg.gamma, leg.theta, leg.vega = iv, g["delta"], g["gamma"], g["theta"], g["vega"]
                    leg.greeks_source = "calc"
                    leg.flags.append(f"GREEKS_CALC(r={rate:g},q={div:g})")
    if open_now:
        warnings.append("Upstox does not send a feed time for each strike, so how fresh each price is cannot be checked here.")
    else:
        warnings.append("Market is closed (by the clock): values are the LAST traded prices; the staleness check is not applied.")
    if not any(leg.greeks_source == "broker" for s in table.values() for leg in s.values()):
        warnings.append("Upstox returned no Greeks; every Greek/IV below is calculated (Black-Scholes) and marked as such.")
    return oc.Chain(info.name, expiry, spot, None, vix, table, open_now, t_years, violations, [], warnings, now)


async def margin(provider, legs: list[dict]) -> dict:
    """Upstox's margin calculator for a basket of {instrument_key, quantity, side}. Returns the numbers the simulator shows.
    `final_margin` is the requirement after the hedge benefit, `required_margin` before it."""
    instruments = [{"instrument_key": x["instrument_key"], "quantity": x["quantity"], "transaction_type": x["side"], "product": "D"} for x in legs]
    d = await provider.call(lambda token: upstox_client.get_margin(token, instruments))
    parts = d.get("margins") or []
    required, final = d.get("required_margin"), d.get("final_margin")
    return {"total": final if final is not None else required, "span": round(sum(float(p.get("span_margin") or 0) for p in parts), 2),
            "exposure": round(sum(float(p.get("exposure_margin") or 0) for p in parts), 2),
            "hedge_benefit": round(float(required) - float(final), 2) if required is not None and final is not None else None, "components": parts}
