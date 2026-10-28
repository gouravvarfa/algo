"""Live prices from Upstox, with the same surface as the Angel One WebSocket feed (app/live/ticks.py TickFeed), so the engine does not care which one it has.

How: while the market is open the last traded price of every watched instrument is asked for about every 1.5 seconds (one light request for all of
them), and 1-minute candles are built from those prices exactly as TickFeed does. A price is a real traded price from Upstox: nothing is invented; a
very brief spike between two requests can be missed (stated here, like in MinuteBuilder). When the market is shut the prices are asked for every few
seconds only, to keep the last price fresh. With no Upstox login the feed says so (status()['error']) and stays not connected.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

from app.live.ticks import IST, STALE_AFTER_SECONDS, MinuteBuilder, Tick
from app.providers import upstox_client

log = logging.getLogger("upstox_feed")
POLL_OPEN_S = 1.5
POLL_CLOSED_S = 8.0
NO_LOGIN_RETRY_S = 5.0
MAX_BACKOFF_S = 30.0


def _market_open(now: dt.datetime) -> bool:
    return now.weekday() < 5 and dt.time(9, 15) <= now.time() <= dt.time(15, 31)


class UpstoxPollFeed:
    source = "Upstox"

    def __init__(self, provider) -> None:
        self._provider = provider
        self.latest: dict[str, Tick] = {}
        self.builder = MinuteBuilder()
        self.subs: dict[int, set[str]] = {}
        self.connected = False
        self.reconnects = 0
        self.last_error: str | None = None
        self.updated = asyncio.Event()           # set on every price; consumers clear it
        self._task: asyncio.Task | None = None
        self._stop = False

    # ---- the same public surface as TickFeed ---------------------------------------------------------------------------------------------
    def subscribe(self, exch_seg: str, token: str) -> None:
        self.subs.setdefault(1, set()).add(str(token))

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

    async def restart(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.connected, self.last_error = False, None
        self.start()

    def age(self, token: str) -> float | None:
        t = self.latest.get(str(token))
        return None if t is None else max(0.0, time.time() - t.recv_at)

    def healthy(self, token: str | None = None) -> bool:
        if not self.connected:
            return False
        ages = [self.age(token)] if token else [self.age(k) for k in self.latest]
        return any(a is not None and a < STALE_AFTER_SECONDS for a in ages)

    def status(self) -> dict:
        ages = [a for a in (self.age(k) for k in self.latest) if a is not None]
        return {"connected": self.connected, "last_tick_age_s": round(min(ages), 1) if ages else None, "reconnects": self.reconnects,
                "subscribed": sum(len(v) for v in self.subs.values()), "error": self.last_error, "source": self.source}

    # ---- the loop ------------------------------------------------------------------------------------------------------------------------
    def keys(self) -> list[str]:
        return sorted({k for toks in self.subs.values() for k in toks})

    async def poll_once(self) -> int:
        """Ask for every watched price once. Returns how many prices arrived. Raises what the provider / Upstox raise."""
        keys = self.keys()
        if not keys:
            return 0
        token = self._provider.access_token()
        prices = await upstox_client.get_ltp(token, keys)
        now_ist = dt.datetime.now(IST)
        got = time.time()
        accepted = 0
        for key, ltp in prices.items():
            if ltp > 0:                                  # a zero or missing price is not a trade
                t = Tick(key, 1, ltp, now_ist, got)
                self.latest[key] = t
                self.builder.add(t)
                accepted += 1
        if accepted:
            self.updated.set()
        return len(prices)

    async def _run(self) -> None:
        delay = 1.0
        while not self._stop:
            wait = POLL_OPEN_S if _market_open(dt.datetime.now(IST).replace(tzinfo=None)) else POLL_CLOSED_S
            try:
                n = await self.poll_once()
                if n or not self.keys():
                    self.connected, self.last_error, delay = True, None, 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
                if isinstance(exc, upstox_client.UpstoxApiError) and exc.kind == "auth":
                    self._provider.forget_login()
                self.reconnects += 1
                wait = NO_LOGIN_RETRY_S if "UpstoxNotConnected" in self.last_error else delay
                delay = min(delay * 2, MAX_BACKOFF_S)
                log.warning("upstox price feed: %s", self.last_error)
            await asyncio.sleep(wait)
