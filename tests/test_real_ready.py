"""Which paper results a real account may act on, and the live evaluation going on through refused candle requests (deep check, 10 Nov 2026).
Candles are SYNTHETIC test fixtures."""
import asyncio
import datetime as dt
import json

import pandas as pd
import pytest

from app.paper_trading import engine as em
from app.strategy.top_bottom.intraday import Minute

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def no_candle_sharing(monkeypatch):
    """A simulated clock: the cross-user candle sharing (15 s of REAL time) and the call gap would hide or slow the Angel One calls checked here."""
    monkeypatch.setattr(em, "CANDLE_SHARE_SECONDS", 0)
    monkeypatch.setattr(em, "CANDLE_CALL_GAP", 0.0)


def _daily(n=60, start=100.0):
    idx = pd.date_range("2026-08-03", periods=n, freq="B", tz=IST)
    base = [start + 6 * (1 if (i // 6) % 2 == 0 else -1) * ((i % 6) + 1) / 3 for i in range(n)]
    return pd.DataFrame({"open": base, "high": [b + 2 for b in base], "low": [b - 2 for b in base], "close": base, "volume": [0] * n}, index=idx)


def _engine(tmp_path, monkeypatch):
    monkeypatch.setattr(em, "STATE_PATH", tmp_path / "w.json")
    monkeypatch.setattr(em, "JOURNAL_PATH", tmp_path / "j.jsonl")
    monkeypatch.setattr(em, "JOURNAL_SEEN", tmp_path / "seen.json")
    monkeypatch.setattr(em, "LEGACY_OWNER_PATH", tmp_path / "l.txt")

    class P:
        async def get_session(self): return object()
        def reset_session(self): pass
    return em.PaperEngine(P())


def _inst(expiry):
    return em.Instrument(id="r1", symbol="DLF", instrument_type="FUTURE", timeframe="1D", quantity=825, lots=1, lot_size=825, token="7",
                         trading_symbol="DLFFUT", exch_seg="NFO", expiry=expiry, from_date="2026-08-03T00:00:00", added_at="2026-08-03T00:00:00", owner="u1")


def _snapshot(e, inst, monkeypatch, live):
    async def own(*a, **k): return _daily()
    async def none(*a, **k): return None
    async def intraday(*a, **k): return live
    monkeypatch.setattr(e, "_own_candles", own)
    monkeypatch.setattr(e, "_gap_bars", none)
    monkeypatch.setattr(e, "_chain", none)
    monkeypatch.setattr(e, "_live_intraday", intraday)
    return asyncio.run(e._build_snapshot(inst))


def test_the_daily_close_fallback_is_shown_but_not_for_a_real_account(tmp_path, monkeypatch):
    e = _engine(tmp_path, monkeypatch)
    snap = _snapshot(e, _inst("2027-01-28"), monkeypatch, live=None)          # 1-minute data unavailable: daily-close result
    assert snap["engine_warning"] and snap["real_ready"] is False


def test_a_proper_intraday_result_is_for_a_real_account(tmp_path, monkeypatch):
    e = _engine(tmp_path, monkeypatch)
    snap = _snapshot(e, _inst("2027-01-28"), monkeypatch, live=([], "Intraday rules live since 2026-10-20"))
    assert snap["real_ready"] is True


def test_an_expired_own_contracts_result_in_the_roll_window_is_not_for_a_real_account(tmp_path, monkeypatch):
    # the cutoff day before the next contract's first minute: the chain cannot run, and the single-contract path would show the EXPIRED contract
    e = _engine(tmp_path, monkeypatch)
    snap = _snapshot(e, _inst("2026-10-27"), monkeypatch, live=([], "Intraday rules live since 2026-10-20"))
    assert snap["real_ready"] is False


def test_a_result_that_is_not_real_ready_is_not_journaled(tmp_path, monkeypatch):
    e = _engine(tmp_path, monkeypatch)
    row = {"live": True, "entry_date": "2026-11-10 10:00", "direction": "BUY", "entry_price": 100.0, "status": "OPEN", "initial_sl": 95.0, "active_stop": 99.0,
           "breakeven": False, "gap_state": None, "contract": "DLFFUT"}
    e._journal(_inst("2027-01-28"), {"trades": [row], "real_ready": False})
    assert not (tmp_path / "j.jsonl").exists() and not (tmp_path / "seen.json").exists()       # nothing recorded, nothing marked as seen
    e._journal(_inst("2027-01-28"), {"trades": [row], "real_ready": True})
    assert (tmp_path / "seen.json").exists()


# ---------------------------------------------------------------- a fixed first day for the live 1-minute rules
class _Provider:
    def __init__(self, minutes=None, fail=False):
        self.minutes, self.fail, self.calls = minutes, fail, 0

    async def get_intraday_ohlc(self, *a, **k):
        self.calls += 1
        if self.fail:
            raise RuntimeError("Angel One refused (HTTP 403)")
        return self.minutes


class _I:
    id, symbol, exch_seg, token = "i9", "CANBK", "NFO", "9"
    trading_symbol, expiry, lot_size, added_at = "CANBK23NOV26FUT", "2026-11-23", 2700, "2026-11-01T10:00:00"
    sess = em.NSE


def _stored_minutes():
    days = pd.bdate_range("2026-10-26", "2026-11-10")
    parts = []
    for d in days:
        idx = pd.date_range(f"{d:%Y-%m-%d} 09:15", periods=375, freq="min")
        px = [100 + ((k // 40) % 5) * 0.4 for k in range(375)]
        parts.append(pd.DataFrame({"open": px, "high": [x + .1 for x in px], "low": [x - .1 for x in px], "close": px, "volume": 1.0}, index=idx))
    return pd.concat(parts)


def test_the_first_day_is_fixed_and_never_moves():
    assert em.PaperEngine._single_minutes_from(_I) == dt.date(2026, 10, 30)                    # every instrument of today: the day they use now
    later = type("L", (), {"added_at": "2026-11-25T09:00:00"})
    assert em.PaperEngine._single_minutes_from(later) == dt.date(2026, 11, 13)                 # a later one: its window on the day it was added
    assert em.PaperEngine._single_minutes_from(type("B", (), {"added_at": "?"})) == em.SINGLE_MINUTES_FROM


def test_the_live_history_does_not_change_overnight_and_goes_on_from_the_stored_candles(monkeypatch):
    # GOLDTEN, 10 Nov 2026: at midnight the window 'last 12 days' moved, its first day was rebuilt from the daily closes and the open BUY changed
    from app.live import candle_store
    from app.live.ticks import TickFeed
    candle_store.put("9", "ONE_MINUTE", _stored_minutes())
    e = em.PaperEngine.__new__(em.PaperEngine)
    e.provider, e._live_good, e._chain_cache, e._cooldown_until = _Provider(fail=True), {}, {}, {}           # Angel One refuses every candle request
    e.feed = TickFeed(lambda: None)
    e._refresh_later = lambda key, load: None
    conf = _daily(72)                                                                         # daily closes up to 10 Nov
    a = asyncio.run(e._live_intraday(_I, conf, [], dt.datetime(2026, 11, 10, 20, 0)))
    e._chain_cache.clear()                                                                    # a restart overnight: nothing cached
    b = asyncio.run(e._live_intraday(_I, conf, [], dt.datetime(2026, 11, 11, 10, 0)))
    assert a is not None and b is not None and not a[1].startswith("WARNING") and not b[1].startswith("WARNING")
    assert "live since 2026-10-30" in a[1] and "live since 2026-10-30" in b[1]
    key = lambda r: [(t.direction, t.entry_date, t.entry_price, t.exit_reason) for t in r[0]]
    assert key(a) == key(b)
