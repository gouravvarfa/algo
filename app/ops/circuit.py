"""Circuit breakers: stop calling a failing service for a short time, so it can recover and callers do not pile up.

CLOSED   normal: calls go through; failures are counted in a sliding window.
OPEN     too many failures: calls fail IMMEDIATELY (CircuitOpen) for `cooldown_s`; nothing is sent to the service.
HALF_OPEN after the cool-down one trial call is let through: success closes the circuit, failure opens it again.
A developer can also force a circuit open or closed (app/ops/api.py); a forced state holds until released.
Failures that are the caller's mistake (bad input) should not be counted: callers pass `count_if` to say which exceptions are real outages.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class CircuitOpen(Exception):
    def __init__(self, name: str, retry_in: float):
        super().__init__(f"circuit '{name}' is open; retry in {retry_in:.0f}s")
        self.name, self.retry_in = name, retry_in


class Breaker:
    def __init__(self, name: str, threshold: int = 8, window_s: float = 60.0, cooldown_s: float = 30.0, on_open: Callable | None = None):
        self.name, self.threshold, self.window_s, self.cooldown_s = name, threshold, window_s, cooldown_s
        self.on_open = on_open
        self._fails: deque[float] = deque()
        self._opened_at: float | None = None
        self._trial = False
        self._forced: str | None = None          # None | 'open' | 'closed'
        self.last_error: str | None = None
        self.opened_count = 0
        self.total_calls = 0
        self.total_failures = 0
        self._lock = threading.Lock()

    # ---- state
    def state(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        if self._forced == "open":
            return "OPEN"
        if self._forced == "closed":
            return "CLOSED"
        if self._opened_at is None:
            return "CLOSED"
        return "HALF_OPEN" if now - self._opened_at >= self.cooldown_s else "OPEN"

    def retry_in(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        if self._forced == "open":
            return float("inf")
        return 0.0 if self._opened_at is None else max(0.0, self.cooldown_s - (now - self._opened_at))

    def force(self, mode: str | None) -> None:
        with self._lock:
            self._forced = mode
            if mode is None:
                self._opened_at, self._trial = None, False
                self._fails.clear()

    def info(self) -> dict:
        now = time.time()
        recent = sum(1 for t in self._fails if now - t <= self.window_s)
        ri = self.retry_in(now)
        return {"name": self.name, "state": self.state(now), "forced": self._forced, "failures_in_window": recent, "threshold": self.threshold,
                "window_s": self.window_s, "cooldown_s": self.cooldown_s, "retry_in_s": None if ri == float("inf") else round(ri, 1),
                "opened_count": self.opened_count, "total_calls": self.total_calls, "total_failures": self.total_failures, "last_error": self.last_error}

    # ---- guarded call
    def _before(self) -> None:
        now = time.time()
        with self._lock:
            self.total_calls += 1
            st = self.state(now)
            if st == "OPEN":
                raise CircuitOpen(self.name, self.retry_in(now))
            if st == "HALF_OPEN":
                if self._trial:
                    raise CircuitOpen(self.name, 1.0)      # one trial at a time
                self._trial = True

    def _success(self) -> None:
        with self._lock:
            if self._forced is None:
                self._opened_at, self._trial = None, False
                self._fails.clear()

    def _failure(self, exc: BaseException) -> None:
        now = time.time()
        opened = False
        with self._lock:
            self.total_failures += 1
            self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            if self._forced is not None:
                return
            if self._trial:                                  # the trial call failed: open again
                self._opened_at, self._trial, opened = now, False, True
            else:
                self._fails.append(now)
                while self._fails and now - self._fails[0] > self.window_s:
                    self._fails.popleft()
                if len(self._fails) >= self.threshold and self._opened_at is None:
                    self._opened_at, opened = now, True
            if opened:
                self.opened_count += 1
        if opened and self.on_open:
            try:
                self.on_open(self)
            except Exception:                                # reporting must never break the call path
                pass

    def call(self, fn: Callable[[], T], count_if: Callable[[BaseException], bool] = lambda e: True) -> T:
        self._before()
        try:
            out = fn()
        except CircuitOpen:
            raise
        except BaseException as exc:
            if count_if(exc):
                self._failure(exc)
            else:
                self._release_trial()
            raise
        self._success()
        return out

    async def acall(self, fn: Callable[[], Awaitable[T]], count_if: Callable[[BaseException], bool] = lambda e: True) -> T:
        self._before()
        try:
            out = await fn()
        except CircuitOpen:
            raise
        except BaseException as exc:
            if count_if(exc):
                self._failure(exc)
            else:
                self._release_trial()
            raise
        self._success()
        return out

    def _release_trial(self) -> None:
        with self._lock:
            self._trial = False


_breakers: dict[str, Breaker] = {}
_reg_lock = threading.Lock()


def breaker(name: str, **kw) -> Breaker:
    """The one breaker of that name (created on first use)."""
    b = _breakers.get(name)
    if b is None:
        with _reg_lock:
            b = _breakers.setdefault(name, Breaker(name, **kw))
    return b


def all_info() -> list[dict]:
    return [b.info() for b in sorted(_breakers.values(), key=lambda x: x.name)]


def reset() -> None:
    with _reg_lock:
        _breakers.clear()
