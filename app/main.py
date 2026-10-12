from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.paper_trading.engine import DEFAULT_LOOKBACK_DAYS, TIMEFRAMES, PaperEngine, PaperError
from app.providers.angelone_provider import AngelOneProvider
from app.replay import api as replay_api
from app.services import angelone_credential_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
engine: PaperEngine | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global engine
    provider = AngelOneProvider()
    engine = PaperEngine(provider)
    replay_api.provider = provider  # replay downloads reuse the same Angel One session
    engine.start()
    yield
    await engine.stop()


app = FastAPI(title="Top-Bottom Paper Trader", lifespan=lifespan)
app.include_router(replay_api.router)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


class AddInstrument(BaseModel):
    symbol: str
    instrument_type: str = "EQUITY"
    timeframe: str = "15m"
    size: int = 1  # equity: shares, future: lots
    lookback_days: int | None = None


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/replay")
async def replay_page():
    return FileResponse(WEB_DIR / "replay.html", headers={"Cache-Control": "no-store"})


@app.get("/api/overview")
async def overview():
    data = engine.overview()
    data["angelone_configured"] = angelone_credential_store.is_configured()
    data["timeframes"] = list(TIMEFRAMES)
    data["default_lookback_days"] = DEFAULT_LOOKBACK_DAYS
    return data


@app.get("/api/instruments/{instrument_id}")
async def instrument(instrument_id: str):
    if instrument_id not in engine.instruments:
        raise HTTPException(404, "Instrument not found")
    snap = engine.snapshots.get(instrument_id)
    if snap is None:
        raise HTTPException(503, engine.errors.get(instrument_id, "Not evaluated yet"))
    return snap


@app.post("/api/instruments")
async def add_instrument(body: AddInstrument):
    try:
        inst = await engine.add(body.symbol, body.instrument_type, body.timeframe, body.size, body.lookback_days)
    except PaperError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Angel One error: {exc}")
    return {"instrument": inst.model_dump(), "error": engine.errors.get(inst.id)}


@app.delete("/api/instruments/{instrument_id}")
async def remove_instrument(instrument_id: str):
    engine.remove(instrument_id)
    return {"ok": True}


@app.post("/api/refresh")
async def refresh():
    await engine.refresh_all()
    return {"ok": True}
