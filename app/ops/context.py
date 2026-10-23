"""Handles to the running parts of the app, set once by app/main.py's lifespan (None while the app runs without a lifespan, e.g. in tests)."""
from __future__ import annotations

import time

started_at = time.time()
engine = None            # PaperEngine
