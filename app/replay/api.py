"""BankNifty INDEX replay simulator API (paper/signal simulation only: nothing
here can place a broker order). Data = BankNifty index candles (Angel One
index token, or a validated CSV import), stored per interval."""
from __future__ import annotations

import asyncio
import bisect
import datetime as dt
import uuid
from collections import OrderedDict
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.replay import angel_fetch
from app.replay.csv_import import SYMBOL, validate_csv
from app.replay.plan import PlanError, plan_session
from app.replay.quality import session_quality
from app.replay.engine import RULE_GAPS, UNIT, StrategyRun, backtest_report, run_hybrid_strategy, run_intraday_strategy, run_strategy, snapshot
from app.strategy.top_bottom.gap_protocol import FirstCandle
from app.replay.store import INDEX_INSTRUMENTS, InstrumentError, ReplayStore
from app.replay.timeframes import INTERVALS

router = APIRouter(prefix="/api/replay", tags=["replay"])

MAX_CANDLES = 120_000  # memory guard for one replay/backtest run
_store: ReplayStore | None = None
_runs: "OrderedDict[str, StrategyRun]" = OrderedDict()  # bounded cache of computed runs
_MAX_RUNS = 4
_jobs: dict[str, dict] = {}
provider = None  # the app's single AngelOneProvider, set by app.main at startup (no second login path)
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "replay" / "angel_cache"


def store() -> ReplayStore:
    global _store
    if _store is None:
        _store = ReplayStore()
    return _store


def set_store(s: ReplayStore | None) -> None:
    """Test hook."""
    global _store
    _store = s
    _runs.clear()
    _jobs.clear()


class ImportRequest(BaseModel):
    csv_text: str


class SessionRequest(BaseModel):
    mode: str = "daily"  # "daily" (classic close-based) | "intraday" (1-minute execution on daily structure)
    interval: str = "1D"
    from_date: dt.date | None = None   # normally omitted: the backend chooses the replay range
    to_date: dt.date | None = None
    block: int | None = None   # which continuous 1-minute block to replay (default: the most recent one)
    min_separation: float | None = Field(None, ge=0)   # OPTIONAL filter, index points; None/0 = original strategy (default)
    slippage_points: float = Field(0.0, ge=0)
    speed: float = 1.0


class PositionRequest(BaseModel):
    index: int
    speed: float | None = None


class DownloadRequest(BaseModel):
    interval: str
    from_date: dt.date
    to_date: dt.date


def _resolve(config: dict) -> dict:
    """Fill in the replay range when the client did not give one (the normal case) and remember the plan."""
    if not config.get("from_date") or not config.get("to_date"):
        try:
            plan = plan_session(store(), config.get("mode", "daily"), config.get("interval"), config.get("block"))
        except PlanError as exc:
            raise HTTPException(400, str(exc)) from exc
        config["from_date"], config["to_date"], config["interval"] = plan["from_date"], plan["to_date"], plan["interval"]
        config["plan"] = plan
    return config


@router.get("/plan")
def get_plan(mode: str = "daily", interval: str | None = None, block: int | None = None) -> dict:
    """Read-only: which range the replay will use for this engine, the stored 1-minute blocks, and any warnings."""
    try:
        return plan_session(store(), mode, interval, block)
    except PlanError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/quality")
def get_quality(mode: str = "hybrid", block: int | None = None) -> dict:
    """Read-only 1-minute completeness report for every Daily session of the chosen block."""
    try:
        plan = plan_session(store(), mode, None, block)
    except PlanError as exc:
        raise HTTPException(400, str(exc)) from exc
    return session_quality(store(), plan["from_date"], plan["to_date"])


