"""The engine's sweep: open positions first, no sleeping between instruments, a short pause that never turns into a busy loop. No network."""
import asyncio
import time
from collections import deque
from types import SimpleNamespace

from app.paper_trading import engine as em


def bare(ids, with_position=()):
    e = em.PaperEngine.__new__(em.PaperEngine)
    e._lock = asyncio.Lock()
    e.instruments = {i: SimpleNamespace(id=i) for i in ids}
    e.snapshots = {i: ({"position": {"direction": "BUY"}} if i in with_position else {"position": None}) for i in ids}
    e._sweeps, e.last_sweep_s, e.last_tick, e.faults = deque(maxlen=300), 0.0, None, {}
    e.visited = []

    async def fake_refresh(inst):
        e.visited.append(inst.id)
    e.refresh_one = fake_refresh
    return e


def test_a_sweep_visits_open_positions_first_and_does_not_sleep_between_instruments():
    e = bare(["a", "b", "c", "d", "e", "f"], with_position={"d", "f"})
    t0 = time.perf_counter()
    asyncio.run(e.refresh_all())
    took = time.perf_counter() - t0
    assert e.visited[:2] == ["d", "f"] and sorted(e.visited) == ["a", "b", "c", "d", "e", "f"]
    assert took < 0.3, took                                  # it used to pause 0.4 s after every instrument (2.4 s for six)
    assert len(e._sweeps) == 1 and e.last_sweep_s > 0


def test_the_pause_after_a_sweep_is_short_but_never_a_busy_loop():
    assert em.PaperEngine.sweep_sleep(0.0) == em.SWEEP_MIN_SLEEP == 0.25
    assert em.PaperEngine.sweep_sleep(0.05) == 0.25
    assert em.PaperEngine.sweep_sleep(0.5) == 1.0            # a slow sweep is followed by a longer pause (about a third of a core at most)
    assert em.PaperEngine.sweep_sleep(2.0) == 4.0


def test_sweep_stats_report_the_pass_time_and_the_cycle():
    e = bare([])
    assert e.sweep_stats() == {"sweeps": 0}
    for i, ms in enumerate([100.0, 200.0, 300.0]):
        e._sweeps.append((1000.0 + i * 2, ms))
    s = e.sweep_stats()
    assert s["sweeps"] == 3 and s["last_ms"] == 300.0 and s["avg_ms"] == 200.0 and s["cycle_s"] == 2.0


def test_a_failing_instrument_is_not_hammered_on_a_fast_loop():
    e = bare(["ok", "bad"])
    e.faults["bad"] = {"message": "403"}
    asyncio.run(e.refresh_all())
    asyncio.run(e.refresh_all())                              # immediately again: "bad" was tried in the first sweep and is still failing
    assert e.visited.count("ok") == 2 and e.visited.count("bad") == 1
    e._fault_tried["bad"] -= em.FAULT_RETRY_SECONDS + 1       # its pause is over
    asyncio.run(e.refresh_all())
    assert e.visited.count("bad") == 2


# ---------------------------------------------------------------- the instrument's own (expired) contract no longer stops the chain
import datetime as dt

import pandas as pd

from app.live import candle_store


def _eng(tmp_path, monkeypatch, provider_result):
    monkeypatch.setattr(candle_store, "DB", tmp_path / "c.db")
    e = em.PaperEngine.__new__(em.PaperEngine)
    e._raw_cache = {}
    e.feed = SimpleNamespace(healthy=lambda *_: False)
    calls = []

    async def fake_candles(*a, **k):
        calls.append(a)
        if isinstance(provider_result, Exception):
            raise provider_result
        return provider_result
    e._candles = fake_candles
    e.calls = calls
    return e


INST = SimpleNamespace(id="i1", symbol="BANKNIFTY", token="61466", exch_seg="NFO", trading_symbol="BANKNIFTY27OCT26FUT", sess=em.NSE)
NOW, FROM = dt.datetime(2026, 11, 1, 12, 0), dt.datetime(2026, 1, 1)
EMPTY = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def _bars(n=6):
    idx = pd.date_range("2026-10-15", periods=n, freq="B")
    return pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1.0}, index=idx)


def test_an_empty_answer_for_an_expired_contract_falls_back_to_the_stored_copy(tmp_path, monkeypatch):
    e = _eng(tmp_path, monkeypatch, EMPTY)
    candle_store.put("61466", "ONE_DAY", _bars())
    got = asyncio.run(e._own_candles(INST, "ONE_DAY", FROM, 300, NOW))
    assert len(got) == 6


def test_an_empty_answer_with_nothing_stored_is_empty_not_an_error(tmp_path, monkeypatch):
    e = _eng(tmp_path, monkeypatch, EMPTY)
    assert asyncio.run(e._own_candles(INST, "ONE_DAY", FROM, 300, NOW)).empty          # the chain of later contracts can still evaluate


