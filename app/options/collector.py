"""Daily collector: after the market closes, save the day's real 1-minute candles of NIFTY / BANKNIFTY and their listed
options (app/options/intraday.py store). Angel One cannot serve a contract once it has expired, so whatever is not saved
while the contract is listed is lost; running this every trading day builds our own history going forward.

It only uses the REST historical endpoint after 15:45 IST and never touches the live WebSocket feed. Each run re-fetches a
few days back (upsert by minute), so a missed day (laptop off, server restart) is filled on the next run while Angel One
still lists those contracts.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from pathlib import Path

from app.options import intraday, master
from app.options.master import IST
from app.ops import flags as ops_flags
from app.ops.stats import tracked

log = logging.getLogger(__name__)

SYMBOLS = ("BANKNIFTY", "NIFTY")
RUN_AFTER = dt.time(15, 45)
DAYS_BACK = 5                  # overlap: covers a missed day or two
HORIZON_DAYS = 45              # expiries within this many days
PAD_POINTS = {"BANKNIFTY": 1500.0, "NIFTY": 600.0}   # strikes within the day's index range +/- this
STATUS = Path(__file__).resolve().parents[2] / "data" / "intraday" / "collector_status.json"


def _status() -> dict:
    try:
        return json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return {}


def status() -> dict:
    return _status()


def due(now: dt.datetime, last: str | None) -> bool:
    """Weekday, after RUN_AFTER, and not yet done today."""
    return now.weekday() < 5 and now.time() >= RUN_AFTER and last != now.date().isoformat()


@tracked("collector.run_once")
async def run_once(provider, symbols=SYMBOLS) -> dict:
    st = _status()
    session = await provider.get_session()
    for sym in symbols:
        info = await asyncio.to_thread(master.load_underlying, sym)
        started = dt.datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds")
        try:
            s = await intraday.ingest(session, info, DAYS_BACK, HORIZON_DAYS, PAD_POINTS.get(sym, 300.0), log=lambda m: log.info("collector %s: %s", sym, m))
            st[sym] = {"started": started, "finished": dt.datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds"), "ok": True,
                       "contracts": s["contracts"], "with_data": s["contracts_with_data"], "option_minutes": s["rows"],
                       "rejected": s["rejected"], "errors": s["errors"][:20]}
        except Exception as exc:                       # keep going with the next symbol; the error is recorded, not hidden
            log.exception("collector failed for %s", sym)
            st[sym] = {"started": started, "ok": False, "error": str(exc)[:300]}
    st["last_run_date"] = dt.datetime.now(IST).date().isoformat()
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(st, indent=1))
    return st


async def loop(provider, check_every: int = 300) -> None:
    while True:
        try:
            now = dt.datetime.now(IST).replace(tzinfo=None)
            if ops_flags.get("feature.collector") and due(now, _status().get("last_run_date")):
                log.info("daily option collector starting")
                await run_once(provider)
                log.info("daily option collector finished")
        except Exception:
            log.exception("daily option collector loop error")
        await asyncio.sleep(check_every)