def _compute_intraday(config: dict) -> StrategyRun:
    """1-minute execution on daily structure. Needs stored 1m candles for the range
    and stored 1D candles (the structure); gaps in either are reported, not filled."""
    s = store()
    start = dt.datetime.combine(dt.date.fromisoformat(config["from_date"]), dt.time())
    end = dt.datetime.combine(dt.date.fromisoformat(config["to_date"]), dt.time(23, 59))
    have = s.count(SYMBOL, "1m", start, end)
    if have == 0:
        raise HTTPException(400, "No 1m candles for BANKNIFTY in the selected range. Download or import 1m data first (see data coverage).")
    if have > MAX_CANDLES:
        raise HTTPException(400, f"{have} 1m candles exceed the {MAX_CANDLES} limit for one run. Choose a shorter range.")
    daily = s.candles(SYMBOL, "1D", None, end)
    if not daily:
        raise HTTPException(400, "Intraday mode needs 1D candles (the System Point structure). Download 1D data covering the range and the weeks before it.")
    rows = s.candles(SYMBOL, "1m", start, end)
    first_day = rows[0][0].date()
    if sum(1 for d in daily if d[0].date() < first_day) < 3:
        raise HTTPException(400, "Not enough 1D history before the start date to form System Points. Download 1D data from earlier dates.")
    return run_intraday_strategy(daily, rows, config["slippage_points"])


def _compute(config: dict) -> StrategyRun:
    _resolve(config)
    run = _compute_resolved(config)
    plan = config.get("plan") or {}
    run.session = {"mode": config.get("mode", "daily"), "interval": config["interval"], "from_date": config["from_date"],
                   "to_date": config["to_date"], "candles": len(run.candles), "end": plan.get("end"), "blocks": plan.get("blocks", []),
                   "selected_block": plan.get("selected_block"), "warmup": plan.get("warmup")}
    if config.get("mode") == "hybrid":
        q = session_quality(store(), config["from_date"], config["to_date"])
        run.quality = {s["date"]: s for s in q["sessions"]}
        run.session["quality"] = {**q["summary"], "warning": q["warning"], "calendar_note": q["calendar_note"]}
        if q["warning"]:
            run.warnings.append(f"1-minute data: {q['summary']['complete']} of {q['summary']['sessions']} Daily sessions are Complete "
                                f"({q['summary']['incomplete']} Incomplete, {q['summary']['not_verified']} Not Verified). {q['warning']}")
    run.warnings = [*plan.get("warnings", []), *run.warnings]
    return run


def _compute_hybrid(config: dict) -> StrategyRun:
    """Daily candles shown; 1-minute candles of each Daily session processed internally. Stored 15-minute candles
    (when present) feed the existing gap protocol; otherwise its first candle is derived from the genuine 1-minute data."""
    s = store()
    start = dt.datetime.combine(dt.date.fromisoformat(config["from_date"]), dt.time())
    end = dt.datetime.combine(dt.date.fromisoformat(config["to_date"]), dt.time(23, 59))
    have = s.count(SYMBOL, "1m", start, end)
    if have == 0:
        raise HTTPException(400, "No 1m candles for BANKNIFTY in the selected range. Download or import 1m data first (see data coverage).")
    if have > MAX_CANDLES:
        raise HTTPException(400, f"{have} 1m candles exceed the {MAX_CANDLES} limit for one run. Choose a shorter range.")
    daily = s.candles(SYMBOL, "1D", None, end)
    if not daily:
        raise HTTPException(400, "This engine needs 1D candles (they are what you see and what gives the System Points). Download 1D data.")
    minutes = s.candles(SYMBOL, "1m", start, end)
    first15 = {c[0].date(): FirstCandle(high=c[2], low=c[3], open=c[1])
               for c in s.candles(SYMBOL, "15m", start, end) if c[0].time() == dt.time(9, 15)}
    run = run_hybrid_strategy(daily, minutes, config["slippage_points"], first15, config.get("min_separation") or None)
    if not first15:
        run.warnings.append("No stored 15-minute candles: the gap protocol's first 15-minute candle is derived from the genuine 1-minute data (09:15-09:30) when it is complete.")
    return run


def _compute_resolved(config: dict) -> StrategyRun:
    if config.get("mode") == "hybrid":
        return _compute_hybrid(config)
    if config.get("mode") == "intraday":
        return _compute_intraday(config)
    s = store()
    iv = config["interval"]
    if iv not in INTERVALS:
        raise HTTPException(400, f"Unsupported interval {iv}")
    start = dt.datetime.combine(dt.date.fromisoformat(config["from_date"]), dt.time())
    end = dt.datetime.combine(dt.date.fromisoformat(config["to_date"]), dt.time(23, 59))
    have = s.count(SYMBOL, iv, start, end)
    if have == 0:
        raise HTTPException(400, f"No {iv} candles for BANKNIFTY in the selected range. Download or import data first (see data coverage).")
    if have > MAX_CANDLES:
        raise HTTPException(400, f"{have} {iv} candles exceed the {MAX_CANDLES} limit for one run. Choose a shorter range.")
    rows = s.candles(SYMBOL, iv, start, end)
    rows15 = s.candles(SYMBOL, "15m", start, end) if iv == "1D" and s.count(SYMBOL, "15m", start, end) else None
    return run_strategy(rows, iv, rows15, config["slippage_points"])


