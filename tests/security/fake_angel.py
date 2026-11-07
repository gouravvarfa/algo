"""A fake Angel One SmartAPI server for end-to-end tests: the app's real HTTP code (login, headers, JSON bodies, answer parsing) talks to it through httpx.
It checks what the real API documents (required headers, the bearer token, the order fields) and keeps a tiny order book that fills only when a test says so.
Made-up data only. Nothing here can reach Angel One."""
from __future__ import annotations

import itertools

import httpx
import pyotp
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ORDER_KEYS = {"variety", "tradingsymbol", "symboltoken", "transactiontype", "exchange", "ordertype", "producttype", "duration", "price", "quantity"}


class FakeAngel:
    def __init__(self, client_code="A123456", pin="1234", api_key="KEY12345", totp_secret="JBSWY3DPEHPK3PXP", lot_sizes=None):
        self.client_code, self.pin, self.api_key, self.totp_secret = client_code, pin, api_key, totp_secret
        self.lot_sizes = lot_sizes or {"555": 30}
        self.orders: dict[str, dict] = {}
        self.requests: list[tuple[str, dict]] = []
        self.ids = itertools.count(1000)
        self.ltp, self.cash, self.margin = 50001.0, 500000.0, 100000.0
        self.timeout_paths: set[str] = set()
        self.reject_next: str | None = None
        self.jwt = "jwt-fake-1"
        self.app = self._build()

    # ---- what a test does ----
    def fill(self, oid: str, avg: float) -> None:
        o = self.orders[oid]
        o.update(status="complete", averageprice=str(avg), filledshares=o["quantity"])

    def open_orders(self, ordertype=None):
        return [o for o in self.orders.values() if o["status"] in ("open", "trigger pending") and (ordertype is None or o["ordertype"] == ordertype)]

    def net(self, symbol):
        n = 0
        for o in self.orders.values():
            if o["status"] == "complete" and o["tradingsymbol"] == symbol:
                n += int(o["quantity"]) * (1 if o["transactiontype"] == "BUY" else -1)
        return n

    # ---- the server ----
    def _build(self) -> FastAPI:
        app = FastAPI()
        fa = self

        def bad(msg, code="AB1004"):
            return JSONResponse({"status": False, "message": msg, "errorcode": code, "data": None})

        def authed(req: Request) -> bool:
            return req.headers.get("X-PrivateKey") == fa.api_key and req.headers.get("Authorization") == f"Bearer {fa.jwt}"

        @app.post("/rest/auth/angelbroking/user/v1/loginByPassword")
        async def login(req: Request):
            b = await req.json()
            fa.requests.append(("login", {k: ("***" if k in ("password", "totp") else v) for k, v in b.items()}))
            if req.headers.get("X-PrivateKey") != fa.api_key:
                return bad("Invalid API Key", "AG8004")
            if b.get("clientcode") != fa.client_code or b.get("password") != fa.pin:
                return bad("Invalid clientcode or password", "AB1007")
            if not pyotp.TOTP(fa.totp_secret).verify(b.get("totp", ""), valid_window=1):
                return bad("Invalid totp", "AB1050")
            return {"status": True, "message": "SUCCESS", "data": {"jwtToken": fa.jwt, "refreshToken": "r", "feedToken": "f"}}

        @app.post("/rest/secure/angelbroking/user/v1/logout")
        async def logout(req: Request):
            return {"status": True, "message": "SUCCESS", "data": ""}

        @app.get("/rest/secure/angelbroking/user/v1/getRMS")
        async def rms(req: Request):
            if not authed(req):
                return bad("Invalid Token", "AG8001")
            return {"status": True, "data": {"availablecash": str(fa.cash), "net": str(fa.cash), "m2mrealized": "0", "m2munrealized": "0"}}

        @app.get("/rest/secure/angelbroking/order/v1/getPosition")
        async def positions(req: Request):
            if not authed(req):
                return bad("Invalid Token", "AG8001")
            syms = {o["tradingsymbol"] for o in fa.orders.values()}
            rows = [{"tradingsymbol": s, "netqty": str(fa.net(s))} for s in syms if fa.net(s)]
            return {"status": True, "data": rows or None}

        @app.get("/rest/secure/angelbroking/order/v1/getOrderBook")
        async def book(req: Request):
            if not authed(req):
                return bad("Invalid Token", "AG8001")
            return {"status": True, "data": [dict(o) for o in fa.orders.values()] or None}

        @app.post("/rest/secure/angelbroking/order/v1/getLtpData")
        async def ltp(req: Request):
            if not authed(req):
                return bad("Invalid Token", "AG8001")
            return {"status": True, "data": {"ltp": fa.ltp}}

        @app.post("/rest/secure/angelbroking/margin/v1/batch")
        async def margin(req: Request):
            if not authed(req):
                return bad("Invalid Token", "AG8001")
            return {"status": True, "data": {"totalMarginRequired": fa.margin}}

        @app.post("/rest/secure/angelbroking/order/v1/placeOrder")
        async def place(req: Request):
            if not authed(req):
                return bad("Invalid Token", "AG8001")
            b = await req.json()
            fa.requests.append(("place", b))
            missing = ORDER_KEYS - set(b)
            if missing:
                return bad(f"missing {sorted(missing)}")
            if b["variety"] == "STOPLOSS" and (b["ordertype"] != "STOPLOSS_LIMIT" or "triggerprice" not in b):
                return bad("stoploss order needs STOPLOSS_LIMIT and a trigger")
            if not all(isinstance(b[k], str) for k in ("price", "quantity")):
                return bad("price and quantity are strings in SmartAPI")
            lot = fa.lot_sizes.get(b["symboltoken"], 1)
            if int(b["quantity"]) % lot:
                return bad(f"quantity must be a multiple of the lot size {lot}")
            if fa.reject_next:
                msg, fa.reject_next = fa.reject_next, None
                return bad(msg)
            oid = str(next(fa.ids))
            fa.orders[oid] = {**b, "orderid": oid, "status": "trigger pending" if b["variety"] == "STOPLOSS" else "open", "filledshares": "0", "averageprice": "0", "text": ""}
            return {"status": True, "message": "SUCCESS", "data": {"script": b["tradingsymbol"], "orderid": oid, "uniqueorderid": "u" + oid}}

        @app.post("/rest/secure/angelbroking/order/v1/modifyOrder")
        async def modify(req: Request):
            if not authed(req):
                return bad("Invalid Token", "AG8001")
            b = await req.json()
            fa.requests.append(("modify", b))
            o = fa.orders.get(b.get("orderid", ""))
            if o is None or o["status"] not in ("open", "trigger pending"):
                return bad("order not open")
            if "transactiontype" in b:
                return bad("modify does not take transactiontype")
            o.update({k: v for k, v in b.items() if k in ("price", "triggerprice", "quantity")})
            return {"status": True, "message": "SUCCESS", "data": {"orderid": o["orderid"]}}

        @app.post("/rest/secure/angelbroking/order/v1/cancelOrder")
        async def cancel(req: Request):
            if not authed(req):
                return bad("Invalid Token", "AG8001")
            b = await req.json()
            fa.requests.append(("cancel", b))
            o = fa.orders.get(b.get("orderid", ""))
            if o is None or o["status"] not in ("open", "trigger pending"):
                return bad("order cannot be cancelled")
            o["status"] = "cancelled"
            return {"status": True, "message": "SUCCESS", "data": {"orderid": o["orderid"]}}

        return app

    def transport(self) -> httpx.AsyncBaseTransport:
        """An httpx transport into this app, which can also pretend a request timed out (the app cannot tell whether it arrived)."""
        inner = httpx.ASGITransport(app=self.app)
        fa = self

        class T(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if request.url.path in fa.timeout_paths:
                    fa.timeout_paths.discard(request.url.path)
                    await inner.handle_async_request(request)           # it DID arrive...
                    raise httpx.ReadTimeout("timed out", request=request)  # ...but the answer never came back
                return await inner.handle_async_request(request)

        return T()
