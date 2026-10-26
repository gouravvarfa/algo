"""Developer console > Upstox: save the Upstox APP details once, then log in on Upstox's own page (OAuth). Read only: it shows the connection and today's
trades, it never places an order. Reads need `system.read`, anything that changes something needs `system.manage` and is written to the audit log
(never with a secret or a token). The API secret and the access token never go to a browser."""
from __future__ import annotations

import datetime as dt
import secrets
import time
import urllib.parse

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth_api import db, require
from app.db import audit
from app.providers import upstox_client
from app.security.users import AuthError
from app.services import upstox_credential_store as store

router = APIRouter()

STATE_TTL_S = 600                       # a login started here must come back within 10 minutes
_states: dict[str, tuple[str, float]] = {}      # state -> (developer id, expires at): proves the way back belongs to a login WE started


def _audit(c, actor, action: str, target_id: str | None = None, detail: dict | None = None) -> None:
    audit.append(c, action, actor_id=actor["id"], actor_role=actor["role"], target_type="broker", target_id=target_id, detail=detail)


def _forget_old_states() -> None:
    now = time.time()
    for k in [k for k, (_, exp) in _states.items() if exp < now]:
        _states.pop(k, None)


@router.get("/api/dev/upstox")
def dev_upstox(_=Depends(require("system.read"))):
    return store.status()


class AppBody(BaseModel):
    api_key: str = Field(max_length=200)
    api_secret: str = Field(max_length=200)
    redirect_uri: str = Field(max_length=300)


@router.put("/api/dev/upstox/app")
def dev_upstox_save_app(body: AppBody, actor=Depends(require("system.manage")), c=Depends(db)):
    """Save the details of the Upstox app (from the developer page of your Upstox account). A new app means a new login, so any old token is dropped."""
    key, secret, uri = body.api_key.strip(), body.api_secret.strip(), body.redirect_uri.strip()
    if len(key) < 8 or len(secret) < 4:
        raise AuthError(400, "BAD_DETAILS", "Check the API key and the API secret: one of them is empty or too short.")
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or " " in uri or parsed.fragment:
        raise AuthError(400, "BAD_REDIRECT", "The redirect URI must be a full web address such as http://127.0.0.1:8020/app/dev/upstox, exactly as registered in your Upstox app.")
    store.save(store.UpstoxCredentials(api_key=key, api_secret=secret, redirect_uri=uri, updated_by=actor["username"]))
    _audit(c, actor, "dev.upstox.app_saved", target_id=store.mask(key), detail={"redirect_uri": uri})
    return store.status()


@router.post("/api/dev/upstox/login")
def dev_upstox_login(actor=Depends(require("system.manage")), c=Depends(db)):
    """Where to send the browser to log in on Upstox. The 'state' ties the way back to this developer and to this moment."""
    creds = store.get()
    if creds is None:
        raise AuthError(409, "NO_APP", "Save the Upstox API key, secret and redirect URI first.")
    _forget_old_states()
    state = secrets.token_urlsafe(24)
    _states[state] = (actor["id"], time.time() + STATE_TTL_S)
    _audit(c, actor, "dev.upstox.login_started", target_id=store.mask(creds.api_key))
    return {"url": upstox_client.build_login_url(creds.api_key, creds.redirect_uri, state)}


class CallbackBody(BaseModel):
    code: str = Field(min_length=1, max_length=2000)
    state: str = Field(min_length=1, max_length=200)


@router.post("/api/dev/upstox/callback")
async def dev_upstox_callback(body: CallbackBody, actor=Depends(require("system.manage")), c=Depends(db)):
    """The page Upstox sent the person back to hands over the one-time code. Exchange it for the access token (this needs the secret, so it is done here)."""
    _forget_old_states()
    owner, expires = _states.pop(body.state, (None, 0.0))
    if owner != actor["id"] or expires < time.time():
        raise AuthError(400, "BAD_STATE", "This Upstox login was not started here or has expired. Press Connect with Upstox again.")
    creds = store.get()
    if creds is None:
        raise AuthError(409, "NO_APP", "Save the Upstox API key, secret and redirect URI first.")
    try:
        token = await upstox_client.exchange_code(body.code, creds.api_key, creds.api_secret, creds.redirect_uri)
    except upstox_client.UpstoxApiError as exc:
        _audit(c, actor, "dev.upstox.connect_failed", target_id=store.mask(creds.api_key), detail={"kind": exc.kind})
        if exc.kind == "auth":
            raise AuthError(400, "UPSTOX_REJECTED", f"Upstox refused the login: {str(exc)[:160]}. Check the API key, secret and redirect URI, then try again.")
        raise AuthError(502, "UPSTOX_UNREACHABLE", f"Could not reach Upstox: {str(exc)[:160]}.")
    now = dt.datetime.now(store.IST)
    creds.access_token = token["access_token"]
    creds.token_expires_at = store.next_token_expiry(now).isoformat(timespec="seconds")
    creds.user_id = str(token.get("user_id") or "") or None
    creds.user_name = str(token.get("user_name") or "") or None
    creds.connected_at = now.isoformat(timespec="seconds")
    creds.updated_by = actor["username"]
    store.save(creds)
    _audit(c, actor, "dev.upstox.connect", target_id=creds.user_id, detail={"user_id": creds.user_id, "token_until": creds.token_expires_at})
    return store.status()


TRADE_KEYS = ("trade_id", "order_id", "exchange", "trading_symbol", "instrument_token", "transaction_type", "quantity", "average_price", "price",
              "product", "order_timestamp", "exchange_timestamp", "trade_time")


@router.get("/api/dev/upstox/trades")
async def dev_upstox_trades(_=Depends(require("system.read"))):
    """Today's trades from Upstox, read only, so a developer can see the connection really works."""
    creds = store.get()
    state = store.token_state(creds)
    if state != "connected":
        raise AuthError(409, "NOT_CONNECTED", "Upstox is not logged in (the login ends every day at about 03:30 IST): press Connect with Upstox." if state == "expired"
                        else "Upstox is not connected yet.")
    try:
        rows = await upstox_client.get_trades_today(creds.access_token)
    except upstox_client.UpstoxApiError as exc:
        if exc.kind == "auth":                       # Upstox ended the login early: forget the token so the page says "log in again"
            creds.access_token = None
            store.save(creds)
            raise AuthError(409, "NOT_CONNECTED", "Upstox ended the login. Press Connect with Upstox again.")
        raise AuthError(502, "UPSTOX_ERROR", f"Upstox did not answer: {str(exc)[:160]}.")
    return {"count": len(rows), "trades": [{k: r.get(k) for k in TRADE_KEYS if k in r} for r in rows[:200] if isinstance(r, dict)]}


@router.delete("/api/dev/upstox")
def dev_upstox_disconnect(actor=Depends(require("system.manage")), c=Depends(db)):
    before = store.status()
    store.clear()
    _audit(c, actor, "dev.upstox.disconnect", target_id=before["user_id"] or before["api_key"])
    return store.status()
