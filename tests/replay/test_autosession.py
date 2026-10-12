"""Backend-chosen replay sessions: no dates from the user, warm-up kept internal, earlier state reconstructed.
SYNTHETIC fixtures (index-like prices)."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.replay import api as replay_api
from app.replay.store import ReplayStore
from tests.replay.helpers import csv_text, daily_rows, day_ts
from tests.replay.test_intraday import minute_day, ramp


@pytest.fixture
def client(tmp_path):
    replay_api.set_store(ReplayStore(tmp_path / "auto.db"))
    yield TestClient(app), tmp_path
    replay_api.set_store(None)


def rows(minutes):
    return [(m.ts, m.open, m.high, m.low, m.close, 0.0) for m in minutes]


def load(c, daily, minutes):
    assert c.post("/api/replay/import", json={"csv_text": csv_text(daily, "1D")}).status_code == 200
    if minutes:
        assert c.post("/api/replay/import", json={"csv_text": csv_text(rows(minutes), "1m")}).status_code == 200


SELL_HELD = [60000, 60500, 59500, 59000, 59400, 59400, 58900]  # day 6 closes below the 59000 Bottom: SELL @59000, stop 60500


def test_run_replay_needs_no_dates_and_replays_the_whole_stored_intraday_block(client):
    c, _ = client
    d7 = {}
    ramp(d7, 0, 60, 58900, 60600)
    load(c, daily_rows(SELL_HELD), minute_day(7, 58900, d7))
    r = c.post("/api/replay/sessions", json={"mode": "intraday"}).json()   # <- all the UI sends
    cfg, snap = r["config"], r["snapshot"]
    assert cfg["interval"] == "1m" and cfg["from_date"] == cfg["to_date"] == day_ts(7).date().isoformat()
    assert snap["total"] == 375 and snap["session"]["candles"] == 375 and snap["interval"] == "1m"
    assert c.get("/api/replay/plan", params={"mode": "intraday"}).json()["candles"] == 375


def test_a_position_opened_before_the_first_replay_day_is_reconstructed_from_daily_history_and_exited_intraday(client):
    c, _ = client
    d7 = {}
    ramp(d7, 0, 60, 58900, 60600)       # pokes through the carried-in SELL's stop (60500) ...
    ramp(d7, 61, 120, 60600, 60300)     # ... and comes back below it
    load(c, daily_rows(SELL_HELD), minute_day(7, 58900, d7))
    r = c.post("/api/replay/sessions", json={"mode": "intraday"}).json()
    sid, total = r["id"], r["total"]
    snap = c.get(f"/api/replay/sessions/{sid}", params={"index": total - 1, "window": total}).json()["snapshot"]
    assert any("Position carried in: SELL" in w and "stop 60,500.00" in w for w in snap["warnings"])   # never silently flat
    sell, buy = snap["trades"][0], snap["trades"][1]
    assert sell["carried_in"] and sell["side"] == "SELL" and sell["reason"] == "STOP_LOSS_REVERSAL" and sell["exit"] == 60500
    assert buy["side"] == "BUY" and buy["entry"] == 60500 and buy["exit"] is None
    assert any(e["label"].startswith("ROUND TRIP") for e in snap["events"])
    # replay == backtest with the same automatic setup
    bt = c.post("/api/replay/backtest", json={"mode": "intraday"}).json()
    strip = lambda s: {k: v for k, v in s.items() if k not in ("total", "at_end", "window_start")}
    assert strip(snap) == strip(bt)


def test_waiting_state_after_an_earlier_breakeven_exit_is_carried_in(client):
    c, _ = client
    daily = daily_rows([20000, 10000, 30000, 40000, 35000, 35000, 40500, 40700, 39900], highs={7: 41000})  # BE exit on day 8
    d9 = {}
    ramp(d9, 0, 60, 39900, 41500)       # crosses the consumed Top (40700): must NOT buy
    load(c, daily, minute_day(9, 39900, d9))
    r = c.post("/api/replay/sessions", json={"mode": "intraday"}).json()
    snap = c.get(f"/api/replay/sessions/{r['id']}", params={"index": r["total"] - 1}).json()["snapshot"]
    assert any("Waiting state carried in" in w for w in snap["warnings"])
    assert snap["trades"] == [] and snap["position"] == "FLAT"


def test_only_the_most_recent_contiguous_block_of_1m_data_is_replayed_and_the_rest_is_reported(client):
    c, _ = client
    closes = [20000, 10000, 30000, 40000, 35000, 35000] + [35000] * 40
    load(c, daily_rows(closes), minute_day(6, 35000) + minute_day(40, 35000))
    plan = c.get("/api/replay/plan", params={"mode": "intraday"}).json()
    assert plan["from_date"] == plan["to_date"] == day_ts(40).date().isoformat()
    assert any("2 separate blocks" in w and day_ts(6).date().isoformat() in w for w in plan["warnings"])


def test_missing_history_gives_a_clear_message_instead_of_silently_starting_flat(client):
    c, _ = client
    assert "No 1m candles stored" in c.post("/api/replay/sessions", json={"mode": "intraday"}).json()["detail"]
    c.post("/api/replay/import", json={"csv_text": csv_text(rows(minute_day(7, 100)), "1m")})
    assert "needs 1D candles" in c.post("/api/replay/sessions", json={"mode": "intraday"}).json()["detail"]
    c.post("/api/replay/import", json={"csv_text": csv_text(daily_rows([100, 101]), "1D")})
    assert "Not enough 1D history" in c.post("/api/replay/sessions", json={"mode": "intraday"}).json()["detail"]


def test_daily_engine_defaults_to_the_whole_stored_range_and_says_it_starts_flat(client):
    c, _ = client
    load(c, daily_rows([20000, 10000, 30000, 40000, 35000, 35000, 41000]), [])
    r = c.post("/api/replay/sessions", json={"mode": "daily"}).json()
    assert r["config"]["from_date"] == day_ts(0).date().isoformat() and r["total"] == 7
    assert any("starts FLAT at the first stored candle" in w for w in r["snapshot"]["warnings"])


def test_pick_start_preview_and_refresh_need_no_dates_either(client):
    c, tp = client
    d7 = {}
    ramp(d7, 0, 60, 58900, 60600)
    load(c, daily_rows(SELL_HELD), minute_day(7, 58900, d7))
    prev = c.get("/api/replay/preview", params={"mode": "intraday"}).json()
    assert prev["interval"] == "1m" and len(prev["candles"]) == 375 and set(prev["candles"][0]) == {"time", "open", "high", "low", "close"}
    sid = c.post("/api/replay/sessions", json={"mode": "intraday"}).json()["id"]
    c.post(f"/api/replay/sessions/{sid}/position", json={"index": 40})
    replay_api.set_store(ReplayStore(tp / "auto.db"))          # fresh process, same database
    back = c.get("/api/replay/sessions/latest").json()
    assert back["snapshot"]["index"] == 40 and back["config"]["mode"] == "intraday" and back["snapshot"]["warnings"]
