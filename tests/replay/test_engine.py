"""Index replay engine + API: causality (no look-ahead), replay == backtest,
gap protocol and breakeven through stored candles, persistence, index-point
units, no broker I/O."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.replay import api as replay_api
from app.main import app
from app.replay.engine import backtest_report, run_strategy, snapshot
from app.replay.store import ReplayStore
from app.replay.timeframes import bars15_by_date
from app.strategy.top_bottom.backtest import run_top_bottom_backtest
from tests.replay.helpers import START, csv_text, daily_rows, intraday_rows, rows15

STORY = [20, 10, 30, 40, 35, 35, 41, 8, 30, 28, 33, 25, 31]  # BUY at 40 on idx6, gap-down day idx7
OPENS = {7: 5}
HIGHS, LOWS = {7: 12}, {7: 3.5}
GAP15 = rows15(7, (5, 12, 4, 8), [(8, 9, 3.5, 4.5)])
BE = [20, 10, 30, 40, 35, 35, 40.5, 40.7, 39.9, 39.5, 38, 37]  # +2% (40.8) reached on idx7
BE_HIGHS = {7: 41.0}


def _rows(closes=STORY):
    return daily_rows(closes, OPENS, HIGHS, LOWS)


def _strip(s):
    return {k: v for k, v in s.items() if k not in ("total", "at_end", "window_start")}


def test_gap_protocol_runs_on_stored_15m_candles():
    run = run_strategy(_rows(), "1D", GAP15)
    t = run.trades[0]
    assert (t.direction, t.entry_price, t.initial_sl) == ("BUY", 40, 10)
    assert (t.exit_reason, t.exit_price, t.gap_state) == ("GAP_EXIT_15M", 4, "GAP_EXITED")
    assert run.gap15_days == [START + __import__('datetime').timedelta(days=7)]


def test_without_15m_data_gap_protocol_is_off_and_close_rules_apply():
    run = run_strategy(_rows(), "1D", None)
    t = run.trades[0]
    assert t.exit_reason == "TRAILING_STOP_REVERSAL" and t.gap_state is None
    assert snapshot(run, 7)["gap_protocol"] == {"active": False, "days_with_15m_data": 0}


def test_matches_existing_strategy_directly():
    rows = _rows()
    run = run_strategy(rows, "1D", GAP15)
    _, direct = run_top_bottom_backtest(
        "BANKNIFTY", "BANKNIFTY", "", "INDEX", 0, [r[0] for r in rows], [r[4] for r in rows], [r[1] for r in rows],
        bars15_by_date(GAP15), [r[2] for r in rows], [r[3] for r in rows],
    )
    key = lambda ts: [(t.direction, t.entry_price, t.exit_price, t.exit_reason) for t in ts]
    assert key(run.trades) == key(direct)


def test_breakeven_event_and_stop_visible_in_replay_only_from_its_bar():
    run = run_strategy(daily_rows(BE, highs=BE_HIGHS), "1D")
    before, at = snapshot(run, 6), snapshot(run, 7)
    assert before["events"] == [] and before["open_trade"]["stop"] == 10 and not before["open_trade"]["breakeven_active"]
    assert [e["label"] for e in at["events"]] == ["2% PROFIT → BREAKEVEN SL"]
    assert at["open_trade"]["stop"] == 40 and at["open_trade"]["breakeven_active"]
    done = snapshot(run, 8)
    # exit at breakeven 40 -> flat and waiting for a NEW System Point (no reversal)
    assert done["trades"][0]["exit"] == 40 and done["trades"][0]["breakeven"] and done["position"] == "FLAT" and done["waiting"]


@pytest.mark.parametrize("k", [3, 6, 7, 9])
def test_future_candles_never_change_what_is_revealed(k):
    a = run_strategy(_rows(), "1D", GAP15)
    altered = STORY[: k + 1] + [200 + i for i in range(len(STORY) - k - 1)]
    b = run_strategy(daily_rows(altered, OPENS, HIGHS, LOWS), "1D", GAP15)
    assert _strip(snapshot(a, k)) == _strip(snapshot(b, k))


@pytest.mark.parametrize("k", range(2, 13))
def test_truncated_run_equals_revealed_part_of_full_run(k):
    rows = _rows()
    full = run_strategy(rows, "1D", GAP15)
    prefix = run_strategy(rows[: k + 1], "1D", GAP15)
    assert _strip(snapshot(prefix, k)) == _strip(snapshot(full, k))


def test_replay_final_state_equals_backtest():
    run = run_strategy(_rows(), "1D", GAP15)
    assert _strip(snapshot(run, len(run.candles) - 1)) == _strip(backtest_report(run))


def test_snapshot_exposes_only_revealed_candles():
    run = run_strategy(_rows(), "1D", GAP15)
    s = snapshot(run, 4)
    assert len(s["candles"]) == 5 and s["trades"] == [] and s["position"] == "FLAT"
    s6 = snapshot(run, 6)
    assert s6["position"] == "BUY" and s6["open_trade"]["stop"] == 10 and s6["stats"]["trades_closed"] == 0
    assert s6["gap_levels"] == []  # the day-7 gap is not known yet


def test_results_are_index_points_not_rupees():
    s = snapshot(run_strategy(_rows(), "1D", GAP15), 8)
    assert s["unit"] == "BankNifty Index Points" and s["instrument"]["instrument_type"] == "INDEX"
    assert s["stats"]["realized_points"] == 4 - 40
    flat = str({k: v for k, v in s.items() if k != "rule_gaps"}).lower()  # rule_gaps text mentions what is NOT modelled
    assert "lot" not in flat and "rupee" not in flat and "pnl" not in flat


def test_slippage_is_in_points():
    s = snapshot(run_strategy(_rows(), "1D", GAP15, slippage_points=1.0), 8)
    assert s["stats"]["realized_points"] == (4 - 40) - 2


def test_intraday_interval_runs_plain_top_bottom_without_gap_protocol():
    rows = []
    for day, px in enumerate([100, 90, 110, 120, 115, 115, 121, 130]):
        rows += [(r[0], px, px, px, px, 0.0) for r in intraday_rows(day, 15, px)]
    run = run_strategy(rows, "15m", GAP15)
    assert run.gap15_days == [] and len(run.candles) == 8 * 25
    assert snapshot(run, 60)["interval"] == "15m"


def test_unsupported_interval_rejected():
    with pytest.raises(ValueError):
        run_strategy(_rows(), "2m")


def test_no_broker_order_code_in_replay_module():
    root = Path(replay_api.__file__).resolve().parent
    text = "".join(p.read_text(encoding="utf-8") for p in root.glob("*.py")).lower()
    for token in ("place_order", "placeorder", "createorder", "/order/", "orderbook", "modifyorder"):
        assert token not in text


# ------------------------------ API ---------------------------------
@pytest.fixture
def client(tmp_path):
    replay_api.set_store(ReplayStore(tmp_path / "api.db"))
    yield TestClient(app), tmp_path
    replay_api.set_store(None)


def _load(c, rows=None, interval="1D"):
    return c.post("/api/replay/import", json={"csv_text": csv_text(rows or _rows(), interval)})


def _cfg(**kw):
    return {"interval": "1D", "from_date": "2026-09-01", "to_date": "2026-09-13", **kw}


def test_instruments_only_advertise_stored_intervals(client):
    c, _ = client
    assert c.get("/api/replay/instruments").json()["available_intervals"] == []
    _load(c)
    info = c.get("/api/replay/instruments").json()
    assert info["available_intervals"] == ["1D"] and info["instrument"]["token"] == "99926009"
    assert info["coverage"][0]["instrument_type"] == "INDEX" and info["coverage"][0]["sources"] == ["IMPORT"]
    r = c.post("/api/replay/sessions", json=_cfg(interval="5m"))
    assert r.status_code == 400 and "No 5m candles" in r.json()["detail"]


def test_futures_csv_rejected_with_reason_and_nothing_stored(client):
    c, _ = client
    r = c.post("/api/replay/import", json={"csv_text": csv_text(_rows(), instrument="BANKNIFTY27OCT26FUT")})
    assert r.status_code == 422 and "futures" in r.json()["detail"]["message"].lower()
    assert c.get("/api/replay/instruments").json()["coverage"] == []


def test_import_then_replay_equals_backtest_endpoint(client):
    c, _ = client
    assert _load(c).status_code == 200
    _load(c, GAP15, "15m")
    created = c.post("/api/replay/sessions", json=_cfg()).json()
    sid, total = created["id"], created["total"]
    final = c.get(f"/api/replay/sessions/{sid}", params={"index": total - 1}).json()["snapshot"]
    bt = c.post("/api/replay/backtest", json=_cfg()).json()
    assert _strip(final) == _strip(bt) and final["gap_protocol"]["active"]


def test_step_speed_and_refresh_restore(client):
    c, tp = client
    _load(c)
    sid = c.post("/api/replay/sessions", json=_cfg(speed=1)).json()["id"]
    assert c.post(f"/api/replay/sessions/{sid}/position", json={"index": 5, "speed": 10}).json()["index"] == 5
    assert c.post(f"/api/replay/sessions/{sid}/position", json={"index": 999}).json()["index"] == 12  # clamped
    c.post(f"/api/replay/sessions/{sid}/position", json={"index": 6})
    replay_api.set_store(ReplayStore(tp / "api.db"))  # "refresh": new process state, same database file
    restored = c.get("/api/replay/sessions/latest").json()
    assert restored["snapshot"]["index"] == 6 and restored["config"]["speed"] == 10
    assert restored["snapshot"]["position"] == "BUY"


def test_jump_to_date_and_datetime(client):
    c, _ = client
    _load(c)
    sid = c.post("/api/replay/sessions", json=_cfg()).json()["id"]
    assert c.get(f"/api/replay/sessions/{sid}/index-for", params={"at": "2026-09-08"}).json()["index"] == 7
    assert c.get(f"/api/replay/sessions/{sid}/index-for", params={"at": "2030-01-01T10:00"}).json()["index"] == 12


def test_out_of_coverage_range_and_oversized_runs_give_clear_errors(client, monkeypatch):
    c, _ = client
    _load(c)
    r = c.post("/api/replay/sessions", json=_cfg(from_date="2027-01-01", to_date="2027-02-01"))
    assert r.status_code == 400 and "No 1D candles" in r.json()["detail"]
    monkeypatch.setattr(replay_api, "MAX_CANDLES", 5)
    r = c.post("/api/replay/sessions", json=_cfg())
    assert r.status_code == 400 and "exceed" in r.json()["detail"]


def test_download_refuses_bad_requests(client):
    c, _ = client
    assert c.post("/api/replay/downloads", json={"interval": "2m", "from_date": "2026-01-01", "to_date": "2026-01-02"}).status_code == 400
    assert c.post("/api/replay/downloads", json={"interval": "1D", "from_date": "2026-02-01", "to_date": "2026-01-02"}).status_code == 400


def test_preview_returns_plain_candles_only_and_is_bounded(client, monkeypatch):
    c, _ = client
    assert c.get("/api/replay/preview", params=_cfg()).status_code == 400  # no data yet
    _load(c)
    r = c.get("/api/replay/preview", params=_cfg()).json()
    assert len(r["candles"]) == 13 and set(r["candles"][0]) == {"time", "open", "high", "low", "close"}
    monkeypatch.setattr(replay_api, "MAX_PREVIEW", 5)
    assert c.get("/api/replay/preview", params=_cfg()).status_code == 400


def test_gap_event_is_explained_in_plain_words_and_only_from_its_own_day():
    run = run_strategy(_rows(), "1D", GAP15)
    assert snapshot(run, 6)["gap_notes"] == [] and snapshot(run, 6)["gap_days"] == []
    s = snapshot(run, 7)
    assert [g["pct"] for g in s["gap_days"]] == [-87.8]
    (note,) = s["gap_notes"]
    assert note["kind"] == "GAP_EXITED" and note["side"] == "BUY"
    for part in ("87.80% gap down", "structural stop 10.00", "high 12.00, low 4.00)", "below the 15-min low", "exited at 4.00", "No reverse trade"):
        assert part in note["detail"], part


def test_gap_up_entry_note_mentions_waiting_for_the_first_15m_candle():
    closes = [20, 10, 30, 40, 35, 35, 50, 52]
    rows = daily_rows(closes, {6: 45}, {6: 50})
    gap = rows15(6, (45, 47, 43, 46), [(46, 46.5, 44, 45), (45, 48, 45, 48)])
    run = run_strategy(rows, "1D", gap)
    (note,) = snapshot(run, 6)["gap_notes"]
    assert note["kind"] == "GAP_ENTRY" and note["side"] == "BUY"
    assert "waited for the first 15-min candle" in note["detail"].replace("The system waited for the first 15-min candle (09:15-09:30)", "waited for the first 15-min candle") or "waited for" in note["detail"]
    assert "above the 15-min high" in note["detail"] and "47.00" in note["detail"] and "system point 40.00" in note["detail"]
