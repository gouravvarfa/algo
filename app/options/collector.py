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
# MCX gold / silver options (owner, 18 Nov 2026): MCX trades until 23:55, so they are collected the next morning, 05:00-07:30, when no market is open
# and Angel One answers best (the data account is shared with the live strategy). Tuesday to Saturday: Saturday morning collects Friday.
MCX_SYMBOLS = ("GOLDM", "SILVERM")
MCX_RUN_WINDOW = (dt.time(5, 0), dt.time(7, 30))     # ends 90 minutes before MCX opens: the live strategy shares the data account
DAYS_BACK = 5                  # overlap: covers a missed day or two
HORIZON_DAYS = 45              # expiries within this many days
PAD_POINTS = {"BANKNIFTY": 1500.0, "NIFTY": 600.0, "GOLDM": 4000.0, "SILVERM": 8000.0}   # strikes within the day's underlying range +/- this
SYMBOL_TRIES = 3               # Angel One refusing once (HTTP 403 in the first minutes after the close) must not lose the day
RETRY_WAIT_S = 90.0
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


def due_mcx(now: dt.datetime, last: str | None) -> bool:
    """Tuesday to Saturday morning inside MCX_RUN_WINDOW, and not yet done today (it collects the sessions before today)."""
    return 1 <= now.weekday() <= 5 and MCX_RUN_WINDOW[0] <= now.time() < MCX_RUN_WINDOW[1] and last != now.date().isoformat()


def _done_today(st: dict, sym: str, today: str) -> bool:
    s = st.get(sym) or {}
    return bool(s.get("ok")) and str(s.get("finished", "")).startswith(today)


@tracked("collector.run_once")
async def run_once(provider, symbols=SYMBOLS, mark: str = "last_run_date", retry_failed: bool = False) -> dict:
    """One collection of `symbols`. With `retry_failed` (the MCX morning window) a symbol that failed all its tries leaves the day open, so the
    next round of the loop tries again while the window lasts; only the symbols not yet saved today are fetched again. 20 Nov 2026: Angel One
    refused GOLDM three times between 05:02 and 05:08, the day was marked done anyway and nothing more was tried until 07:30."""
    st = _status()
    session = await provider.get_session()
    today = dt.datetime.now(IST).date().isoformat()
    for sym in symbols:
        if retry_failed and _done_today(st, sym, today):
            continue
        info = await asyncio.to_thread(master.load_underlying, sym)
        started = dt.datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds")
        for attempt in range(1, SYMBOL_TRIES + 1):
            if await _collect_symbol(session, sym, info, started, st, last=attempt == SYMBOL_TRIES):
                break
            if attempt == SYMBOL_TRIES:
                log.warning("collector %s: try %d of %d failed%s", sym, attempt, SYMBOL_TRIES, ": tried again in the next round of the window" if retry_failed else "")
                break
            log.warning("collector %s: try %d of %d failed, again in %.0fs", sym, attempt, SYMBOL_TRIES, RETRY_WAIT_S)
            await asyncio.sleep(RETRY_WAIT_S)
    failed = [s for s in symbols if not (st.get(s) or {}).get("ok")]
    if retry_failed and failed:
        log.warning("collector: %s not saved yet today; the day stays open", ", ".join(failed))
    else:
        st[mark] = today
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(st, indent=1))
    return st


async def _collect_symbol(session, sym, info, started, st, last) -> bool:
    """One try for one symbol; the outcome goes into st[sym]. Only the last failed try is logged as an error."""
    try:
        s = await intraday.ingest(session, info, DAYS_BACK, HORIZON_DAYS, PAD_POINTS.get(sym, 300.0), log=lambda m: log.info("collector %s: %s", sym, m))
        fixed = await intraday.repair_errors(session, info, DAYS_BACK, log=lambda m: log.info("collector %s: %s", sym, m))      # a rate-limited contract gets a second chance
        s["errors"] = fixed["still_failing"]
        s["contracts_with_data"] += fixed["fixed"]
        st[sym] = {"started": started, "finished": dt.datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds"), "ok": True,
                   "contracts": s["contracts"], "with_data": s["contracts_with_data"], "option_minutes": s["rows"],
                   "rejected": s["rejected"], "errors": s["errors"][:20]}
        return True
    except Exception as exc:                       # keep going with the next symbol; the error is recorded, not hidden
        if last:
            log.exception("collector failed for %s", sym)
        st[sym] = {"started": started, "ok": False, "error": str(exc)[:300]}
        return False


async def loop(provider, check_every: int = 300) -> None:
    while True:
        try:
            now = dt.datetime.now(IST).replace(tzinfo=None)
            if ops_flags.get("feature.collector") and due(now, _status().get("last_run_date")):
                log.info("daily option collector starting")
                await run_once(provider)
                log.info("daily option collector finished")
            if ops_flags.get("feature.collector") and due_mcx(now, _status().get("last_mcx_run_date")):
                log.info("MCX option collector starting (%s)", ", ".join(MCX_SYMBOLS))
                await run_once(provider, MCX_SYMBOLS, mark="last_mcx_run_date", retry_failed=True)
                log.info("MCX option collector finished")
        except Exception:
            log.exception("daily option collector loop error")
        await asyncio.sleep(check_every)
