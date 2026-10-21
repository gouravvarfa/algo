"""Live tick feed from Angel One's SmartAPI WebSocket (wss://smartapisocket.angelone.in/smart-stream).

Why: polling the REST candle endpoint every few seconds hits Angel One's rate limit and is 30-90 s late. The WebSocket pushes every
price change within a second and has no such limit. Prices here are REAL traded prices from the exchange feed: nothing is invented.

Protocol (verified against the live feed): JSON subscribe {"correlationID","action":1,"params":{"mode":1,"tokenList":[{"exchangeType":2,"tokens":["48699"]}]}}
exchangeType 1=NSE cash, 2=NSE F&O, 3=BSE cash, 4=BSE F&O, 5=MCX. Binary replies (little-endian): byte0 mode, byte1 exchange type,
bytes 2-26 token (NUL padded), 27-34 sequence (int64), 35-42 exchange timestamp in ms (int64), 43-46 LTP in paise (int32).
The server expects a text "ping" about every 10 s and answers "pong".
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import struct
import time
from dataclasses import dataclass
from typing import Callable

import websockets

log = logging.getLogger("ticks")
URL = "wss://smartapisocket.angelone.in/smart-stream"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
EXCHANGE_TYPE = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4, "MCX": 5}
HEARTBEAT_SECONDS = 10
STALE_AFTER_SECONDS = 15          # no tick for this long while the socket is "connected" -> treated as not healthy
KEEP_MINUTES = 6 * 60 * 5         # minute candles kept in memory per token (several trading days)


@dataclass(frozen=True)
class Tick:
    token: str
    exchange_type: int
    ltp: float
    exch_ts: dt.datetime       # exchange timestamp (IST)
    recv_at: float             # local monotonic-ish wall clock (time.time())


def parse_tick(data: bytes, recv_at: float | None = None) -> Tick | None:
    """One binary frame -> Tick, or None when it is not a valid LTP frame (never guesses)."""
    if len(data) < 47:
        return None
    mode, exch = struct.unpack("<bb", data[0:2])
    token = data[2:27].split(b"\x00")[0].decode("utf-8", "ignore")
    (ts_ms,) = struct.unpack("<q", data[35:43])
    (ltp_paise,) = struct.unpack("<i", data[43:47])
    if not token or ltp_paise <= 0 or ts_ms <= 0:
        return None
    return Tick(token, exch, ltp_paise / 100.0, dt.datetime.fromtimestamp(ts_ms / 1000.0, IST), recv_at or time.time())


class MinuteBuilder:
    """1-minute OHLC built ONLY from received ticks (by exchange time). High/low are the extremes of the ticks we received,
    so a very brief spike between two ticks can be missed: that is a property of tick data, stated here, not hidden."""

    def __init__(self) -> None:
        self.bars: dict[str, dict[dt.datetime, list[float]]] = {}

    def add(self, t: Tick) -> None:
        m = t.exch_ts.replace(second=0, microsecond=0)
        bars = self.bars.setdefault(t.token, {})
        b = bars.get(m)
        if b is None:
            bars[m] = [t.ltp, t.ltp, t.ltp, t.ltp]
            if len(bars) > KEEP_MINUTES:
                for old in sorted(bars)[: len(bars) - KEEP_MINUTES]:
                    del bars[old]
        else:
            b[1], b[2], b[3] = max(b[1], t.ltp), min(b[2], t.ltp), t.ltp

    def minutes(self, token: str) -> list[tuple[dt.datetime, float, float, float, float]]:
        return [(m, *b) for m, b in sorted(self.bars.get(token, {}).items())]


def merge_minutes(rest: list, ticks: list[tuple]) -> list:
    """REST candles are authoritative for the minutes they cover; tick-built minutes only FILL minutes REST does not have
    (the minute that just closed, and the one still forming). `rest` items need .ts/.open/.high/.low/.close and a class that can
    be built as cls(ts, o, h, l, c); returns a time-sorted list."""
    if not ticks:
        return list(rest)
    cls = type(rest[0]) if rest else None
    if cls is None:
        return []
    aware = rest[0].ts.tzinfo is not None
    have = {m.ts.replace(second=0, microsecond=0) for m in rest}
    out = list(rest)
    for ts, o, h, l, c in ticks:
        key = ts if aware else ts.replace(tzinfo=None)
        if key not in have:
            out.append(cls(key, o, h, l, c))
    return sorted(out, key=lambda m: m.ts)


class TickFeed:
    """Keeps one WebSocket open, reconnects with backoff, re-subscribes, and exposes the latest price + health."""

    def __init__(self, get_session: Callable, reset_session: Callable | None = None) -> None:
        self._get_session, self._reset_session = get_session, reset_session
        self.latest: dict[str, Tick] = {}
        self.builder = MinuteBuilder()
        self.subs: dict[int, set[str]] = {}
        self.connected = False
        self.reconnects = 0
        self.last_error: str | None = None
        self.updated = asyncio.Event()           # set on every accepted tick; consumers clear it
        self._ws = None
        self._task: asyncio.Task | None = None
        self._stop = False

    # ---- public
    def subscribe(self, exch_seg: str, token: str) -> None:
        et = EXCHANGE_TYPE.get(exch_seg.upper())
        if et is None:
            raise ValueError(f"unsupported exchange segment {exch_seg}")
        self.subs.setdefault(et, set()).add(str(token))
        if self._ws is not None and self.connected:
            asyncio.ensure_future(self._send_subscribe({et: {str(token)}}))

    def unsubscribe_all_for(self, token: str) -> None:
        for toks in self.subs.values():
            toks.discard(str(token))

    def start(self) -> None:
        self._stop = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()

    def age(self, token: str) -> float | None:
        t = self.latest.get(str(token))
        return None if t is None else max(0.0, time.time() - t.recv_at)

    def healthy(self, token: str | None = None) -> bool:
        """Connected AND a tick arrived recently (for `token`, or for any token)."""
        if not self.connected:
            return False
        ages = [self.age(token)] if token else [self.age(k) for k in self.latest]
        return any(a is not None and a < STALE_AFTER_SECONDS for a in ages)

    def status(self) -> dict:
        ages = [a for a in (self.age(k) for k in self.latest) if a is not None]
        return {"connected": self.connected, "last_tick_age_s": round(min(ages), 1) if ages else None, "reconnects": self.reconnects,
                "subscribed": sum(len(v) for v in self.subs.values()), "error": self.last_error}

    # ---- internals
    def _sub_message(self, subs: dict[int, set[str]]) -> str:
        return json.dumps({"correlationID": "algo-app", "action": 1,
                           "params": {"mode": 1, "tokenList": [{"exchangeType": et, "tokens": sorted(toks)} for et, toks in subs.items() if toks]}})

    async def _send_subscribe(self, subs: dict[int, set[str]]) -> None:
        if any(subs.values()) and self._ws is not None:
            try:
                await self._ws.send(self._sub_message(subs))
            except Exception as exc:  # the reconnect loop re-subscribes everything
                log.warning("subscribe failed: %s", exc)

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await ws.send("ping")

    async def _run(self) -> None:
        delay = 1.0
        while not self._stop:
            hb = None
            try:
                s = await self._get_session()
                headers = {"Authorization": s.jwt_token, "x-api-key": s.api_key, "x-client-code": s.client_code, "x-feed-token": s.feed_token}
                async with websockets.connect(URL, additional_headers=headers, ping_interval=None, open_timeout=15, max_size=2**20) as ws:
                    self._ws, self.connected, self.last_error = ws, True, None
                    delay = 1.0
                    await self._send_subscribe(self.subs)
                    hb = asyncio.create_task(self._heartbeat(ws))
                    log.info("tick feed connected (%d tokens)", sum(len(v) for v in self.subs.values()))
                    async for msg in ws:
                        if isinstance(msg, (bytes, bytearray)):
                            t = parse_tick(bytes(msg))
                            if t is not None:
                                self.latest[t.token] = t
                                self.builder.add(t)
                                self.updated.set()
                        elif isinstance(msg, str) and msg != "pong":
                            try:
                                body = json.loads(msg)
                            except ValueError:
                                body = {"raw": msg[:120]}
                            if body.get("errorCode") or body.get("errorMessage") or body.get("status") is False:
                                self.last_error = str(body)[:200]
                                log.warning("tick feed message: %s", self.last_error)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                log.warning("tick feed dropped: %s", self.last_error)
                if self._reset_session and ("401" in self.last_error or "403" in self.last_error or "Unauthorized" in self.last_error):
                    self._reset_session()          # token expired: log in again on the next attempt
            finally:
                self.connected, self._ws = False, None
                if hb:
                    hb.cancel()
            if self._stop:
                break
            self.reconnects += 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
