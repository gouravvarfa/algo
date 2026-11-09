"""Angel One's candle quota during market hours (found live on 9 Nov 2026: about half of all candle requests refused with a 403 page between
9:00 and 15:30, ~650 logins a day). Covers: no fresh login for a rate-limit refusal (one for a dead session), candle refusals never block the
account calls real orders need, an expired contract held to its last day is never asked for again, and a sweep that only WAITED is not followed
by a long pause. No network."""
import asyncio
import datetime as dt
import time
from types import SimpleNamespace

import pandas as pd
import pytest

from app.live import candle_store
from app.live.chain_eval import ContractRef
from app.ops import circuit, health
from app.paper_trading import engine as em
from app.providers import angelone_client as ac
from app.providers import angelone_provider as ap
from app.providers.angelone_client import AngelOneApiError

RATE_LIMIT = AngelOneApiError("Angel One returned a non-JSON response (HTTP 403).", "data", retryable=True)
NETWORK = AngelOneApiError("Could not reach Angel One: timeout", "network", retryable=True)
DEAD = AngelOneApiError("Invalid Token", "data", retryable=False)


# ---------------------------------------------------------------- a fresh login only when it can help
def test_only_a_dead_session_answer_is_worth_a_fresh_login():
    assert not ap.relogin_can_help(RATE_LIMIT)
    assert not ap.relogin_can_help(NETWORK)
    assert ap.relogin_can_help(DEAD)
    assert ap.relogin_can_help(AngelOneApiError("AG8001", "data"))
    assert ap.relogin_can_help(AngelOneApiError("Token expired", "data"))
    assert ap.relogin_can_help(AngelOneApiError("Invalid totp", "auth"))
    assert not ap.relogin_can_help(AngelOneApiError("No data found for the given parameters", "data"))


def _provider(monkeypatch, answers):
    logins = []

    async def fake_login(*a):
        logins.append(1)
        return SimpleNamespace(jwt_token=f"t{len(logins)}")
    seq = list(answers)

    async def fake_candles(session, **kw):
        x = seq.pop(0)
        if isinstance(x, Exception):
            raise x
        return x
    monkeypatch.setattr(ap, "login", fake_login)
    monkeypatch.setattr(ap, "get_candle_data", fake_candles)
    monkeypatch.setattr(ap.angelone_credential_store, "get_credentials", lambda: SimpleNamespace(api_key="k", client_code="c", pin="p", totp_secret="s"))
    return ap.AngelOneProvider(), logins


REQ = dict(exchange="NFO", symbol_token="1", interval="ONE_DAY", from_date="2026-11-01 09:15", to_date="2026-11-09 15:30")


def test_a_rate_limit_refusal_does_not_log_in_again(monkeypatch):
    p, logins = _provider(monkeypatch, [RATE_LIMIT, RATE_LIMIT, NETWORK])
    for _ in range(3):
        with pytest.raises(AngelOneApiError):
            asyncio.run(p._candle_rows(**REQ))
    assert len(logins) == 1 and p.relogins == 0              # the first login only (it used to log in again after every refusal)


def test_a_dead_session_gets_exactly_one_fresh_login_and_retry(monkeypatch):
    p, logins = _provider(monkeypatch, [DEAD, [["2026-11-09T00:00:00+05:30", 1, 2, 0.5, 1.5, 9]]])
    assert asyncio.run(p._candle_rows(**REQ)) == [["2026-11-09T00:00:00+05:30", 1, 2, 0.5, 1.5, 9]]
    assert len(logins) == 2 and p.relogins == 1


def test_a_long_stall_gets_one_fresh_login_at_most_every_ten_minutes(monkeypatch):
    p, logins = _provider(monkeypatch, [RATE_LIMIT, RATE_LIMIT, RATE_LIMIT])
    p._last_ok = time.monotonic() - ap.RELOGIN_STALL_S - 1     # nothing has worked for over five minutes
    with pytest.raises(AngelOneApiError):
        asyncio.run(p._candle_rows(**REQ))                       # refused, logged in again, refused again
    assert len(logins) == 2 and p.relogins == 1
    with pytest.raises(AngelOneApiError):
        asyncio.run(p._candle_rows(**REQ))                       # still stalled, but a fresh login was just tried
    assert len(logins) == 2


def test_the_stall_safety_net_works_on_a_machine_that_just_booted(monkeypatch):
    # time.monotonic() counts from boot: CI runners and a just-rebooted server are only minutes old. "No fresh login yet" must not read as
    # "a fresh login at second 0", or the safety net stays shut for the first ten minutes (this failed on CI, never on a laptop up for days).
    p, logins = _provider(monkeypatch, [RATE_LIMIT, RATE_LIMIT])
    monkeypatch.setattr(ap, "_clock", lambda: 200.0)
    p._last_ok = 200.0 - ap.RELOGIN_STALL_S - 1
    with pytest.raises(AngelOneApiError):
        asyncio.run(p._candle_rows(**REQ))
    assert len(logins) == 2 and p.relogins == 1


