"""Known-answer checks ("which function is correct?").

Each check feeds a small FIXED input to a real function and compares with an answer worked out independently (a published value, a
rule written down in the spec, or the result approved by the owner). A check that fails means that function no longer behaves as
agreed, and the developer panel shows WHICH one and the difference. These run on demand and every few minutes; they use no network
and never touch user data.

The two strategy checks pin the approved rules. If a rule is changed ON PURPOSE (with the owner's approval) its expected answer here
must be updated in the same change, so a surprise change cannot slip through unnoticed.
"""
from __future__ import annotations

import datetime as dt
import struct
import time

from app.ops import incidents, stats


def _check_parse_tick():
    from app.live.ticks import parse_tick
    frame = bytes([1, 2]) + b"61466".ljust(25, b"\x00") + struct.pack("<q", 1) + struct.pack("<q", 1792368000000) + struct.pack("<i", 5579580)
    t = parse_tick(frame)
    got = None if t is None else (t.token, t.exchange_type, t.ltp)
    return got == ("61466", 2, 55795.8), ("61466", 2, 55795.8), got


def _check_parse_tick_rejects_junk():
    from app.live.ticks import parse_tick
    got = parse_tick(b"\x01\x02short")
    return got is None, None, got


def _check_black_scholes():
    from app.options import bs
    got = round(bs.price(100, 100, 1.0, 0.0, 0.0, 0.2, True), 4)         # textbook value for S=K=100, T=1, r=0, sigma=20%
    return got == 7.9656, 7.9656, got


def _check_implied_vol_round_trip():
    from app.options import bs
    p = bs.price(22800, 23000, 6 / 365, 0.065, 0.0, 0.15, True)
    iv = bs.implied_vol(p, 22800, 23000, 6 / 365, 0.065, 0.0, True)
    return iv is not None and abs(iv - 0.15) < 1e-3 or (iv is not None and abs(iv - 15.0) < 0.1), "0.15 (or 15 in %)", iv


def _check_swings():
    from app.strategy.top_bottom.swing_detection import detect_confirmed_swings
    closes = [100, 110, 100, 90, 100, 108, 109, 111, 103, 95, 85, 88, 96]
    dates = [dt.datetime(2026, 1, 1) + dt.timedelta(days=i) for i in range(len(closes))]
    got = [(s.kind, s.price, s.pivot_index, s.confirmed_index) for s in detect_confirmed_swings(dates, closes, 1, 1)]
    want = [("TOP", 110, 1, 2), ("BOTTOM", 90, 3, 4), ("TOP", 111, 7, 8), ("BOTTOM", 85, 10, 11)]
    return got == want, want, got


def _check_daily_strategy():
    from app.strategy.top_bottom.backtest import run_top_bottom_backtest
    closes = [100, 110, 100, 90, 100, 108, 109, 111, 103, 95, 85, 88, 96]
    dates = [dt.datetime(2026, 1, 1) + dt.timedelta(days=i) for i in range(len(closes))]
    f = [float(c) for c in closes]
    _, trs = run_top_bottom_backtest("X", "X", "", "STOCK", 1, dates, f, f, None, [c + 1 for c in f], [c - 1 for c in f])
    got = [(t.direction, t.entry_price, t.exit_price, t.exit_reason) for t in trs]
    want = [("BUY", 110.0, 90.0, "TRAILING_STOP_REVERSAL"), ("SELL", 90.0, 90.0, "BREAKEVEN_EXIT_AFTER_2_PERCENT_PROFIT")]
    return got == want, want, got


def _check_contract_cutoff():
    from app.live.contracts import cutoff
    got = (cutoff(dt.date(2026, 10, 27)).isoformat(), cutoff(dt.date(2026, 11, 23)).isoformat())      # 8 days before; a weekend moves to Monday
    want = ("2026-10-19", "2026-11-16")
    return got == want, want, got


def _check_totp_rfc6238():
    import pyotp
    got = pyotp.TOTP("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ").at(59)                                         # RFC 6238 test vector (6 digits)
    return got == "287082", "287082", got


def _check_password_round_trip():
    from app.security import passwords
    h = passwords.hash_password("correct horse battery staple")
    ok = passwords.verify_password(h, "correct horse battery staple") and not passwords.verify_password(h, "wrong")
    return ok, True, ok


def _check_audit_chain():
    import tempfile
    from pathlib import Path
    from app.db import audit, platform_db
    with tempfile.TemporaryDirectory() as d:                         # a throw-away database: the real audit log is never touched
        c = platform_db.connect(Path(d) / "check.db")
        try:
            audit.append(c, "selftest.a")
            audit.append(c, "selftest.b")
            got = audit.verify_chain(c)
        finally:
            c.close()
    return got == (True, None), (True, None), got


CHECKS = [
    ("live.parse_tick", _check_parse_tick), ("live.parse_tick_junk", _check_parse_tick_rejects_junk),
    ("options.black_scholes", _check_black_scholes), ("options.implied_vol", _check_implied_vol_round_trip),
    ("top_bottom.swings", _check_swings), ("top_bottom.daily_strategy", _check_daily_strategy),
    ("live.contract_cutoff", _check_contract_cutoff),
    ("auth.totp_rfc6238", _check_totp_rfc6238), ("auth.password", _check_password_round_trip), ("audit.hash_chain", _check_audit_chain),
]

_last: dict = {"at": None, "results": []}


def run_all(report: bool = True) -> dict:
    results = []
    for name, fn in CHECKS:
        t0 = time.perf_counter()
        try:
            ok, want, got = fn()
            err = None
        except Exception as exc:           # a crash inside the function under test is a failed check, not a crashed panel
            ok, want, got, err = False, "no exception", None, f"{type(exc).__name__}: {str(exc)[:200]}"
        ms = (time.perf_counter() - t0) * 1000
        stats.record(f"selftest.{name}", ms, ok)
        if not ok and report:
            incidents.record(f"selftest.{name}", f"known-answer check failed: expected {want!r}, got {got!r}{' (' + err + ')' if err else ''}",
                             severity="error", auto_action="function marked FAULTY on the board")
        results.append({"name": name, "ok": bool(ok), "expected": repr(want), "actual": repr(got), "error": err, "ms": round(ms, 2)})
    _last.update({"at": time.time(), "results": results})
    return {"at": _last["at"], "passed": sum(r["ok"] for r in results), "failed": sum(not r["ok"] for r in results), "results": results}


def last() -> dict:
    r = _last["results"]
    return {"at": _last["at"], "passed": sum(x["ok"] for x in r), "failed": sum(not x["ok"] for x in r), "results": r}
