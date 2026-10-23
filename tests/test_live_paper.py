"""Hosted-deployment pieces: login, 15-minute gap-candle timing, live journal. Snapshots here are SYNTHETIC fixtures."""
import datetime as dt
import json

import pytest

from app import auth
from app.paper_trading import engine as pe

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def no_candle_sharing(monkeypatch):
    """These tests move a simulated clock; the cross-user candle sharing (15 s of REAL time) would hide the repeated Angel One calls they check."""
    monkeypatch.setattr(pe, "CANDLE_SHARE_SECONDS", 0)


# ---------------------------------------------------------------- login
@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("APP_USERNAME", "gaurav")
    monkeypatch.setenv("APP_PASSWORD_HASH", auth.hash_password("correct horse battery", iterations=1000))
    monkeypatch.setenv("APP_SECRET", "x" * 64)


def test_password_and_session(creds):
    assert auth.enabled() and auth.login("gaurav", "correct horse battery")
    assert not auth.login("gaurav", "wrong") and not auth.login("other", "correct horse battery")
    tok = auth.make_session("gaurav", now=1000)
    assert auth.valid_session(tok, now=1001) and not auth.valid_session(tok, now=1000 + auth.SESSION_SECONDS + 1)
    assert not auth.valid_session(tok.replace("gaurav", "admin")) and not auth.valid_session("junk")


def test_login_disabled_without_env(monkeypatch):
    for k in ("APP_USERNAME", "APP_PASSWORD_HASH", "APP_SECRET"):
        monkeypatch.delenv(k, raising=False)
    assert not auth.enabled()


def test_throttle_after_five_failures():
    ip = "9.9.9.9"
    auth._fails.pop(ip, None)
    for _ in range(5):
        auth.record_failure(ip)
    assert auth.throttled(ip)


