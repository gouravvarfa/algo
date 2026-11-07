"""A slow read is waited for once; afterwards the answer is returned at once, and a stale one is refreshed in the background."""
import asyncio
import time

from app.options import api


def test_a_stale_answer_is_returned_at_once_and_refreshed_in_the_background(monkeypatch):
    monkeypatch.setattr(api, "_CACHE", {})
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.2)
        return len(calls)

    async def run():
        assert await api._cached("k", slow) == 1                      # the first caller waits
        t = time.time()
        assert await api._cached("k", slow) == 1 and time.time() - t < 0.05
        monkeypatch.setattr(api, "CACHE_TTL", 0.0)                     # now the answer is stale
        t = time.time()
        assert await api._cached("k", slow) == 1 and time.time() - t < 0.05    # still instant, old answer
        await asyncio.sleep(0.5)                                       # the refresh ran in the background
        assert await api._cached("k", slow) in (2, 3) and len(calls) >= 2

    asyncio.run(run())
