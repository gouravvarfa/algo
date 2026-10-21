from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import urllib.parse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.paper_trading.engine import DEFAULT_LOOKBACK_DAYS, TIMEFRAMES, PaperEngine, PaperError
from app.providers.angelone_provider import AngelOneProvider
from app.breakout15 import api as breakout15_api
from app.options import api as options_api
from app.options import collector as option_collector
from app.replay import api as replay_api
from app import auth
from app.services import angelone_credential_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
engine: PaperEngine | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global engine
    provider = AngelOneProvider()
    engine = PaperEngine(provider)
    breakout15_api.provider = provider
    options_api.provider = provider
    replay_api.provider = provider  # replay downloads reuse the same Angel One session
    engine.start()
    asyncio.create_task(options_api.warm())
    asyncio.create_task(option_collector.loop(provider))   # daily after-close 1-minute option download (REST only; the WebSocket feed is untouched)
    yield
    await engine.stop()


app = FastAPI(title="Top-Bottom Paper Trader", lifespan=lifespan)
app.include_router(breakout15_api.router)
app.include_router(options_api.router)
app.include_router(replay_api.router)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.middleware("http")
async def require_login(request, call_next):
    """Only active on a hosted deployment (see app/auth.py). Pages redirect to /login, APIs answer 401."""
    if not auth.enabled():
        return await call_next(request)
    path = request.url.path
    if path in ("/login", "/healthz") or path.startswith(("/static/fonts/", "/static/theme.css", "/static/shell.js")):
        return await call_next(request)
    if auth.valid_session(request.cookies.get(auth.COOKIE)):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "login required"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.get("/login")
async def login_page():
    return FileResponse(WEB_DIR / "login.html", headers={"Cache-Control": "no-store"})


@app.post("/login")
async def login_submit(request: Request):
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
    if auth.throttled(ip):
        return RedirectResponse("/login?e=wait", status_code=303)
    form = urllib.parse.parse_qs((await request.body()).decode("utf-8", "replace"))
    user, pw = (form.get("username") or [""])[0], (form.get("password") or [""])[0]
    if not auth.enabled() or not auth.login(user, pw):
        auth.record_failure(ip)
        return RedirectResponse("/login?e=1", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp.set_cookie(auth.COOKIE, auth.make_session(user), max_age=auth.SESSION_SECONDS, httponly=True, samesite="strict", secure=secure)
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/top-bottom")
@app.get("/journal")
async def top_bottom_page():
    # BankNifty live Top-Bottom chart + live event log (the old /journal link still works)
    return FileResponse(WEB_DIR / "top_bottom.html", headers={"Cache-Control": "no-store"})


@app.get("/api/journal")
async def journal_api(limit: int = 300):
    return engine.journal(max(1, min(limit, 5000))) if engine else []


@app.get("/api/index/{name}")
async def index_quote(name: str):
    name = name.upper()
    if engine is None or name not in ("BANKNIFTY", "NIFTY"):
        raise HTTPException(404, "unknown index")
    try:
        return await engine.index_quote(name)
    except Exception as exc:
        raise HTTPException(502, f"index quote unavailable: {exc}")


@app.get("/api/collector")
async def collector_status():
    return option_collector.status()


@app.get("/healthz")
async def healthz():
    return {"ok": True, "paper_engine_last_tick": engine.last_tick.isoformat() if engine and engine.last_tick else None, "feed": engine.feed.status() if engine else None}


@app.middleware("http")
async def static_revalidate(request, call_next):
    # browsers kept a stale theme.css/shell.js after UI changes; make them revalidate (cheap 304 when unchanged)
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


class AddInstrument(BaseModel):
    symbol: str
    instrument_type: str = "EQUITY"
    timeframe: str = "15m"
    size: int = 1  # equity: shares, future: lots
    lookback_days: int | None = None


@app.get("/replay")
async def replay_page():
    return FileResponse(WEB_DIR / "replay.html", headers={"Cache-Control": "no-store"})


@app.get("/builder")
@app.get("/backtesting")
@app.get("/history")
async def builder_page():
    # one page, three separate views chosen by the path: define a strategy / run backtests / saved results
    return FileResponse(WEB_DIR / "builder.html", headers={"Cache-Control": "no-store"})


@app.get("/simulator")
async def simulator_page():
    return FileResponse(WEB_DIR / "simulator.html", headers={"Cache-Control": "no-store"})


@app.get("/settings")
async def settings_page():
    return FileResponse(WEB_DIR / "settings.html", headers={"Cache-Control": "no-store"})


@app.get("/options")
async def options_page():
    return FileResponse(WEB_DIR / "options.html", headers={"Cache-Control": "no-store"})


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/breakout15")
async def breakout15_page():
    return FileResponse(WEB_DIR / "breakout15.html", headers={"Cache-Control": "no-store"})


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