def test_intraday_candles_still_come_back_as_a_frame(monkeypatch):
    p, _ = _provider(monkeypatch, [[["2026-11-09T09:15:00+05:30", 100, 101, 99, 100.5, 7]]])
    df = asyncio.run(p.get_intraday_ohlc("NFO", "1", "ONE_MINUTE", 1, include_partial=True))
    assert len(df) == 1 and float(df["close"].iloc[0]) == 100.5


# ---------------------------------------------------------------- candle refusals never block the account calls
def test_an_open_candle_circuit_does_not_block_the_account_calls(monkeypatch):
    assert ac._candle_circuit is not ac._rest and ac._candle_circuit.name == "angelone.candles"
    s = SimpleNamespace(api_key="k", jwt_token="t", client_code="c")
    ac._rest.force(None)
    ac._candle_circuit.force("open")
    try:
        with pytest.raises(circuit.CircuitOpen):
            asyncio.run(ac.get_candle_data(s, "NFO", "1", "ONE_DAY", "2026-11-01 09:15", "2026-11-09 15:30"))

        async def answered(fn, *a, **k):
            return {"net": "5"}
        monkeypatch.setattr(ac, "_retry_async", answered)
        assert asyncio.run(ac.get_funds(s)) == {"net": "5"}       # the order book / funds / positions path is still open
    finally:
        ac._candle_circuit.force(None)
        ac._rest.force(None)


@pytest.fixture
def fresh_circuits():
    circuit.reset()
    yield
    circuit.reset()


def test_the_health_tile_grades_the_two_circuits(fresh_circuits):
    cb = circuit.breaker("angelone.candles", threshold=1)
    with pytest.raises(OSError):
        cb.call(lambda: (_ for _ in ()).throw(OSError("403")))
    t = health.angel_rest_tile()
    assert t["status"] == health.DEGRADED and "candles circuit OPEN" in t["reason"]        # strategies run on stored candles + ticks
    rb = circuit.breaker("angelone.rest", threshold=1)
    with pytest.raises(OSError):
        rb.call(lambda: (_ for _ in ()).throw(OSError("x")))
    assert health.angel_rest_tile()["status"] == health.DOWN                              # account calls blocked: real orders cannot work


# ---------------------------------------------------------------- an expired contract held to its last day is final
NOW, FROM = dt.datetime(2026, 11, 9, 10, 0), dt.datetime(2026, 1, 1)
OCT = SimpleNamespace(id="i1", symbol="BANKNIFTY", token="61466", exch_seg="NFO", trading_symbol="BANKNIFTY27OCT26FUT", expiry="2026-10-27", sess=em.NSE)
NOV = SimpleNamespace(id="i2", symbol="BANKNIFTY", token="70001", exch_seg="NFO", trading_symbol="BANKNIFTY23NOV26FUT", expiry="2026-11-23", sess=em.NSE)


def _daily(last="2026-10-27", n=6):
    idx = pd.date_range(end=last, periods=n, freq="B")
    return pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1.0}, index=idx)


def _minutes(day="2026-10-27", n=30):
    idx = pd.date_range(f"{day} 09:15", periods=n, freq="min")
    return pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1.0}, index=idx)


def _eng(tmp_path, monkeypatch, answer):
    monkeypatch.setattr(candle_store, "DB", tmp_path / "c.db")
    e = em.PaperEngine.__new__(em.PaperEngine)
    e._raw_cache, e._chain_cache, e._bars15, e._cooldown_until = {}, {}, {}, {}
    e.feed = SimpleNamespace(healthy=lambda *_: False, builder=SimpleNamespace(minutes=lambda *_: []))
    e.calls, e.refreshes = [], []

    async def fake_candles(*a, **k):
        e.calls.append(a)
        return answer
    e._candles = fake_candles
    e._refresh_later = lambda key, load: e.refreshes.append(key)
    return e


def test_settled_means_expired_and_held_to_the_expiry_day():
    assert em._settled("2026-10-27", pd.Timestamp("2026-10-27"), NOW)
    assert em._settled(dt.date(2026, 10, 27), dt.datetime(2026, 10, 27, 15, 29), NOW)
    assert not em._settled("2026-10-27", pd.Timestamp("2026-10-23"), NOW)                 # the store stops before the last day: keep asking
    assert not em._settled("2026-10-27", None, NOW)
    assert not em._settled("2026-11-09", pd.Timestamp("2026-11-09"), NOW)                 # the expiry day itself is still trading
    assert not em._settled("", pd.Timestamp("2026-11-06"), NOW)                           # an equity has no expiry
    assert not em._settled("garbage", pd.Timestamp("2026-11-06"), NOW)


