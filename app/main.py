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
from app.options import api as options_api
from app.options import collector as option_collector
from app import auth, auth_api
from app import admin_view, user_data
from app.ops import api as ops_api, context as ops_context, selftests as ops_selftests, stats as ops_stats
from app.security import authz, mode as login_mode
from app.security.users import AuthError
from app.services import angelone_credential_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
engine: PaperEngine | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global engine
    provider = AngelOneProvider()
    engine = PaperEngine(provider)
    options_api.provider = provider
    options_api.live_engine = engine     # live option chain shares the engine's single WebSocket feed
    ops_context.engine = engine          # the developer panel reads the engine's health from here
    engine.start()
    asyncio.create_task(options_api.warm())
    asyncio.create_task(option_collector.loop(provider))   # daily after-close 1-minute option download (REST only; the WebSocket feed is untouched)
    asyncio.create_task(_selftest_loop())                  # known-answer checks every 10 minutes (developer panel)
    yield
    await engine.stop()


async def _selftest_loop():
    while True:
        try:
            await asyncio.to_thread(ops_selftests.run_all)
        except Exception:
            logging.getLogger("ops").exception("self-test run failed")
        await asyncio.sleep(600)


app = FastAPI(title="Top-Bottom Paper Trader", lifespan=lifespan)


@app.middleware("http")
async def request_metrics(request, call_next):
    """Per-route timing and error counts for the developer panel (route TEMPLATE, e.g. /api/instruments/{instrument_id}, never raw ids)."""
    import time as _t
    t0 = _t.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        ops_stats.record_request(getattr(route, "path", request.url.path), (_t.perf_counter() - t0) * 1000, status)
app.include_router(options_api.router)
app.include_router(auth_api.router)
app.include_router(ops_api.router)
app.include_router(user_data.router)
app.include_router(admin_view.router)
app.add_exception_handler(AuthError, auth_api.handle_auth_error)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.middleware("http")
async def require_login(request, call_next):
    """Which login applies is decided by app/security/mode.py: 'platform' (password + TOTP, server-side sessions, auth_api.gate),
    'legacy' (the old single-user login of app/auth.py, below) or 'open' (no login, local use). Pages redirect to /login, APIs answer 401."""
    current = login_mode.current()
    if current == "open":
        return await call_next(request)
    if current == "platform":
        blocked = auth_api.gate(request)
        return blocked if blocked is not None else await call_next(request)
    path = request.url.path
    if path in ("/login", "/healthz", "/api/auth/mode") or path.startswith(("/static/fonts/", "/static/theme.css", "/static/shell.js")):
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


@app.get("/setup/{token}")
async def setup_page(token: str):
    # the one-time setup link an admin hands to a new user (public page; the token is checked by /api/auth/setup/{token})
    return FileResponse(WEB_DIR / "setup.html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@app.get("/account")
async def account_page():
    # who am I + change password (platform login only: without it the page sends you to /login)
    return FileResponse(WEB_DIR / "account.html", headers={"Cache-Control": "no-store"})


@app.get("/logout")
async def logout(request: Request):
    auth_api.end_current_session(request)          # platform mode: close the server-side session (does nothing in the other modes)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    auth_api.clear_cookies(resp)
    return resp


@app.get("/top-bottom")
@app.get("/journal")
async def top_bottom_page():
    # BankNifty live Top-Bottom chart + live event log (the old /journal link still works)
    return FileResponse(WEB_DIR / "top_bottom.html", headers={"Cache-Control": "no-store"})


def _owner(request: Request) -> str | None:
    """The platform user whose data this request may touch (None = no login: the single local watchlist). The id comes from the session only."""
    u = getattr(request.state, "user", None)
    if u is not None and authz.is_owner(u):
        engine.adopt_orphans(u["id"])           # the watchlist and journal from before user accounts belong to the first account
    return u["id"] if u else None


@app.get("/api/journal")
async def journal_api(request: Request, limit: int = 300):
    return engine.journal(max(1, min(limit, 5000)), owner=_owner(request)) if engine else []


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
    return RedirectResponse("/top-bottom", status_code=307)   # the Dashboard page was removed; Top Bottom is the home page


@app.get("/api/overview")
async def overview(request: Request):
    data = engine.overview(owner=_owner(request))
    data["angelone_configured"] = angelone_credential_store.is_configured()
    data["timeframes"] = list(TIMEFRAMES)
    data["default_lookback_days"] = DEFAULT_LOOKBACK_DAYS
    return data


@app.get("/api/instruments/{instrument_id}")
async def instrument(instrument_id: str, request: Request):
    if engine.owned(instrument_id, _owner(request)) is None:
        raise HTTPException(404, "Instrument not found")
    snap = engine.snapshots.get(instrument_id)
    if snap is None:
        raise HTTPException(503, engine.errors.get(instrument_id, "Not evaluated yet"))
    return snap


@app.post("/api/instruments")
async def add_instrument(body: AddInstrument, request: Request):
    owner = _owner(request)
    try:
        inst = await engine.add(body.symbol, body.instrument_type, body.timeframe, body.size, body.lookback_days, owner=owner)
    except PaperError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Angel One error: {exc}")
    return {"instrument": inst.model_dump(), "error": engine.errors.get(inst.id)}


@app.delete("/api/instruments/{instrument_id}")
async def remove_instrument(instrument_id: str, request: Request):
    if not engine.remove(instrument_id, owner=_owner(request)):
        raise HTTPException(404, "Instrument not found")
    return {"ok": True}


@app.post("/api/refresh")
async def refresh(request: Request):
    owner = _owner(request)
    for inst in [i for i in list(engine.instruments.values()) if i.owner == owner]:      # only your own instruments
        await engine.refresh_one(inst)
    return {"ok": True}