def _run_for(sid: str) -> tuple[StrategyRun, dict, int]:
    saved = store().load_session(sid)
    if saved is None:
        raise HTTPException(404, "Unknown replay session")
    run = _runs.get(sid)
    if run is None:
        run = _compute(saved["config"])
        _runs[sid] = run
        while len(_runs) > _MAX_RUNS:
            _runs.popitem(last=False)
    return run, saved["config"], saved["position"]


@router.get("/instruments")
def instruments() -> dict:
    cov = store().coverage()
    return {
        "instrument": {"symbol": SYMBOL, "instrument_type": "INDEX", **INDEX_INSTRUMENTS[SYMBOL]},
        "unit": UNIT, "intervals": list(INTERVALS), "coverage": cov,
        "available_intervals": [c["interval"] for c in cov if c["candles"]],
        "rule_gaps": RULE_GAPS,
    }


@router.post("/import")
def import_csv(req: ImportRequest) -> dict:
    result = validate_csv(req.csv_text)
    if not result.ok:
        raise HTTPException(422, {"message": result.report.get("fatal", "Import failed"), "report": result.report})
    result.report["source"] = "IMPORT"
    try:
        store().add_candles(result.meta["symbol"], result.meta["interval"], result.rows, "IMPORT", result.report)
    except InstrumentError as exc:
        raise HTTPException(422, str(exc)) from exc
    _runs.clear()
    return {"meta": result.meta, "report": result.report}


async def _run_download(job_id: str, req: DownloadRequest) -> None:
    from app.providers import angelone_client
    from app.providers import angelone_scrip_master as scrip

    job = _jobs[job_id]
    try:
        m = await scrip.resolve_index(SYMBOL)
        if m is None or m.token != angel_fetch.INDEX_TOKEN or m.exch_seg != "NSE":
            raise RuntimeError(f"BANKNIFTY index instrument could not be verified (got {m}); refusing to download.")
        if provider is None:
            raise RuntimeError("Angel One provider is not initialised")

        async def fetch_chunk(angel_iv, a, b):
            session = await provider.get_session()
            return await angelone_client.get_candle_data(
                session, m.exch_seg, m.token, angel_iv, a.strftime("%Y-%m-%d %H:%M"), b.strftime("%Y-%m-%d %H:%M"))

        def progress(done: int, total: int) -> None:
            job.update(done=done, total=total)

        result = await angel_fetch.fetch_index(req.interval, req.from_date, req.to_date, fetch_chunk, CACHE_DIR, progress=progress)
        if not result.ok:
            raise RuntimeError(result.report.get("fatal", "No valid data returned"))
        store().add_candles(SYMBOL, req.interval, result.rows, "ANGELONE", result.report)
        _runs.clear()
        job.update(state="done", report={k: v for k, v in result.report.items() if k != "rejected"})
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI; the chunk cache keeps completed work
        job.update(state="error", error=str(exc))


@router.post("/downloads")
async def start_download(req: DownloadRequest) -> dict:
    """Explicit, user-triggered, resumable chunked download of real index
    candles. Completed chunks are cached on disk; re-posting resumes."""
    if req.interval not in INTERVALS:
        raise HTTPException(400, f"Unsupported interval {req.interval}")
    if req.from_date > req.to_date:
        raise HTTPException(400, "from_date must not be after to_date")
    if any(j["state"] == "running" for j in _jobs.values()):
        raise HTTPException(409, "A download is already running")
    chunks = angel_fetch.chunks(req.interval, req.from_date, req.to_date)
    jid = uuid.uuid4().hex[:10]
    _jobs[jid] = {"id": jid, "state": "running", "interval": req.interval, "from_date": req.from_date.isoformat(),
                  "to_date": req.to_date.isoformat(), "done": 0, "total": len(chunks), "error": None, "report": None}
    asyncio.create_task(_run_download(jid, req))
    return _jobs[jid]


