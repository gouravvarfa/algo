"""
Live option chain from real Angel One data (no synthetic prices anywhere).

  - quotes  : POST /market/v1/quote FULL  -> LTP, best bid/ask, volume, open interest, exchange feed/trade times
  - greeks  : POST /marketData/v1/optionGreek -> delta, gamma, theta, vega and IV per strike (broker-provided)
  - fallback: if the broker returns no Greeks/IV for a strike they are computed with Black-Scholes (app/options/bs.py)
              from the strike's mid price (LTP when there is no two-sided quote) and marked source="calc".

Data quality is checked loudly (every violation is listed and returned), and nothing is repaired silently:
  HARD  (exit code 3): zero/negative price, bid > ask, a strike present in the master but missing in the data.
  STALE (only judged while the market is open): feed time older than the allowed age.
  ILLIQ (soft flag, excluded from strike selection): OI/volume below the minimum or spread above the maximum.
Not provided by this Angel One endpoint: CHANGE in open interest (only the current OI) - shown as n/a.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field

import httpx

from app.options import bs
from app.options.master import IST, EXPIRY_TIME, OptionContract, UnderlyingInfo
from app.providers import angelone_client as ac

QUOTE_PATH = "/rest/secure/angelbroking/market/v1/quote/"
GREEK_PATH = "/rest/secure/angelbroking/marketData/v1/optionGreek"
MAX_TOKENS_PER_QUOTE = 50
MIN_SECONDS_BETWEEN_CALLS = 1.1     # quote endpoint is limited to ~1 request per second


@dataclass
class Leg:
    contract: OptionContract
    ltp: float | None = None
    bid: float | None = None
    ask: float | None = None
    volume: int | None = None
    oi: int | None = None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    greeks_source: str = "n/a"          # "broker" | "calc" | "n/a"
    feed_time: dt.datetime | None = None
    trade_time: dt.datetime | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def mid(self) -> float | None:
        return round((self.bid + self.ask) / 2, 2) if self.bid and self.ask else None

    @property
    def spread_pct(self) -> float | None:
        m = self.mid
        return round((self.ask - self.bid) / m * 100, 2) if m else None


@dataclass
class Chain:
    underlying: str
    expiry: dt.date
    spot: float
    spot_feed_time: dt.datetime | None
    vix: float | None
    rows: dict[float, dict[str, Leg]]        # strike -> {"CE": Leg, "PE": Leg}
    market_open: bool
    t_years: float
    violations: list[str]                    # HARD
    stale: list[str]
    warnings: list[str]
    fetched_at: dt.datetime


def market_open(now: dt.datetime) -> bool:
    """By the clock only: there is no exchange holiday calendar here (a holiday shows up as no fresh ticks)."""
    return now.weekday() < 5 and dt.time(9, 15) <= now.time() <= dt.time(15, 30)


def _t(raw: str | None) -> dt.datetime | None:
    try:
        v = dt.datetime.strptime(raw, "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)
        return None if v.year < 2000 else v
    except (TypeError, ValueError):
        return None


def window_strikes(strikes: list[float], spot: float, n: int) -> list[float]:
    """The ATM strike and n listed strikes on each side (using the strikes actually listed, whatever their spacing)."""
    ladder = sorted(set(strikes))
    atm = min(ladder, key=lambda k: abs(k - spot))
    i = ladder.index(atm)
    return ladder[max(0, i - n): i + n + 1]


async def _post(client: httpx.AsyncClient, session, path: str, body: dict) -> dict:
    headers = {**ac._base_headers(session.api_key), "Authorization": f"Bearer {session.jwt_token}"}
    last = None
    for attempt in range(4):
        r = await client.post(ac.BASE_URL + path, headers=headers, json=body)
        try:
            j = r.json()
        except ValueError:                       # rate-limit pages are not JSON: back off and retry
            last = f"HTTP {r.status_code}, non-JSON"
            await asyncio.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 200 and j.get("status") is True:
            return j
        last = j.get("message") or f"HTTP {r.status_code}"
        if "rate" not in str(last).lower() and "access" not in str(last).lower():
            break
        await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Angel One {path} failed: {last}")


async def fetch_chain(session, info: UnderlyingInfo, expiry: dt.date, n_strikes: int = 10, max_age: float = 5.0,
                      min_oi: int = 1000, min_volume: int = 100, max_spread_pct: float = 10.0,
                      rate: float = 0.065, div: float = 0.0, now: dt.datetime | None = None) -> Chain:
    now = now or dt.datetime.now(IST)
    mine = [c for c in info.contracts if c.expiry == expiry]
    if not mine:
        raise ValueError(f"no contracts for expiry {expiry}")
    async with httpx.AsyncClient(timeout=30) as client:
        # 1) spot + VIX first (they decide which strikes are 'ATM +/- n')
        idx_tokens = [info.spot_token] + ([info.vix_token] if info.vix_token else [])
        j = await _post(client, session, QUOTE_PATH, {"mode": "FULL", "exchangeTokens": {"NSE": idx_tokens}})
        got = {f["symbolToken"]: f for f in j["data"]["fetched"]}
        spot_q = got.get(info.spot_token)
        if not spot_q or not spot_q.get("ltp"):
            raise RuntimeError(f"no spot quote for {info.name}")
        spot = float(spot_q["ltp"])
        vix = float(got[info.vix_token]["ltp"]) if info.vix_token and info.vix_token in got else None
        strikes = window_strikes([c.strike for c in mine], spot, n_strikes)
        wanted = [c for c in mine if c.strike in strikes]
        by_token = {c.token: c for c in wanted}
        # 2) option quotes in batches of <= 50 tokens, >= 1.1 s apart
        quotes: dict[str, dict] = {}
        tokens = list(by_token)
        unfetched: list[str] = []
        for i in range(0, len(tokens), MAX_TOKENS_PER_QUOTE):
            await asyncio.sleep(MIN_SECONDS_BETWEEN_CALLS)
            jj = await _post(client, session, QUOTE_PATH, {"mode": "FULL", "exchangeTokens": {"NFO": tokens[i:i + MAX_TOKENS_PER_QUOTE]}})
            quotes.update({f["symbolToken"]: f for f in jj["data"]["fetched"]})
            unfetched += [str(u) for u in (jj["data"].get("unfetched") or [])]
        # 3) the broker's own Greeks / IV for this expiry
        await asyncio.sleep(MIN_SECONDS_BETWEEN_CALLS)
        greeks: dict[tuple[float, str], dict] = {}
        try:
            gj = await _post(client, session, GREEK_PATH, {"name": info.name, "expirydate": expiry.strftime("%d%b%Y").upper()})
            for g in gj.get("data") or []:
                greeks[(float(g["strikePrice"]), g["optionType"])] = g
        except RuntimeError:
            pass   # handled below: every leg then falls back to the local Black-Scholes calculation (flagged)

    open_now = market_open(now)
    expiry_dt = dt.datetime.combine(expiry, EXPIRY_TIME, tzinfo=IST)
    t_years = max((expiry_dt - now).total_seconds(), 0.0) / (365.0 * 86400.0)
    violations, stale, warnings = [], [], []
    rows: dict[float, dict[str, Leg]] = {}
    for c in wanted:
        leg = Leg(contract=c)
        rows.setdefault(c.strike, {})[c.option_type] = leg
        q = quotes.get(c.token)
        if q is None:
            violations.append(f"MISSING {c.symbol}: in the master but no quote was returned")
            leg.flags.append("MISSING")
            continue
        leg.ltp = float(q.get("ltp") or 0)
        buys, sells = (q.get("depth") or {}).get("buy") or [], (q.get("depth") or {}).get("sell") or []
        leg.bid = float(buys[0]["price"]) if buys and buys[0]["price"] else None
        leg.ask = float(sells[0]["price"]) if sells and sells[0]["price"] else None
        leg.volume, leg.oi = int(q.get("tradeVolume") or 0), int(q.get("opnInterest") or 0)
        leg.feed_time, leg.trade_time = _t(q.get("exchFeedTime")), _t(q.get("exchTradeTime"))
        if leg.ltp <= 0:
            violations.append(f"BAD_PRICE {c.symbol}: LTP {leg.ltp}")
            leg.flags.append("BAD_PRICE")
        if leg.bid is not None and leg.ask is not None and leg.bid > leg.ask:
            violations.append(f"CROSSED {c.symbol}: bid {leg.bid} > ask {leg.ask}")
            leg.flags.append("CROSSED")
        if open_now and leg.feed_time and (now - leg.feed_time).total_seconds() > max_age:
            stale.append(f"STALE {c.symbol}: feed {int((now - leg.feed_time).total_seconds())}s old (> {max_age:g}s)")
            leg.flags.append("STALE")
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
        g = greeks.get((c.strike, c.option_type))
        if g and g.get("impliedVolatility") not in (None, "", "0.000000"):
            leg.iv = float(g["impliedVolatility"])
            leg.delta, leg.gamma, leg.theta, leg.vega = (float(g[k]) for k in ("delta", "gamma", "theta", "vega"))
            leg.greeks_source = "broker"
        else:   # fallback: Black-Scholes from the strike's own price (mid if two-sided, else LTP) - flagged as calculated
            ref = leg.mid or (leg.ltp if leg.ltp > 0 else None)
            iv = bs.implied_vol(ref, spot, c.strike, t_years, rate, div, c.option_type == "CE") if ref else None
            if iv:
                gr = bs.greeks(spot, c.strike, t_years, rate, div, iv / 100.0, c.option_type == "CE")
                leg.iv, leg.delta, leg.gamma, leg.theta, leg.vega = iv, gr["delta"], gr["gamma"], gr["theta"], gr["vega"]
                leg.greeks_source = "calc"
                leg.flags.append(f"GREEKS_CALC(r={rate:g},q={div:g})")
    for k, sides in rows.items():
        for typ in ("CE", "PE"):
            if typ not in sides:
                violations.append(f"MISSING_STRIKE {info.name} {expiry:%d%b%Y} {k:g}{typ}: not in the master")
    if unfetched:
        violations.append(f"UNFETCHED tokens returned by the broker: {', '.join(unfetched[:6])}")
    if open_now and quotes and not any(_fresh(l, now, 300) for s in rows.values() for l in s.values()):
        warnings.append("The clock says the market is open but there are no ticks from the last 5 minutes: holiday or feed problem.")
    if not open_now:
        warnings.append("Market is closed (by the clock): values are the LAST traded prices; the staleness check is not applied.")
    if len([g for g in greeks]) == 0:
        warnings.append("The broker returned no Greeks; every Greek/IV below is calculated (Black-Scholes) and marked as such.")
    sf = _t(spot_q.get("exchFeedTime"))
    return Chain(info.name, expiry, spot, sf, vix, rows, open_now, t_years, violations, stale, warnings, now)


def _fresh(leg: Leg, now: dt.datetime, secs: int) -> bool:
    return bool(leg.feed_time and (now - leg.feed_time).total_seconds() <= secs)


# ------------------------------------------------------------------ strike selection helpers (liquid strikes only)
def liquid(leg: Leg | None) -> bool:
    return leg is not None and not any(f.startswith(("ILLIQ", "BAD_PRICE", "CROSSED", "MISSING", "STALE")) for f in leg.flags)


def atm_strike(chain: Chain) -> float:
    return min(chain.rows, key=lambda k: abs(k - chain.spot))


def otm_by_points(chain: Chain, side: str, points: float) -> float | None:
    """Listed strike closest to ATM +/- `points` on the OTM side (CE: above, PE: below)."""
    target = atm_strike(chain) + (points if side == "CE" else -points)
    cands = [k for k, s in chain.rows.items() if liquid(s.get(side))]
    return min(cands, key=lambda k: abs(k - target)) if cands else None


def by_delta(chain: Chain, side: str, target_delta: float) -> float | None:
    """Strike whose |delta| is closest to `target_delta` (needs Greeks)."""
    cands = [(k, abs(s[side].delta)) for k, s in chain.rows.items() if side in s and liquid(s[side]) and s[side].delta is not None]
    return min(cands, key=lambda kv: abs(kv[1] - abs(target_delta)))[0] if cands else None


def nearest_premium(chain: Chain, side: str, target: float) -> float | None:
    """Strike whose premium (mid, else LTP) is closest to `target` rupees."""
    cands = [(k, s[side].mid or s[side].ltp) for k, s in chain.rows.items() if side in s and liquid(s[side])]
    return min(cands, key=lambda kv: abs(kv[1] - target))[0] if cands else None
