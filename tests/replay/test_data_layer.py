"""Index instrument selection, CSV/Angel validation, storage, resumable download."""
import asyncio
import datetime as dt

import pytest

from app.providers import angelone_scrip_master as scrip
from app.replay import angel_fetch
from app.replay.csv_import import validate_csv
from app.replay.store import InstrumentError, ReplayStore
from tests.replay.helpers import START, csv_text, daily_rows, day_ts, intraday_rows


# ------------------------ instrument selection ------------------------
def test_index_resolution_picks_the_index_not_futures(monkeypatch):
    rows = [
        {"token": "48699", "symbol": "BANKNIFTY27OCT26FUT", "name": "BANKNIFTY", "instrumenttype": "FUTIDX", "exch_seg": "NFO"},
        {"token": "99926009", "symbol": "Nifty Bank", "name": "BANKNIFTY", "instrumenttype": "AMXIDX", "exch_seg": "NSE"},
    ]

    async def fake():
        return rows

    monkeypatch.setattr(scrip, "_fetch_scrip_master", fake)
    m = asyncio.run(scrip.resolve_index("BANKNIFTY"))
    assert (m.token, m.exch_seg) == (angel_fetch.INDEX_TOKEN, "NSE")


def test_futures_data_cannot_enter_index_replay(tmp_path):
    res = validate_csv(csv_text(daily_rows([1, 2, 3]), instrument="BANKNIFTY27OCT26FUT"))
    assert not res.ok and "futures" in res.report["fatal"].lower() + str(res.report["rejected"])
    s = ReplayStore(tmp_path / "r.db")
    with pytest.raises(InstrumentError):
        s.add_candles("BANKNIFTY27OCT26FUT", "1D", daily_rows([1, 2]), "IMPORT", {})
    with pytest.raises(InstrumentError):
        s.add_candles("NIFTY", "1D", daily_rows([1, 2]), "IMPORT", {})
    assert s.coverage() == []


# ------------------------------ validation ------------------------------
def test_sorted_deduplicated_and_reported():
    rows = daily_rows([100, 101, 102, 103])
    res = validate_csv(csv_text([rows[2], rows[0], rows[1], rows[1], rows[3]]))
    assert res.ok and [r[0] for r in res.rows] == [day_ts(i) for i in range(4)]
    assert any("duplicate" in r["reason"] for r in res.report["rejected"]) and res.report["warnings"]


def test_missing_intraday_candles_reported_not_filled():
    rows = [r for i, r in enumerate(intraday_rows(0, 15)) if i not in (3, 4, 20)]
    res = validate_csv(csv_text(rows, "15m"))
    assert len(res.rows) == 22 and res.report["missing_candles_total"] == 3
    assert res.report["incomplete_days_detail"][START.isoformat()] == ["10:00", "10:15", "14:15"]


def test_1m_missing_minutes_and_daily_gap_flag():
    rows = [r for i, r in enumerate(intraday_rows(0, 1)) if not 10 <= i <= 19]
    assert validate_csv(csv_text(rows, "1m")).report["missing_candles_total"] == 10
    gap = daily_rows([1, 2, 3])
    gap[2] = (day_ts(30), *gap[2][1:])
    assert validate_csv(csv_text(gap)).report["incomplete_days"] == 1


def test_off_grid_ohlc_and_columns_rejected():
    rows = intraday_rows(0, 15)[:3] + [(day_ts(0).replace(hour=9, minute=20), 1, 1, 1, 1, 0)]
    res = validate_csv(csv_text(rows, "15m"))
    assert res.ok and "grid" in res.report["rejected"][0]["reason"]
    lines = csv_text(daily_rows([10, 11])).splitlines()
    parts = lines[1].split(",")
    parts[4] = "1"  # high below open/close
    res = validate_csv("\n".join([lines[0], ",".join(parts), lines[2]]))
    assert res.ok and "OHLC" in res.report["rejected"][0]["reason"] and res.report["rejected"][0]["line"] == 2
    assert "Missing required columns" in validate_csv("timestamp,open\n2026-09-01,1").report["fatal"]


