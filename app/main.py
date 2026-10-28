from __future__ import annotations

# use the OS certificate store for HTTPS and WebSocket: antivirus or office networks re-sign traffic and Python's own bundle then fails
# with CERTIFICATE_VERIFY_FAILED
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import urllib.parse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.paper_trading.engine import DEFAULT_LOOKBACK_DAYS, TIMEFRAMES, PaperEngine, PaperError
from app.providers.angelone_provider import AngelOneProvider
from app.providers.upstox_provider import UpstoxProvider
from app.options import api as options_api
from app.options import collector as option_collector
from app import auth, auth_api
from app import admin_extra, admin_view, option_journal, user_data
from app.ops import api as ops_api, context as ops_context, selftests as ops_selftests, stats as ops_stats, upstox_api
from app.security import authz, mode as login_mode
from app.security.users import AuthError
from app.services import angelone_credential_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

engine: PaperEngine | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global engine
    # DATA_SOURCE=upstox runs the live engine (Top Bottom, prices, candles), the option chain and the margin calculator on Upstox data instead of Angel One.
    # Only the nightly 1-minute option download (for the simulator's history) is still Angel One only and stays off in that mode.
    upstox_mode = os.environ.get("DATA_SOURCE", "angelone").strip().lower() == "upstox"
    provider = UpstoxProvider() if upstox_mode else AngelOneProvider()
    engine = PaperEngine(provider)
    options_api.provider = provider      # the option chain and the margin calculator use whichever data source is active (app/options/api.py)
    options_api.live_engine = engine     # live option chain shares the engine's single WebSocket feed
    ops_context.engine = engine          # the developer panel reads the engine's health from here
    engine.start()
    try:                                  # legs saved before the options journal existed go into it once (idempotent)
        from app.db import platform_db
        _c = platform_db.connect()
        try:
            option_journal.backfill(_c)
        finally:
            _c.close()
    except Exception as e:                # the journal must never stop the app from starting
        logging.getLogger(__name__).warning("options journal backfill failed: %s", e)
    asyncio.create_task(options_api.warm())
    if not upstox_mode:
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
app.include_router(upstox_api.router)
app.include_router(user_data.router)
app.include_router(admin_view.router)
app.include_router(admin_extra.router)
app.include_router(option_journal.router)
app.add_exception_handler(AuthError, auth_api.handle_auth_error)


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
    if path in ("/login", "/healthz", "/api/auth/mode", "/app") or path.startswith("/app/"):
        return await call_next(request)
    if auth.valid_session(request.cookies.get(auth.COOKIE)):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "login required"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


# ---- the React app (frontend/, built with `npm run build`) is served under /app --------------------------------------------------------
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
for _sub in ("assets", "fonts"):
    if (FRONTEND_DIST / _sub).is_dir():
        app.mount(f"/app/{_sub}", StaticFiles(directory=FRONTEND_DIST / _sub), name=f"app-{_sub}")


@app.get("/app")
@app.get("/app/{path:path}")
async def react_app(path: str = ""):
    index = FRONTEND_DIST / "index.html"
    if not index.exists():
        return PlainTextResponse("The React app is not built yet: run `npm install && npm run build` inside the frontend folder.", status_code=503)
    return FileResponse(index, headers={"Cache-Control": "no-store"})


def _to_app(request: Request, path: str) -> RedirectResponse:
    """Old addresses (/simulator, /login, /setup/<token> ...) keep working: they lead to the same page in the React app, query string included."""
    q = request.url.query
    return RedirectResponse(f"/app{path}" + (f"?{q}" if q else ""), status_code=307, headers={"Referrer-Policy": "no-referrer"})


@app.get("/login")
async def login_page(request: Request):
    return _to_app(request, "/login")


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
async def setup_page(token: str, request: Request):
    # the one-time setup link an admin hands to a new user: the React page checks the token with /api/auth/setup/{token}
    return _to_app(request, f"/setup/{token}")


@app.get("/account")
async def account_page(request: Request):
    return _to_app(request, "/account")


@app.get("/logout")
async def logout(request: Request):
    auth_api.end_current_session(request)          # platform mode: close the server-side session (does nothing in the other modes)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    auth_api.clear_cookies(resp)
    return resp


@app.get("/top-bottom")
@app.get("/journal")
@app.get("/builder")
@app.get("/backtesting")
@app.get("/history")
@app.get("/simulator")
@app.get("/settings")
@app.get("/options")
async def old_page_address(request: Request):
    # every page is now in the React app; /journal was the old name of Top Bottom
    path = request.url.path
    return _to_app(request, "/top-bottom" if path == "/journal" else path)


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


@app.get("/")
async def index():
    return RedirectResponse("/app/", status_code=307)   # the React app is the home page


@app.get("/api/overview")
async def overview(request: Request):
    data = engine.overview(owner=_owner(request))
    data["angelone_configured"] = angelone_credential_store.is_configured()
    data["data_source"] = getattr(engine.provider, "name", "Angel One")
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
    if getattr(request.state, "user", None) is not None and not (body.symbol.strip().upper() == "BANKNIFTY" and body.instrument_type.upper() == "FUTURE" and body.timeframe == "1D"):
        # with accounts, a person only gets the BankNifty strategy on their own; every other symbol is assigned by an admin
        raise HTTPException(403, "Only an admin can assign a strategy for another symbol.")
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
