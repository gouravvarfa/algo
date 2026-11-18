"""Option contract list built from Angel One's instruments master (downloaded once per day, cached on disk).

Only real broker data: symbol, strike, expiry, lot size and tick size are taken from the master as-is. Lot size and
tick size are whatever the master says TODAY; a past date's lot size is NOT inferred from it (see `lot_size_note`).
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx

MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "instruments"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
EXPIRY_TIME = dt.time(15, 30)  # NSE index options stop trading at 15:30 IST on expiry day
NSE_SESSION = (dt.time(9, 15), dt.time(15, 30))
# MCX options on futures (owner, 18 Nov 2026: gold and silver in the options tools). Each is an option on a FUTURES contract, quoted per 10 g (gold)
# or per kg (silver); `point_value` is the rupees one lot makes or loses per 1-point move (Gold Mini lot 100 g -> Rs 10, Silver Mini lot 5 kg -> Rs 5).
# In these tools an option's lot_size is that point value, so every "price x lots x lot_size" P&L stays right.
MCX_OPTIONS = {"GOLDM": {"point_value": 10, "label": "Gold Mini"}, "SILVERM": {"point_value": 5, "label": "Silver Mini"}}
MCX_SESSION = (dt.time(9, 0), dt.time(23, 55))
MCX_EXPIRY_TIME = dt.time(23, 30)  # an MCX option trades until the session close on its expiry day: 23:30 in US summer time, else 23:55 (the earlier one)


@dataclass(frozen=True)
class OptionContract:
    symbol: str
    token: str
    name: str
    strike: float          # index points (the master stores strike x 100)
    expiry: dt.date
    option_type: str       # "CE" | "PE"
    lot_size: int
    tick_size: float       # rupees (the master stores paise)
    exch_lot: int = 0      # the exchange's own quantity per lot (MCX: grams / kg, used for margin); 0 = the same as lot_size

    @property
    def expiry_str(self) -> str:
        return self.expiry.strftime("%d%b%Y").upper()


@dataclass
class UnderlyingInfo:
    name: str
    spot_token: str
    spot_symbol: str
    vix_token: str | None
    contracts: list[OptionContract]
    source: str            # "downloaded <date>" | "cache <date>"
    exch: str = "NFO"                      # where the options trade
    spot_exch: str = "NSE"                 # where the underlying trades (an NSE index, or the MCX futures)
    futures: dict = field(default_factory=dict)   # MCX: option expiry -> (token, trading symbol) of the futures contract it is an option on
    expiry_time: dt.time = EXPIRY_TIME
    session: tuple = NSE_SESSION
    black76: bool = False                  # options on futures: priced with Black-76 (Black-Scholes with q = r)

    def underlying_for(self, expiry: dt.date | None) -> tuple[str, str]:
        """(token, symbol) of the price the options of `expiry` are on: the index, or (MCX) that expiry's futures contract."""
        return self.futures.get(expiry) or (self.spot_token, self.spot_symbol)

    def carry(self, rate: float) -> float:
        """The `q` for Black-Scholes: 0 for an index (no dividend model here), the rate itself for an option on futures (= Black-76)."""
        return rate if self.black76 else 0.0


def _load_rows(day: dt.date, force: bool) -> tuple[list[dict], str]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"scrip_master_options_v2_{day.isoformat()}.json"      # v2: also the MCX gold / silver options and their futures
    if path.exists() and not force:
        return json.loads(path.read_text(encoding="utf-8")), f"cache {day.isoformat()}"
    rows = httpx.get(MASTER_URL, timeout=180).json()
    keep = [r for r in rows if (r.get("exch_seg") == "NFO" and r.get("instrumenttype") == "OPTIDX")
            or (r.get("exch_seg") == "NSE" and r.get("instrumenttype") == "AMXIDX")
            or (r.get("exch_seg") == "MCX" and r.get("instrumenttype") in ("OPTFUT", "FUTCOM") and r.get("name") in MCX_OPTIONS)]
    for old in CACHE_DIR.glob("scrip_master_options_*.json"):   # keep only today's file
        old.unlink()
    path.write_text(json.dumps(keep), encoding="utf-8")
    return keep, f"downloaded {day.isoformat()}"