def test_the_instruments_own_expired_contract_is_read_from_the_store_and_never_refreshed(tmp_path, monkeypatch):
    e = _eng(tmp_path, monkeypatch, pd.DataFrame(columns=["open", "high", "low", "close", "volume"]))
    candle_store.put(OCT.token, "ONE_DAY", _daily())
    assert len(asyncio.run(e._own_candles(OCT, "ONE_DAY", FROM, 320, NOW))) == 6
    asyncio.run(e._own_candles(OCT, "ONE_DAY", FROM, 320, NOW + dt.timedelta(hours=1)))  # long stale
    assert e.calls == [] and e.refreshes == []                                            # it was asked every 15 s before


def test_an_expired_contract_missing_its_last_days_is_still_asked_for(tmp_path, monkeypatch):
    e = _eng(tmp_path, monkeypatch, pd.DataFrame(columns=["open", "high", "low", "close", "volume"]))
    candle_store.put(OCT.token, "ONE_DAY", _daily(last="2026-10-23"))
    asyncio.run(e._own_candles(OCT, "ONE_DAY", FROM, 320, NOW))
    assert len(e.calls) == 1


def test_a_live_contract_is_refreshed_as_before(tmp_path, monkeypatch):
    e = _eng(tmp_path, monkeypatch, _daily(last="2026-11-06"))
    asyncio.run(e._own_candles(NOV, "ONE_DAY", FROM, 320, NOW))
    asyncio.run(e._own_candles(NOV, "ONE_DAY", FROM, 320, NOW + dt.timedelta(minutes=5)))
    assert len(e.calls) == 1 and e.refreshes == [("o", "i2")]


def test_chain_contracts_daily_and_minutes_of_an_expired_contract_are_final(tmp_path, monkeypatch):
    e = _eng(tmp_path, monkeypatch, pd.DataFrame(columns=["open", "high", "low", "close", "volume"]))
    c = ContractRef("BANKNIFTY27OCT26FUT", OCT.token, "NFO", dt.date(2026, 10, 27), 30)
    candle_store.put(c.token, "ONE_DAY", _daily())
    candle_store.put(c.token, "ONE_MINUTE", _minutes())
    assert len(asyncio.run(e._contract_daily(c, FROM, NOW))) == 6
    assert len(asyncio.run(e._contract_minutes(c, dt.date(2026, 10, 20), NOW))) == 30
    later = NOW + dt.timedelta(hours=1)
    asyncio.run(e._contract_daily(c, FROM, later))
    asyncio.run(e._contract_minutes(c, dt.date(2026, 10, 20), later))
    assert e.calls == [] and e.refreshes == []


def test_chain_contract_still_trading_is_refreshed_as_before(tmp_path, monkeypatch):
    e = _eng(tmp_path, monkeypatch, _daily(last="2026-11-06"))
    c = ContractRef("BANKNIFTY23NOV26FUT", NOV.token, "NFO", dt.date(2026, 11, 23), 30)
    asyncio.run(e._contract_daily(c, FROM, NOW))
    asyncio.run(e._contract_daily(c, FROM, NOW + dt.timedelta(minutes=5)))
    assert len(e.calls) == 1 and e.refreshes == [("d", NOV.token)]


def test_the_15m_bars_of_an_expired_contract_are_not_refreshed(tmp_path, monkeypatch):
    e = _eng(tmp_path, monkeypatch, None)
    e._new_15m_candle_closed = lambda *_: True
    e._bars15 = {"i1": (NOW - dt.timedelta(hours=2), {"kept": 1}), "i2": (NOW - dt.timedelta(hours=2), {"kept": 2})}
    assert asyncio.run(e._gap_bars(OCT, FROM, NOW)) == {"kept": 1}
    assert asyncio.run(e._gap_bars(NOV, FROM, NOW)) == {"kept": 2}
    assert e.refreshes == [("g", "i2")]                                                   # only the contract that still trades


# ---------------------------------------------------------------- waiting is not CPU
def test_a_sweep_that_only_waited_is_not_followed_by_a_long_pause():
    from collections import deque
    e = em.PaperEngine.__new__(em.PaperEngine)
    e._lock = asyncio.Lock()
    e.instruments = {"a": SimpleNamespace(id="a")}
    e.snapshots = {"a": {"position": None}}
    e._sweeps, e.last_sweep_s, e.last_tick, e.faults = deque(maxlen=300), 0.0, None, {}

    async def waits(inst):
        await asyncio.sleep(0.3)                         # e.g. Angel One refusing, then the retries' back-off
    e.refresh_one = waits
    asyncio.run(e.refresh_all())
    assert e.last_sweep_s >= 0.25 and e.last_sweep_cpu_s >= 0.0   # both are recorded (exact CPU numbers depend on the machine)
    e.last_sweep_s, e.last_sweep_cpu_s = 30.0, 0.05       # waited 30 s on Angel One, used 0.05 s of CPU
    assert e.pause_after_sweep() == em.SWEEP_MIN_SLEEP   # the wall-time rule slept 2 x 30 s = 60 s before looking at stops again
    e.last_sweep_cpu_s = 0.5
    assert e.pause_after_sweep() == 1.0                   # real CPU work still earns a proportional pause
