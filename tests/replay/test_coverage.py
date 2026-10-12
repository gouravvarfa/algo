"""1-minute completeness per Daily session, continuous-block selection, end-of-data information.
SYNTHETIC fixtures. day_ts(0) is Tue 2026-09-01, so day 4/5 and 11/12 are weekend dates."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.replay import api as replay_api
from app.replay.plan import PlanError, plan_session, split_blocks
from app.replay.quality import session_quality
from app.replay.store import ReplayStore
from tests.replay.helpers import day_ts
from tests.replay.test_intraday import minute_day

D = lambda i: day_ts(i).date().isoformat()


def drows(days, price=100.0):
    return [(day_ts(i), price, price + 1, price - 1, price, 0.0) for i in days]


def mrows(*days, drop=(), price=100.0):
    out = []
    for i in days:
        out += [(m.ts, m.open, m.high, m.low, m.close, 0.0) for m in minute_day(i, price, drop=drop if i == days[-1] else ())]
    return out


@pytest.fixture
def store(tmp_path):
    return ReplayStore(tmp_path / "cov.db")


def fill(store, daily_days, minute_rows):
    store.add_candles("BANKNIFTY", "1D", drows(daily_days), "IMPORT", {})
    if minute_rows:
        store.add_candles("BANKNIFTY", "1m", minute_rows, "IMPORT", {})


# ------------------------------------------------------------------ completeness
def test_complete_incomplete_and_boundary_problems_are_reported_with_exact_counts(store):
    fill(store, [0, 1, 2, 3, 6, 7, 8, 9], mrows(6) + mrows(7, drop=range(10, 20)) + mrows(8, drop=range(0, 5)))
    q = session_quality(store, D(6), D(9))
    by = {s["date"]: s for s in q["sessions"]}
    assert (by[D(6)]["status"], by[D(6)]["actual"], by[D(6)]["missing"], by[D(6)]["coverage_pct"]) == ("Complete", 375, 0, 100.0)
    assert (by[D(7)]["status"], by[D(7)]["actual"], by[D(7)]["missing"]) == ("Incomplete", 365, 10)
    assert by[D(7)]["missing_ranges"] == ["09:25-09:34"] and by[D(7)]["coverage_pct"] == 97.33
    assert by[D(8)]["status"] == "Incomplete" and "starts at 09:20" in by[D(8)]["note"]
    assert by[D(9)]["status"] == "Incomplete" and by[D(9)]["actual"] == 0 and "no 1-minute candles" in by[D(9)]["note"]
    assert q["summary"] == {"sessions": 4, "complete": 1, "incomplete": 3, "not_verified": 0, "expected_minutes": 1500,
                            "actual_minutes": 375 + 365 + 370, "coverage_pct": 74.0, "weekdays_without_any_data": 0}
    assert "may have missed" in q["warning"]


def test_not_verified_weekend_and_no_daily_candle_and_weekdays_without_data_are_not_called_missing(store):
    # day 11 = Saturday with Daily + 1m (special session); day 13 = Monday with 1m but NO Daily candle; day 14 = Tuesday with nothing
    fill(store, [0, 1, 2, 3, 10, 11, 15], mrows(10) + mrows(11) + mrows(13) + mrows(15))
    q = session_quality(store, D(10), D(15))
    by = {s["date"]: s for s in q["sessions"]}
    assert by[D(10)]["status"] == "Complete"
    assert by[D(11)]["status"] == "Not Verified" and "weekend" in by[D(11)]["note"]
    assert by[D(13)]["status"] == "Not Verified" and "without a stored Daily candle" in by[D(13)]["note"]
    assert D(14) in q["weekdays_without_any_data"] and D(14) not in by      # neither a session nor "missing": unknown
    assert "No exchange holiday calendar" in q["calendar_note"]
    assert q["summary"]["not_verified"] == 2 and q["summary"]["complete"] == 2


def test_invalid_ohlc_in_the_store_makes_the_session_incomplete(store):
    rows = mrows(6)
    rows[50] = (rows[50][0], 100.0, 90.0, 110.0, 100.0, 0.0)   # high below low
    fill(store, [0, 1, 2, 3, 6], rows)
    s = session_quality(store, D(6), D(6))["sessions"][0]
    assert s["status"] == "Incomplete" and s["invalid_ohlc"] == 1 and "inconsistent OHLC" in s["note"] and s["actual"] == 375


# ------------------------------------------------------------------ continuous blocks
def test_weekends_and_holidays_do_not_split_a_block_but_a_session_without_1m_does():
    daily = [D(i) for i in (0, 1, 2, 3, 6, 7, 8, 9, 10)]
    assert len(split_blocks([D(3), D(6), D(7)], daily)) == 1                       # Fri -> Mon: no Daily session in between
    blocks = split_blocks([D(6), D(7), D(9), D(10)], daily)                        # day 8 is a stored session without 1m
    assert [b["days"] for b in blocks] == [[D(6), D(7)], [D(9), D(10)]]
    assert "1 stored Daily session(s)" in blocks[1]["gap_before"] and D(8) in blocks[1]["gap_before"]
    far = split_blocks(["2030-01-01", "2030-01-03", "2030-02-01"], daily)          # outside Daily coverage: old 5-day rule
    assert len(far) == 2 and "no Daily data there to verify" in far[1]["gap_before"]


def test_plan_lists_every_block_says_why_they_are_separate_and_lets_one_be_selected(store):
    fill(store, [0, 1, 2, 3, 6, 7, 8, 9, 10], mrows(6) + mrows(7) + mrows(9) + mrows(10))
    plan = plan_session(store, "hybrid")
    assert (plan["from_date"], plan["to_date"], plan["selected_block"]) == (D(9), D(10), 1)       # default: most recent
    assert [(b["from_date"], b["to_date"], b["sessions"], b["minute_candles"], b["selected"]) for b in plan["blocks"]] == [
        (D(6), D(7), 2, 750, False), (D(9), D(10), 2, 750, True)]
    assert "2 separate blocks" in plan["warnings"][0] and D(6) in plan["warnings"][0] and "never joined" in plan["warnings"][0]
    assert plan["warmup"].startswith("Approximate warm-up")
    older = plan_session(store, "hybrid", block=0)
    assert (older["from_date"], older["to_date"]) == (D(6), D(7)) and older["candles"] == 2
    end = older["end"]
    assert end["later_block"] == {"from_date": D(9), "to_date": D(10)} and end["daily_sessions_after_without_1m"] == 1
    assert end["first_daily_after"] == D(8) and end["provider_checked"] is False
    with pytest.raises(PlanError):
        plan_session(store, "hybrid", block=5)


def test_end_of_data_information_for_the_latest_block(store):
    fill(store, [0, 1, 2, 3, 6, 7, 8, 9], mrows(6) + mrows(7))      # Daily goes on to day 9, 1m stops at day 7
    end = plan_session(store, "hybrid")["end"]
    assert end == {"last_minute_day": D(7), "last_daily_session": D(7), "last_stored_daily": D(9),
                   "daily_sessions_after_without_1m": 2, "first_daily_after": D(8), "later_block": None, "provider_checked": False}


# ------------------------------------------------------------------ API / snapshot
@pytest.fixture
def client(tmp_path):
    s = ReplayStore(tmp_path / "cov_api.db")
    replay_api.set_store(s)
    yield TestClient(app), s
    replay_api.set_store(None)


def test_snapshot_carries_the_session_completeness_and_block_information(client):
    c, s = client
    fill(s, [0, 1, 2, 3, 6, 7, 8, 9, 10], mrows(6) + mrows(7, drop=range(10, 20)) + mrows(9) + mrows(10))
    r = c.post("/api/replay/sessions", json={"mode": "hybrid", "block": 0}).json()
    assert r["config"]["from_date"] == D(6) and r["total"] == 2
    snap0 = r["snapshot"]
    assert snap0["data_quality"]["status"] == "Complete" and snap0["data_quality"]["actual"] == 375
    snap1 = c.get(f"/api/replay/sessions/{r['id']}", params={"index": 1}).json()["snapshot"]
    assert (snap1["data_quality"]["status"], snap1["data_quality"]["missing"], snap1["data_quality"]["missing_ranges"]) == ("Incomplete", 10, ["09:25-09:34"])
    ses = snap1["session"]
    assert ses["selected_block"] == 0 and len(ses["blocks"]) == 2 and ses["quality"]["incomplete"] == 1 and ses["end"]["later_block"]
    assert any("1 of 2 Daily sessions are Complete" in w for w in snap1["warnings"])
    assert snap1["at_end"] is True
    q = c.get("/api/replay/quality", params={"mode": "hybrid", "block": 0}).json()
    assert [x["status"] for x in q["sessions"]] == ["Complete", "Incomplete"]
    latest = c.post("/api/replay/sessions", json={"mode": "hybrid"}).json()
    assert latest["config"]["from_date"] == D(9)                                  # default is still the most recent block
    assert c.post("/api/replay/sessions", json={"mode": "hybrid", "block": 9}).status_code == 400


def test_event_log_entries_carry_trade_id_and_direction(client):
    from tests.replay.test_intraday import BUY_D, whipsaw_path
    from tests.replay.helpers import csv_text, daily_rows
    c, _ = client
    mins = [(m.ts, m.open, m.high, m.low, m.close, 0.0) for m in minute_day(6, 35000, whipsaw_path())]
    c.post("/api/replay/import", json={"csv_text": csv_text(daily_rows(BUY_D + [38000]), "1D")})
    c.post("/api/replay/import", json={"csv_text": csv_text(mins, "1m")})
    snap = c.post("/api/replay/sessions", json={"mode": "hybrid"}).json()["snapshot"]
    log = snap["event_log"]
    assert [(e["type"], e["trade"], e["side"]) for e in log if e["type"] in ("BUY_ENTRY", "STOP_EXIT", "REVERSAL_ENTRY")] == [
        ("BUY_ENTRY", 1, "BUY"), ("STOP_EXIT", 1, "BUY"), ("REVERSAL_ENTRY", 2, "SELL")]
    assert all(e["time"][11:16] >= "09:15" for e in log)      # original 1-minute timestamps (IST), never day-level
