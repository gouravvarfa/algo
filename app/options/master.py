"""Option contract list built from Angel One's instruments master (downloaded once per day, cached on disk).

Only real broker data: symbol, strike, expiry, lot size and tick size are taken from the master as-is. Lot size and
tick size are whatever the master says TODAY; a past date's lot size is NOT inferred from it (see `lot_size_note`).
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

import httpx

MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "instruments"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
EXPIRY_TIME = dt.time(15, 30)  # NSE index options stop trading at 15:30 IST on expiry day


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


def _load_rows(day: dt.date, force: bool) -> tuple[list[dict], str]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"scrip_master_options_{day.isoformat()}.json"
    if path.exists() and not force:
        return json.loads(path.read_text(encoding="utf-8")), f"cache {day.isoformat()}"
    rows = httpx.get(MASTER_URL, timeout=180).json()
    keep = [r for r in rows if (r.get("exch_seg") == "NFO" and r.get("instrumenttype") == "OPTIDX")
            or (r.get("exch_seg") == "NSE" and r.get("instrumenttype") == "AMXIDX")]
    for old in CACHE_DIR.glob("scrip_master_options_*.json"):   # keep only today's file
        old.unlink()
    path.write_text(json.dumps(keep), encoding="utf-8")
    return keep, f"downloaded {day.isoformat()}"


def load_underlying(name: str, today: dt.date | None = None, force_refresh: bool = False) -> UnderlyingInfo:
    name = name.upper()
    today = today or dt.datetime.now(IST).date()
    rows, source = _load_rows(today, force_refresh)
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


def expiries(info: UnderlyingInfo, now: dt.datetime | None = None) -> list[dt.date]:
    """Expiries that are still tradable right now (an expiry stops being tradable at 15:30 IST on its day)."""
    now = now or dt.datetime.now(IST)
    return sorted({c.expiry for c in info.contracts
                   if dt.datetime.combine(c.expiry, EXPIRY_TIME, tzinfo=IST) > now})


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
