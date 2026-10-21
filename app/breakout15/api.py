"""15-minute opening-candle CALL breakout backtest API. Historical index-point
backtest only: nothing here can place an order."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections import OrderedDict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.breakout15 import data, engine

router = APIRouter(prefix="/api/breakout15", tags=["breakout15"])
provider = None  # set by app.main (the app's single Angel One session)
_jobs: "OrderedDict[str, dict]" = OrderedDict()
_results: dict[str, dict] = {}  # latest result per symbol


class RunRequest(BaseModel):
    symbol: str = "NIFTY"
    from_date: dt.date
    to_date: dt.date


async def _run(job_id: str, req: RunRequest) -> None:
    from app.providers import angelone_client
    from app.providers import angelone_scrip_master as scrip

    job = _jobs[job_id]
    try:
        m = await scrip.resolve_index(req.symbol)
        if m is None or m.token != data.INDICES[req.symbol] or m.exch_seg != "NSE":
            raise RuntimeError(f"{req.symbol} index could not be verified (got {m}); refusing to run.")
        if provider is None:
            raise RuntimeError("Angel One provider is not initialised")

        async def fetch_chunk(a, b):
            for attempt in range(3):  # Angel One sometimes answers 403 under rate limiting; back off and retry
                try:
                    session = await provider.get_session()
                    return await angelone_client.get_candle_data(
                        session, m.exch_seg, m.token, "FIFTEEN_MINUTE", a.strftime("%Y-%m-%d %H:%M"), b.strftime("%Y-%m-%d %H:%M"))
                except Exception:  # noqa: BLE001
                    if attempt == 2:
                        raise
                    await asyncio.sleep(8 * (attempt + 1))

        bars = await data.fetch_minutes(req.symbol, req.from_date, req.to_date, fetch_chunk,
                                        progress=lambda d, t: job.update(done=d, total=t))
        if not bars:
            raise RuntimeError("Angel One returned no 15-minute candles for this range")
        rows, summary = engine.run_backtest(bars)
        _results[req.symbol] = {
            "symbol": req.symbol, "token": m.token, "from": req.from_date.isoformat(), "to": req.to_date.isoformat(),
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"), "summary": summary,
            "rows": [r.as_dict() for r in rows],
        }
        job.update(state="done")
    except Exception as exc:  # noqa: BLE001 - shown in the UI; chunk cache keeps finished work
        job.update(state="error", error=str(exc))


@router.post("/runs")
async def start_run(req: RunRequest) -> dict:
    if req.symbol not in data.INDICES:
        raise HTTPException(400, f"symbol must be one of {list(data.INDICES)}")
    if req.from_date > req.to_date:
        raise HTTPException(400, "from must not be after to")
    if any(j["state"] == "running" for j in _jobs.values()):
        raise HTTPException(409, "A run is already in progress")
    jid = uuid.uuid4().hex[:8]
    _jobs[jid] = {"id": jid, "symbol": req.symbol, "state": "running", "done": 0, "total": 0, "error": None}
    while len(_jobs) > 10:
        _jobs.popitem(last=False)
    asyncio.create_task(_run(jid, req))
    return _jobs[jid]


@router.get("/runs/{job_id}")
async def get_run(job_id: str) -> dict:
    if job_id not in _jobs:
        raise HTTPException(404, "unknown run")
    return _jobs[job_id]


@router.get("/results/{symbol}")
async def get_result(symbol: str) -> dict:
    if symbol not in _results:
        raise HTTPException(404, "no result yet; run the backtest first")
    return _results[symbol]
