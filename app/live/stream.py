"""Push of live ticks to the browser over Server-Sent Events (SSE). An OBSERVER of the tick feed: it only listens (TickFeed.listeners), never
writes to the paper engine, the strategy or the shadow account, and a failure here only costs the browser its live updates (it falls back to polling).

Why SSE: the data only ever flows server -> browser; the browser reconnects by itself; the same login cookie works; nginx needs only the
`X-Accel-Buffering: no` header the response already carries (no nginx edit).

How it stays light: every client names the tokens it wants (`NFO:61466,NSE:99926009`); a tick marks only those clients; the pump waits a few
milliseconds so several ticks join one message and keeps only the LATEST price per token (conflation), so a slow browser never builds a queue.

Message format (one JSON object, compact):
  event: snap   data: {"p": {"61466": [ltp, exch_ms], ...}, "feed": true}      the latest known price of each requested token, sent first
  event: ticks  data: {"t": server_ms, "p": {"61466": [ltp, exch_ms], ...}}     only the tokens that changed since the last message
  event: bye    data: {"reason": "..."}                                          the stream ends (signed out, lifetime over); the browser reconnects
  ": hb"                                                                         a comment every HEARTBEAT_SECONDS to keep proxies from closing it"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("stream")

MAX_TOKENS_PER_CLIENT = 400
MAX_CLIENTS = 200
BATCH_SECONDS = 0.15              # ticks arriving within this window travel in one message
HEARTBEAT_SECONDS = 15
RECHECK_SECONDS = 60              # the login is checked again this often; a signed-out browser is cut off (the daily 16:00 sign-out included)
MAX_LIFETIME_SECONDS = 3600       # then the stream ends and the browser reconnects (this also clears any half-dead connection)
SPEC = re.compile(r"^[A-Z]{3,4}:\d{1,10}$")


@dataclass
class Client:
    id: int
    user: str | None
    specs: list[tuple[str, str]]
    tokens: set[str]
    ua: str
    opened: float = field(default_factory=time.time)
    event: asyncio.Event = field(default_factory=asyncio.Event)
    pending: dict[str, tuple[float, int, float]] = field(default_factory=dict)       # token -> (ltp, exchange ms, received at)
    msgs: int = 0
    bytes: int = 0


class StreamError(ValueError):
    pass


def parse_specs(raw: str) -> list[tuple[str, str]]:
    """'NFO:61466,NSE:99926009' -> [('NFO', '61466'), ('NSE', '99926009')]; anything malformed is refused, never guessed."""
    out, seen = [], set()
    for part in (raw or "").split(","):
        part = part.strip().upper()
        if not part:
            continue
        if not SPEC.match(part):
            raise StreamError(f"bad token spec {part[:20]!r}: use EXCHANGE:TOKEN, e.g. NFO:61466")
        exch, tok = part.split(":")
        if tok not in seen:
            seen.add(tok)
            out.append((exch, tok))
    if not out:
        raise StreamError("no tokens requested")
    if len(out) > MAX_TOKENS_PER_CLIENT:
        raise StreamError(f"at most {MAX_TOKENS_PER_CLIENT} tokens per stream")
    return out


class StreamHub:
    def __init__(self, feed) -> None:
        self.feed = feed
        self.clients: dict[int, Client] = {}
        self.by_token: dict[str, set[int]] = {}
        self._ids = itertools.count(1)
        self.connects = self.disconnects = self.errors = 0
        self._sent: deque[tuple[float, int, int]] = deque(maxlen=20000)       # (time, messages, bytes) for the per-second rates
        self._lag: deque[tuple[float, float]] = deque(maxlen=5000)             # (time, ms from tick received to message written)

    # ------------------------------------------------------------------ feed side
    def on_tick(self, tick) -> None:
        ids = self.by_token.get(tick.token)
        if not ids:
            return
        row = (tick.ltp, int(tick.exch_ts.timestamp() * 1000), tick.recv_at)
        for cid in ids:
            c = self.clients.get(cid)
            if c is not None:
                c.pending[tick.token] = row            # conflation: the latest price per token wins
                c.event.set()

    # ------------------------------------------------------------------ clients
    def open(self, user: str | None, specs: list[tuple[str, str]], ua: str = "") -> Client:
        if len(self.clients) >= MAX_CLIENTS:
            raise StreamError("too many live streams open right now")
        for exch, tok in specs:
            self.feed.subscribe(exch, tok)             # idempotent; the feed re-subscribes after every reconnect
        c = Client(next(self._ids), user, specs, {t for _, t in specs}, ua[:80])
        self.clients[c.id] = c
        for t in c.tokens:
            self.by_token.setdefault(t, set()).add(c.id)
        self.connects += 1
        return c

    def close(self, c: Client) -> None:
        if self.clients.pop(c.id, None) is None:
            return
        for t in c.tokens:
            ids = self.by_token.get(t)
            if ids:
                ids.discard(c.id)
                if not ids:
                    self.by_token.pop(t, None)
        self.disconnects += 1

    # ------------------------------------------------------------------ the stream itself
    @staticmethod
    def frame(event: str, data: dict) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()

    def _count(self, c: Client, n: int, lag_from: list[float] | None = None) -> None:
        now = time.time()
        c.msgs += 1
        c.bytes += n
        self._sent.append((now, 1, n))
        for r in lag_from or ():
            self._lag.append((now, (now - r) * 1000.0))

    async def events(self, c: Client, still_allowed: Callable[[], bool]):
        """Async generator of SSE bytes for one client. Always unregisters the client when it ends (disconnect, sign-out, lifetime)."""
        try:
            yield b"retry: 2000\n\n"
            snap = {}
            for t in c.tokens:
                tk = self.feed.latest.get(t)
                if tk is not None:
                    snap[t] = [tk.ltp, int(tk.exch_ts.timestamp() * 1000)]
            out = self.frame("snap", {"p": snap, "feed": bool(self.feed.connected)})
            self._count(c, len(out))
            yield out
            last_check = time.time()
            while True:
                try:
                    await asyncio.wait_for(c.event.wait(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield b": hb\n\n"
                else:
                    await asyncio.sleep(BATCH_SECONDS)
                    c.event.clear()
                    batch, c.pending = c.pending, {}                 # clear and swap without awaiting in between: no tick is lost
                    if batch:
                        out = self.frame("ticks", {"t": int(time.time() * 1000), "p": {t: [v[0], v[1]] for t, v in batch.items()}})
                        self._count(c, len(out), [v[2] for v in batch.values()])
                        yield out
                now = time.time()
                if now - c.opened > MAX_LIFETIME_SECONDS:
                    yield self.frame("bye", {"reason": "refresh"})
                    return
                if now - last_check > RECHECK_SECONDS:
                    last_check = now
                    if not still_allowed():
                        yield self.frame("bye", {"reason": "signed out"})
                        return
        except asyncio.CancelledError:
            raise
        except Exception:
            self.errors += 1
            log.exception("stream %s failed", c.id)
        finally:
            self.close(c)

    # ------------------------------------------------------------------ developer panel
    def stats(self) -> dict:
        now = time.time()
        recent = [x for x in list(self._sent) if now - x[0] <= 10]
        lags = sorted(x[1] for x in list(self._lag) if now - x[0] <= 60)
        pct = lambda p: round(lags[min(len(lags) - 1, int(len(lags) * p))], 1) if lags else None
        return {"clients": len(self.clients), "tokens_watched": len(self.by_token), "connects": self.connects, "disconnects": self.disconnects, "errors": self.errors,
                "msgs_per_s": round(sum(x[1] for x in recent) / 10, 2), "bytes_per_s": round(sum(x[2] for x in recent) / 10, 1),
                "lag_ms_avg": round(sum(lags) / len(lags), 1) if lags else None, "lag_ms_p95": pct(0.95), "lag_samples": len(lags),
                "feed_connected": bool(self.feed.connected),
                "list": [{"id": c.id, "user": (c.user or "-")[:8], "tokens": len(c.tokens), "age_s": round(now - c.opened), "msgs": c.msgs, "bytes": c.bytes, "ua": c.ua}
                         for c in sorted(self.clients.values(), key=lambda c: c.opened)][:60]}
