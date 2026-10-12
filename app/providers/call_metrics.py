"""
Counts real Angel One calls actually issued, so usage is observable rather
than estimated. Angel One has no daily quota (unlike the Tapetide free tier
this project used to also track), just a per-second rate limit, but the
count is still useful for spotting an unexpectedly chatty scan.

Nothing here ever issues a call of its own; every number is recorded by the
code paths already making the calls.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
METRICS_PATH = Path(".provider_call_metrics.json")


def _today_ist() -> str:
    return dt.datetime.now(IST).date().isoformat()


@dataclass
class CallMetrics:
    day: str
    angelone_calls: int = 0


_lock = Lock()
_metrics: CallMetrics | None = None


def _load() -> CallMetrics:
    today = _today_ist()
    if METRICS_PATH.exists():
        try:
            data = json.loads(METRICS_PATH.read_text())
            if data.get("day") == today:
                return CallMetrics(**data)
        except (json.JSONDecodeError, TypeError):
            pass  # a corrupt counter file must never break a scan — start fresh
    return CallMetrics(day=today)


def _save(metrics: CallMetrics) -> None:
    try:
        METRICS_PATH.write_text(json.dumps(asdict(metrics), indent=2))
    except OSError:
        pass  # metrics are diagnostics; failing to persist them must not break a scan


def record_angelone_call() -> None:
    """One real HTTP call actually sent to Angel One."""
    global _metrics
    with _lock:
        if _metrics is None or _metrics.day != _today_ist():
            _metrics = _load()
        _metrics.angelone_calls += 1
        _save(_metrics)


def snapshot() -> CallMetrics:
    global _metrics
    with _lock:
        if _metrics is None or _metrics.day != _today_ist():
            _metrics = _load()
        return CallMetrics(**asdict(_metrics))


def reset_for_tests() -> None:
    global _metrics
    with _lock:
        _metrics = CallMetrics(day=_today_ist())