def test_timezone_offset_converted_to_ist():
    text = csv_text(intraday_rows(0, 15)[:1], "15m").replace("2026-09-01T09:15:00", "2026-09-01T03:45:00+00:00")
    assert validate_csv(text).rows[0][0] == dt.datetime(2026, 9, 1, 9, 15)


def test_mixed_interval_rows_rejected():
    a = csv_text(daily_rows([1, 2]))
    text = a + "\n" + csv_text(intraday_rows(0, 15)[:1], "15m").splitlines()[1]
    res = validate_csv(text)
    assert res.ok and "differs" in res.report["rejected"][0]["reason"]


# ------------------------------- storage -------------------------------
def test_store_roundtrip_coverage_and_sessions(tmp_path):
    path = tmp_path / "r.db"
    s = ReplayStore(path)
    s.add_candles("BANKNIFTY", "1D", daily_rows([10, 11, 12]), "IMPORT", {})
    s.add_candles("BANKNIFTY", "15m", intraday_rows(0, 15)[:-2], "ANGELONE", {})
    cov = {c["interval"]: c for c in s.coverage()}
    assert set(cov) == {"1D", "15m"} and cov["1D"]["candles"] == 3 and cov["1D"]["instrument_type"] == "INDEX"
    assert cov["15m"]["sources"] == ["ANGELONE"] and cov["15m"]["incomplete_days"] == 1
    assert s.intervals_available("BANKNIFTY") == ["15m", "1D"]
    s.add_candles("BANKNIFTY", "1D", daily_rows([10, 11, 12]), "ANGELONE", {})  # merge, idempotent
    assert s.count("BANKNIFTY", "1D") == 3
    assert [c for c in s.coverage() if c["interval"] == "1D"][0]["sources"] == ["ANGELONE", "IMPORT"]
    s.save_session("abc", {"x": 1}, 7)
    assert ReplayStore(path).load_session("abc") == {"config": {"x": 1}, "position": 7}


# ------------------------- Angel One download -------------------------
def test_chunk_plans_respect_per_request_caps():
    plan = angel_fetch.chunks("1m", dt.date(2026, 1, 1), dt.date(2026, 3, 31))
    assert all((b.date() - a.date()).days + 1 <= 25 for a, b in plan) and plan[0][0].time() == dt.time(9, 15)
    daily = angel_fetch.chunks("1D", dt.date(2016, 1, 1), dt.date(2026, 1, 1))
    assert len(daily) == 3 and daily[0][0].time() == dt.time(0, 0)
    with pytest.raises(angel_fetch.FetchRefused):
        angel_fetch.chunks("2m", START, START)


def _angel_rows(a, b):
    out, d = [], a.date()
    while d <= b.date():
        for r in intraday_rows(0, 15):
            ts = dt.datetime.combine(d, r[0].time())
            out.append([ts.isoformat() + "+05:30", 100.0, 101.0, 99.0, 100.5, 0])
        d += dt.timedelta(days=1)
    return out


def test_download_resumes_after_interruption_and_reports(tmp_path):
    calls = []

    async def flaky(iv, a, b):
        calls.append(a.date())
        if len(calls) == 3:
            raise RuntimeError("network down")
        return _angel_rows(a, a)

    start, end = dt.date(2026, 1, 1), dt.date(2026, 6, 30)  # 15m: 180-day cap -> 2 chunks; use 5m (90d) -> 3
    kw = dict(cache_dir=tmp_path, delay=0, today=dt.date(2026, 10, 10))
    with pytest.raises(RuntimeError):
        asyncio.run(angel_fetch.fetch_index("5m", start, end, flaky, **kw))
    first = list(calls)
    assert len(first) == 3
    calls.clear()

    async def ok(iv, a, b):
        calls.append(a.date())
        return _angel_rows(a, a)

    # 5m grid vs 15m rows: off-grid rows are reported, not silently accepted
    res = asyncio.run(angel_fetch.fetch_index("5m", start, end, ok, **kw))
    assert calls == [first[2]]  # only the failed chunk is refetched
    assert res.report["source"] == "ANGELONE" and res.ok