@router.get("/downloads/{jid}")
def download_status(jid: str) -> dict:
    if jid not in _jobs:
        raise HTTPException(404, "Unknown download")
    return _jobs[jid]


MAX_PREVIEW = 20_000


@router.get("/preview")
def preview(interval: str | None = None, from_date: dt.date | None = None, to_date: dt.date | None = None, mode: str = "daily", block: int | None = None) -> dict:
    """Plain stored candles (no strategy output) so the user can pick the replay
    start point on the chart, TradingView-style. Nothing computed from the
    strategy is exposed here; the replay itself reveals state only up to the cut."""
    if not from_date or not to_date:   # normal case: the backend chooses the range, like the replay itself
        cfg = _resolve({"mode": mode, "interval": interval, "block": block})
        interval, from_date, to_date = cfg["interval"], dt.date.fromisoformat(cfg["from_date"]), dt.date.fromisoformat(cfg["to_date"])
    if interval not in INTERVALS:
        raise HTTPException(400, f"Unsupported interval {interval}")
    start = dt.datetime.combine(from_date, dt.time())
    end = dt.datetime.combine(to_date, dt.time(23, 59))
    n = store().count(SYMBOL, interval, start, end)
    if n == 0:
        raise HTTPException(400, f"No {interval} candles for BANKNIFTY in the selected range. Download or import data first (see data coverage).")
    if n > MAX_PREVIEW:
        raise HTTPException(400, f"{n} candles is too many to preview (limit {MAX_PREVIEW}). Choose a shorter range.")
    rows = store().candles(SYMBOL, interval, start, end)
    return {"interval": interval, "candles": [
        {"time": r[0].isoformat(), "open": r[1], "high": r[2], "low": r[3], "close": r[4]} for r in rows]}


@router.post("/sessions")
def create_session(req: SessionRequest) -> dict:
    if req.from_date and req.to_date and req.from_date > req.to_date:
        raise HTTPException(400, "from_date must not be after to_date")
    config = req.model_dump(mode="json")
    run = _compute(config)
    sid = uuid.uuid4().hex[:12]
    store().save_session(sid, config, 0)
    _runs[sid] = run
    return {"id": sid, "config": config, "total": len(run.candles), "snapshot": snapshot(run, 0)}


@router.get("/sessions/latest")
def latest_session() -> dict:
    sid = store().latest_session_id()
    if sid is None:
        raise HTTPException(404, "No saved replay session")
    return get_session(sid)


@router.get("/sessions/{sid}")
def get_session(sid: str, index: int | None = None, window: int = 400) -> dict:
    """Snapshot at `index` (default: the saved position, so a refresh resumes)."""
    run, config, position = _run_for(sid)
    return {"id": sid, "config": config, "snapshot": snapshot(run, position if index is None else index, window)}


@router.get("/sessions/{sid}/index-for")
def index_for(sid: str, at: str) -> dict:
    """First candle index at/after a date ('YYYY-MM-DD') or date-time (IST), else the last."""
    run, _, _ = _run_for(sid)
    try:
        t = dt.datetime.fromisoformat(at)
    except ValueError as exc:
        raise HTTPException(400, "at must be an ISO date or date-time") from exc
    i = bisect.bisect_left(run.dates, t)
    return {"index": min(i, len(run.candles) - 1)}


@router.post("/sessions/{sid}/position")
def save_position(sid: str, req: PositionRequest) -> dict:
    run, config, _ = _run_for(sid)
    idx = max(0, min(req.index, len(run.candles) - 1))
    if req.speed is not None:
        config["speed"] = req.speed
    store().save_session(sid, config, idx)
    return {"id": sid, "index": idx, "total": len(run.candles)}


@router.post("/backtest")
def full_backtest(req: SessionRequest) -> dict:
    if req.from_date and req.to_date and req.from_date > req.to_date:
        raise HTTPException(400, "from_date must not be after to_date")
    return backtest_report(_compute(req.model_dump(mode="json")))
