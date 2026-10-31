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
