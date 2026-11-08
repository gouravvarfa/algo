"""The last few thousand log lines of this process, kept in memory for the developer's Live logs page.

On the server the logs go to systemd's journal (there is no log file the app could read), so the app keeps its own copy: every record of the app's
loggers and of uvicorn (requests and errors) lands here as one formatted line. Secrets are masked when the page reads them.
"""
from __future__ import annotations

import logging
from collections import deque

LINES: deque = deque(maxlen=3000)
_FMT = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S")
_installed = False


class _Ring(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = _FMT.format(record)
            LINES.extend(line.splitlines() or [line])
        except Exception:
            pass                                    # logging must never break the app


def install() -> None:
    """Attach once to the root logger and to uvicorn's loggers (which do not pass their records up to the root)."""
    global _installed
    if _installed:
        return
    h = _Ring(level=logging.INFO)
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.addHandler(h)
        if name == "" and lg.level > logging.INFO:
            lg.setLevel(logging.INFO)
    _installed = True


def tail(n: int) -> list[str]:
    return list(LINES)[-n:]