def test_an_angel_one_error_with_nothing_stored_still_raises_and_with_a_stored_copy_does_not(tmp_path, monkeypatch):
    import pytest
    e = _eng(tmp_path, monkeypatch, RuntimeError("HTTP 403"))
    with pytest.raises(RuntimeError):
        asyncio.run(e._own_candles(INST, "ONE_DAY", FROM, 300, NOW))
    candle_store.put("61466", "ONE_DAY", _bars())
    assert len(asyncio.run(_eng(tmp_path, monkeypatch, RuntimeError("HTTP 403"))._own_candles(INST, "ONE_DAY", FROM, 300, NOW))) == 6


def test_good_candles_are_returned_filtered_and_cached(tmp_path, monkeypatch):
    e = _eng(tmp_path, monkeypatch, _bars(8))
    assert len(asyncio.run(e._own_candles(INST, "ONE_DAY", dt.datetime(2026, 10, 20), 300, NOW))) == 5      # only the bars since the start day
    asyncio.run(e._own_candles(INST, "ONE_DAY", FROM, 300, NOW + dt.timedelta(seconds=5)))
    assert len(e.calls) == 1                                                                                  # the second look came from the short cache


def test_a_snapshot_survives_a_restart_and_is_marked_restored(tmp_path, monkeypatch):
    monkeypatch.setattr(em, "SNAPSHOT_DIR", tmp_path / "s")
    e = em.PaperEngine.__new__(em.PaperEngine)
    e.snapshots = {"a": {"ltp": 5.0}}
    e._store_snapshot("a")
    e.snapshots["a"] = {"ltp": 6.0}
    e._store_snapshot("a")                                    # within the minute: not written again
    fresh = em.PaperEngine.__new__(em.PaperEngine)
    fresh.snapshots = {}
    fresh._restore_snapshot("a")
    fresh._restore_snapshot("missing")
    assert fresh.snapshots == {"a": {"ltp": 5.0, "restored": True}}


def test_a_slow_angel_one_does_not_hold_up_a_sweep_and_the_refresh_arrives_later(tmp_path, monkeypatch):
    from app.live.chain_eval import ContractRef
    monkeypatch.setattr(candle_store, "DB", tmp_path / "c.db")
    c = ContractRef("BANKNIFTY23NOV26FUT", "1", "NFO", dt.date(2026, 11, 23), 30)
    old, new = _bars(6), _bars(7)
    calls = []

    async def go():
        e = em.PaperEngine.__new__(em.PaperEngine)
        e._chain_cache = {("d", "1"): (NOW - dt.timedelta(hours=1), old)}
        release = asyncio.Event()

        async def slow(*a, **k):
            calls.append(a)
            await release.wait()
            return new
        e._candles = slow
        t0 = time.perf_counter()
        first = await asyncio.wait_for(e._contract_daily(c, FROM, NOW), 1)
        assert time.perf_counter() - t0 < 0.2 and len(first) == 6            # answered at once from what it had
        await asyncio.sleep(0)
        await e._contract_daily(c, FROM, NOW)                                 # still stale: no second call while one is running
        assert len(calls) == 1
        release.set()
        await asyncio.sleep(0.05)
        assert len(e._chain_cache[("d", "1")][1]) == 7                        # the newer candles arrived
    asyncio.run(go())


def test_a_live_index_price_never_waits_for_the_candle_queue():
    async def go():
        e = em.PaperEngine.__new__(em.PaperEngine)
        e._prev_close = {}
        e.feed = SimpleNamespace(latest={"99926009": SimpleNamespace(ltp=55000.0)}, healthy=lambda *_: True, age=lambda *_: 0.1)
        release = asyncio.Event()

        async def slow(*a, **k):
            await release.wait()
            return pd.DataFrame({"close": [54000.0, 55100.0]}, index=pd.to_datetime(["2026-01-01", "2099-01-01"]))
        e._candles = slow
        first = await asyncio.wait_for(e.index_quote("BANKNIFTY"), 1)
        assert first["price"] == 55000.0 and first["live"] and first["prev_close"] is None     # the price at once; the close follows
        release.set()
        await asyncio.sleep(0.05)
        second = await e.index_quote("BANKNIFTY")
        assert second["prev_close"] == 54000.0 and second["change"] == 1000.0
    asyncio.run(go())


def test_a_failed_15m_fetch_is_remembered_so_the_next_sweep_does_not_wait_again():
    calls = []

    async def go():
        e = em.PaperEngine.__new__(em.PaperEngine)
        e._bars15 = {}

        async def refused(*a, **k):
            calls.append(a)
            raise RuntimeError("HTTP 403")
        e._candles = refused
        e._new_15m_candle_closed = lambda *_: False
        assert await e._gap_bars(INST, FROM, NOW) is None
        assert await e._gap_bars(INST, FROM, NOW + dt.timedelta(seconds=1)) is None
        assert len(calls) == 1
    asyncio.run(go())