def test_middleware_protects_pages_and_apis(creds):
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)                                   # no context manager: the paper engine is not started
    assert c.get("/simulator", follow_redirects=False).status_code == 303
    assert c.get("/api/options/backtests").status_code == 401
    assert c.get("/login", follow_redirects=False).status_code == 307
    r = c.post("/login", content="username=gaurav&password=correct+horse+battery", headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    assert r.status_code == 303 and auth.COOKIE in r.cookies
    assert c.get("/api/options/backtests").status_code == 200


# ---------------------------------------------------------------- 15-minute candle timing
def test_new_15m_candle_detection():
    t = lambda h, m, s=0: dt.datetime(2026, 10, 19, h, m, s, tzinfo=IST)
    f = pe.PaperEngine._new_15m_candle_closed
    assert f(t(9, 25), t(9, 30, 25))           # 09:15-09:30 candle closed after the last fetch
    assert not f(t(9, 25), t(9, 30, 10))       # not yet published (+20 s)
    assert not f(t(9, 30, 30), t(9, 40))       # already fetched after this boundary
    assert not f(t(15, 40), t(15, 45, 30))     # market closed


# ---------------------------------------------------------------- journal
class _Inst:
    id, symbol, trading_symbol, quantity = "abc", "DIXON", "DIXON27OCT26FUT", 50


def _row(status="OPEN", be=False, gap=None, live=True, exit_price=None):
    return {"live": live, "entry_date": "2026-10-16T00:00:00+05:30", "direction": "SELL", "entry_price": 12000.0, "status": status,
            "initial_sl": 12500.0, "active_stop": 12500.0, "breakeven": be, "gap_state": gap, "exit_date": None if status == "OPEN" else "2026-10-19T09:30:00+05:30",
            "exit_price": exit_price, "exit_reason": None if status == "OPEN" else "GAP_EXIT", "pnl_amount": 0 if status == "OPEN" else 1000.0, "protective_sl": None}


def test_journal_records_each_event_once_and_marks_backfill(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "JOURNAL_PATH", tmp_path / "j.jsonl")
    monkeypatch.setattr(pe, "JOURNAL_SEEN", tmp_path / "seen.json")
    eng = pe.PaperEngine.__new__(pe.PaperEngine)
    eng._journal(_Inst, {"trades": [_row(live=False), _row()]})              # first pass: existing open trade = backfill
    eng._journal(_Inst, {"trades": [_row()]})                                 # nothing new -> nothing written
    eng._journal(_Inst, {"trades": [_row(be=True)]})
    eng._journal(_Inst, {"trades": [_row(status="WIN", be=True, gap="GAP_EXITED", exit_price=11980.0)]})
    ev = [json.loads(x) for x in (tmp_path / "j.jsonl").read_text().splitlines()]
    assert [e["event"] for e in ev] == ["ENTRY", "SL_TO_BREAKEVEN", "EXIT", "GAP"]
    assert ev[0]["backfill"] and not ev[1]["backfill"] and ev[2]["exit_price"] == 11980.0 and ev[2]["reason"] == "GAP_EXIT"
    assert [e["event"] for e in eng.journal()] == ["GAP", "EXIT", "SL_TO_BREAKEVEN", "ENTRY"]          # newest first


# ---------------------------------------------------------------- live intraday rules (stop touch -> exit + reverse)
import pandas as pd


class _Provider:
    def __init__(self, minutes=None, boom=False):
        self.minutes, self.boom, self.calls = minutes, boom, 0

    async def get_intraday_ohlc(self, *a, **k):
        self.calls += 1
        if self.boom:
            raise RuntimeError("angel down")
        return self.minutes


def _daily(n=40, start=100.0):
    idx = pd.date_range("2026-08-03", periods=n, freq="B", tz=IST)
    base = [start + 6 * (1 if (i // 6) % 2 == 0 else -1) * ((i % 6) + 1) / 3 for i in range(n)]
    return pd.DataFrame({"open": base, "high": [b + 2 for b in base], "low": [b - 2 for b in base], "close": base}, index=idx)


def _minutes(day, prices):
    idx = pd.date_range(f"{day} 09:15", periods=len(prices), freq="min", tz=IST)
    return pd.DataFrame({"open": prices, "high": [p + .05 for p in prices], "low": [p - .05 for p in prices], "close": prices}, index=idx)


def _engine(provider):
    import asyncio
    e = pe.PaperEngine.__new__(pe.PaperEngine)
    from app.live.ticks import TickFeed
    e.provider, e._minutes, e._live_good, e._cooldown_until = provider, {}, {}, {}
    e.feed = TickFeed(lambda: None)
    return e


class _I:
    id, symbol, exch_seg, token = "i1", "BANKNIFTY", "NFO", "1"


def test_live_intraday_failure_never_breaks_the_instrument(caplog):
    import asyncio
    conf = _daily()
    out = asyncio.run(_engine(_Provider(boom=True))._live_intraday(_I, conf, [], dt.datetime(2026, 9, 30, 11, 0, tzinfo=IST)))
    assert out is None                                             # nothing to reuse: caller keeps the daily result AND shows a warning


def test_rate_limit_reuses_last_good_result_then_cools_down_and_expires():
    import asyncio
    conf = _daily(60)
    day = (conf.index[-1] + pd.offsets.BDay(1)).strftime("%Y-%m-%d")
    good = _Provider(_minutes(day, [100 + (i % 7) * .1 for i in range(120)]))
    eng = _engine(good)
    t0 = dt.datetime.fromisoformat(day + "T11:30:00").replace(tzinfo=IST)
    first = asyncio.run(eng._live_intraday(_I, conf, [], t0))
    assert first is not None and not first[1].startswith("WARNING")
    class Limited(_Provider):
        async def get_intraday_ohlc(self, *a, **k):
            self.calls += 1
            raise RuntimeError("Too many requests")
    lim = Limited(); eng.provider = lim
    t1 = t0 + dt.timedelta(minutes=2)
    r = asyncio.run(eng._live_intraday(_I, conf, [], t1))
    assert r is not None and r[1].startswith("WARNING") and "last successful intraday evaluation" in r[1] and r[0] == first[0]
    calls = lim.calls
    r2 = asyncio.run(eng._live_intraday(_I, conf, [], t1 + dt.timedelta(seconds=30)))      # inside the cooldown: no new Angel call
    assert lim.calls == calls and r2[1].startswith("WARNING")
    r3 = asyncio.run(eng._live_intraday(_I, conf, [], t0 + dt.timedelta(minutes=30)))     # too old to show as current
    assert r3 is None


def test_live_intraday_too_few_minutes_keeps_daily_result():
    import asyncio
    conf = _daily()
    out = asyncio.run(_engine(_Provider(_minutes("2026-09-28", [100.0] * 10)))._live_intraday(_I, conf, [], dt.datetime(2026, 9, 28, 9, 40, tzinfo=IST)))
    assert out is None


def test_live_intraday_returns_trades_and_a_note_and_caches_minutes():
    import asyncio
    conf = _daily(60)
    day = (conf.index[-1] + pd.offsets.BDay(1)).strftime("%Y-%m-%d")
    prov = _Provider(_minutes(day, [100 + (i % 7) * .1 for i in range(120)]))
    eng = _engine(prov)
    now = dt.datetime.fromisoformat(day + "T11:30:00").replace(tzinfo=IST)
    out = asyncio.run(eng._live_intraday(_I, conf, [], now))
    assert out is not None and isinstance(out[0], list) and "Intraday rules live since" in out[1]
    asyncio.run(eng._live_intraday(_I, conf, [], now))
    assert prov.calls == 1                                         # second call inside the refresh window reuses the minutes


# ---------------------------------------------------------------- partial first day of the minute window
def test_trim_partial_first_day_starts_at_a_complete_session():
    from app.strategy.top_bottom.intraday import Minute
    mk = lambda d, h, m: Minute(dt.datetime(2026, 10, d, h, m, tzinfo=IST), 1, 1, 1, 1)
    partial = [mk(9, 13, 10), mk(9, 13, 11), mk(12, 9, 15), mk(12, 9, 16), mk(12, 15, 29)]
    assert [(m.ts.day, m.ts.hour) for m in pe.trim_partial_first_day(partial)] == [(12, 9), (12, 9), (12, 15)]   # the 9 Oct afternoon is dropped
    full = [mk(9, 9, 15), mk(9, 9, 16), mk(12, 9, 15)]
    assert pe.trim_partial_first_day(full) == full                                                               # a complete first day is kept
    assert pe.trim_partial_first_day([mk(9, 13, 10), mk(9, 13, 11)]) == []                                       # nothing complete: no guess
    assert pe.trim_partial_first_day([]) == []
