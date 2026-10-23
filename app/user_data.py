"""Per-user data stored on the server instead of in the browser: the Options Simulator's positions and small preferences (LIVE mode, ...).

Why: a position that only exists in one browser cannot follow a person to another computer, and an admin cannot "view as" a screen that
lives in somebody's local storage. Every row is keyed by user id; a user can only ever read and write their own rows (the id comes from the
session, never from the request). With no login (local use) everything belongs to the single user "local".
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.auth_api import db
from app.security.users import AuthError

router = APIRouter()
LOCAL_USER = "local"
MAX_POSITIONS = 300
MAX_BYTES = 400_000
SYMBOLS = ("NIFTY", "BANKNIFTY")


def user_id_of(request: Request) -> str:
    u = getattr(request.state, "user", None)
    return u["id"] if u else LOCAL_USER


def read_positions(c, user_id: str, symbol: str) -> dict:
    r = c.execute("SELECT data_json, updated_at FROM user_sim_positions WHERE user_id=? AND symbol=?", (user_id, symbol)).fetchone()
    return {"symbol": symbol, "positions": json.loads(r["data_json"]) if r else [], "updated_at": r["updated_at"] if r else None}


def read_settings(c, user_id: str) -> dict:
    return {r["key"]: json.loads(r["value_json"]) for r in c.execute("SELECT key, value_json FROM user_settings WHERE user_id=?", (user_id,))}


def _symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if symbol not in SYMBOLS:
        raise AuthError(400, "BAD_SYMBOL", f"symbol must be one of {list(SYMBOLS)}")
    return symbol


class PositionsBody(BaseModel):
    positions: list[dict] = Field(max_length=MAX_POSITIONS)


@router.get("/api/me/sim-positions")
def get_sim_positions(request: Request, symbol: str = "NIFTY", c=Depends(db)):
    return read_positions(c, user_id_of(request), _symbol(symbol))


@router.put("/api/me/sim-positions")
def put_sim_positions(body: PositionsBody, request: Request, symbol: str = "NIFTY", c=Depends(db)):
    from app import admin_extra
    u = getattr(request.state, "user", None)
    if u is not None:
        admin_extra.check_positions(c, u["id"], _symbol(symbol), body.positions)       # per-account limits set by an admin
    data = json.dumps(body.positions, separators=(",", ":"))
    if len(data) > MAX_BYTES:
        raise AuthError(413, "TOO_LARGE", "too many positions to store")
    now = time.time()
    c.execute("INSERT INTO user_sim_positions (user_id, symbol, data_json, updated_at) VALUES (?,?,?,?) "
              "ON CONFLICT(user_id, symbol) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at",
              (user_id_of(request), _symbol(symbol), data, now))
    c.commit()
    from app import option_journal
    option_journal.sync(c, user_id_of(request), _symbol(symbol), body.positions, now)       # the options journal keeps every leg for good
    return {"ok": True, "updated_at": now, "count": len(body.positions)}


class SettingBody(BaseModel):
    value: bool | int | float | str | list | dict | None = None


@router.get("/api/me/settings")
def get_settings(request: Request, c=Depends(db)):
    return {"settings": read_settings(c, user_id_of(request))}


@router.put("/api/me/settings/{key}")
def put_setting(key: str, body: SettingBody, request: Request, c=Depends(db)):
    if not key.replace(".", "").replace("_", "").isalnum() or len(key) > 60:
        raise AuthError(400, "BAD_KEY", "setting names are letters, digits, '.' and '_'")
    data = json.dumps(body.value)
    if len(data) > 5000:
        raise AuthError(413, "TOO_LARGE", "setting too large")
    c.execute("INSERT INTO user_settings (user_id, key, value_json, updated_at) VALUES (?,?,?,?) "
              "ON CONFLICT(user_id, key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
              (user_id_of(request), key, data, time.time()))
    c.commit()
    return {"ok": True}