def load_underlying(name: str, today: dt.date | None = None, force_refresh: bool = False) -> UnderlyingInfo:
    name = name.upper()
    today = today or dt.datetime.now(IST).date()
    rows, source = _load_rows(today, force_refresh)
    if name in MCX_OPTIONS:
        return _load_mcx(name, rows, source, today)
    spot = next((r for r in rows if r["exch_seg"] == "NSE" and r["instrumenttype"] == "AMXIDX" and r["name"] == name), None)
    if spot is None:
        raise ValueError(f"index '{name}' not found in the instruments master")
    vix = next((r for r in rows if r["exch_seg"] == "NSE" and r["instrumenttype"] == "AMXIDX" and r["name"] == "INDIA VIX"), None)
    contracts: list[OptionContract] = []
    for r in rows:
        if r["exch_seg"] != "NFO" or r["name"] != name or not r["symbol"].endswith(("CE", "PE")):
            continue
        contracts.append(OptionContract(
            symbol=r["symbol"], token=r["token"], name=name, strike=float(r["strike"]) / 100.0,
            expiry=dt.datetime.strptime(r["expiry"], "%d%b%Y").date(), option_type=r["symbol"][-2:],
            lot_size=int(r["lotsize"]), tick_size=float(r["tick_size"]) / 100.0))
    contracts.sort(key=lambda c: (c.expiry, c.strike, c.option_type))
    return UnderlyingInfo(name, spot["token"], spot["symbol"], vix["token"] if vix else None, contracts, source)


def _load_mcx(name: str, rows: list[dict], source: str, today: dt.date) -> UnderlyingInfo:
    """An MCX option underlying: the options of `name` and, for each expiry, the futures contract it is an option on (the first futures that
    expires on or after the option: MCX options expire a few days before their futures)."""
    def exp(r):
        return dt.datetime.strptime(r["expiry"], "%d%b%Y").date()
    futs = sorted((r for r in rows if r["exch_seg"] == "MCX" and r["instrumenttype"] == "FUTCOM" and r["name"] == name), key=exp)
    pv = int(MCX_OPTIONS[name]["point_value"])
    contracts = [OptionContract(symbol=r["symbol"], token=r["token"], name=name, strike=float(r["strike"]) / 100.0, expiry=exp(r),
                                option_type=r["symbol"][-2:], lot_size=pv, tick_size=float(r["tick_size"]) / 100.0, exch_lot=int(float(r["lotsize"])))
                 for r in rows if r["exch_seg"] == "MCX" and r["instrumenttype"] == "OPTFUT" and r["name"] == name and r["symbol"].endswith(("CE", "PE"))]
    if not contracts or not futs:
        raise ValueError(f"no MCX options or futures for '{name}' in the instruments master")
    contracts.sort(key=lambda c: (c.expiry, c.strike, c.option_type))
    under = {}
    for e in sorted({c.expiry for c in contracts}):
        f = next((r for r in futs if exp(r) >= e), None)
        if f is not None:
            under[e] = (f["token"], f["symbol"])
    live = [e for e in sorted(under) if e >= today]
    tok, sym = under[live[0]] if live else (futs[-1]["token"], futs[-1]["symbol"])
    return UnderlyingInfo(name, tok, sym, None, contracts, source, exch="MCX", spot_exch="MCX", futures=under,
                          expiry_time=MCX_EXPIRY_TIME, session=MCX_SESSION, black76=True)


def expiries(info: UnderlyingInfo, now: dt.datetime | None = None) -> list[dt.date]:
    """Expiries that are still tradable right now (an expiry stops being tradable at its close: 15:30 IST for NSE, 23:30 for MCX)."""
    now = now or dt.datetime.now(IST)
    return sorted({c.expiry for c in info.contracts
                   if dt.datetime.combine(c.expiry, info.expiry_time, tzinfo=IST) > now})


def pick_expiry(info: UnderlyingInfo, which: str = "nearest", now: dt.datetime | None = None) -> dt.date:
    """'nearest', 'next', or an explicit DDMMMYYYY such as 27OCT2026."""
    avail = expiries(info, now)
    if not avail:
        raise ValueError("no tradable expiry in the instruments master")
    if which == "nearest":
        return avail[0]
    if which == "next":
        if len(avail) < 2:
            raise ValueError("there is no 'next' expiry in the master")
        return avail[1]
    want = dt.datetime.strptime(which.upper(), "%d%b%Y").date()
    if want not in avail:
        raise ValueError(f"{which} is not a tradable expiry; available: {[e.strftime('%d%b%Y').upper() for e in avail]}")
    return want


lot_size_note = ("Lot size and tick size are today's values from the instruments master. The master does not carry history, "
                 "so a past date's lot size must come from the exchange circulars, never from today's master.")
