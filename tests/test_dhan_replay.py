"""Dhan download -> the Simulator's replay database, and the API choosing between it and the Angel One database. All rows are SYNTHETIC fixtures."""
import datetime as dt
import sqlite3

import pytest

from app.options import api, dhan_replay, intraday
from tests.test_options_api import client  # noqa: F401

SRC_SCHEMA = """CREATE TABLE candles(symbol TEXT, flag TEXT, code INT, opt TEXT, rel INT, ts TEXT, strike REAL, open REAL, high REAL, low REAL, close REAL,
                volume INTEGER, iv REAL, oi REAL, spot REAL, PRIMARY KEY(symbol, flag, code, opt, rel, ts))"""


def weekdays(a: dt.date, b: dt.date, skip=()):
    out, x = [], a
    while x <= b:
        if x.weekday() < 5 and x not in skip:
            out.append(x.isoformat())
        x += dt.timedelta(days=1)
    return out


def test_the_expiry_follows_the_exchange_calendar_thursday_then_tuesday_with_holiday_shift():
    days = weekdays(dt.date(2024, 3, 11), dt.date(2024, 4, 5), skip=[dt.date(2024, 3, 21)])           # pretend Thursday 21 Mar was a holiday
    ex = dhan_replay.weekly_expiries(days)
    assert dt.date(2024, 3, 20) in ex and dt.date(2024, 3, 21) not in ex                            # moved back to Wednesday
    assert dt.date(2024, 3, 14) in ex and dt.date(2024, 3, 28) in ex
    assert dhan_replay.next_expiry("2024-03-18", ex) == dt.date(2024, 3, 20)
    assert dhan_replay.next_expiry("2024-03-20", ex) == dt.date(2024, 3, 20)                         # the expiry day itself
    days = weekdays(dt.date(2025, 8, 20), dt.date(2025, 9, 12))
    ex = dhan_replay.weekly_expiries(days)
    assert dt.date(2025, 8, 28) in ex and dt.date(2025, 9, 2) in ex and dt.date(2025, 9, 9) in ex   # last Thursday, then Tuesdays
    assert dt.date(2025, 9, 4) not in ex and dt.date(2025, 9, 11) not in ex


def make_src(path, rows):
    c = sqlite3.connect(path)
    c.execute(SRC_SCHEMA)
    c.executemany("INSERT INTO candles VALUES ('NIFTY','WEEK',1,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()


def day_rows(day, base, spot0):
    """CE/PE at ATM (rel 0) and one strike out, minutes 09:15, 09:16, 15:29 and a post-close 15:35 that must not be copied."""
    rows = []
    for i, hm in enumerate(("09:15", "09:16", "15:29", "15:35")):
        sp = spot0 + i
        for o in ("CE", "PE"):
            for rel, k in ((0, 22500.0), (1, 22550.0)):
                px = base + 10 * rel + i
                rows.append((o, rel, f"{day} {hm}", k, px, px + 1, px - 1, px, 7 + i, 12.5, 1000.0, sp))
    return rows


@pytest.fixture
def replay(tmp_path):
    src = tmp_path / "dhan.db"
    make_src(src, day_rows("2024-03-18", 100.0, 22500.0) + day_rows("2024-03-19", 200.0, 22600.0))
    dst = tmp_path / "replay.db"
    return src, dst


def test_build_makes_the_simulator_table_with_the_right_expiry_and_no_post_close_rows(replay):
    src, dst = replay
    out = dhan_replay.build(src, dst, log=lambda *_: None)
    assert out["days"] == 2
    c = intraday.connect(dst)
    opt = c.execute("SELECT expiry, strike, opt, ts, open, high, low, close, volume FROM candles WHERE symbol='NIFTY' AND opt!='IDX' AND ts LIKE '2024-03-18%' ORDER BY ts, opt, strike").fetchall()
    assert {r[0] for r in opt} == {"2024-03-21"}                                                     # the next Thursday
    assert {r[3][11:] for r in opt} == {"09:15", "09:16", "15:29"}                                    # 15:35 is after the close
    ce = [r for r in opt if r[2] == "CE" and r[1] == 22500.0 and r[3].endswith("09:15")][0]
    assert ce[4:] == (100.0, 101.0, 99.0, 100.0, 7)
    idx = c.execute("SELECT expiry, strike, ts, close FROM candles WHERE opt='IDX' AND ts LIKE '2024-03-18%' ORDER BY ts").fetchall()
    assert [r[2][11:] for r in idx] == ["09:15", "09:16", "15:29"] and idx[0][:2] == ("", 0.0) and idx[0][3] == 22500.0
    assert dhan_replay.days(c, "NIFTY") == [{"date": "2024-03-18", "index_minutes": 3, "contracts": 4}, {"date": "2024-03-19", "index_minutes": 3, "contracts": 4}]
    n = c.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
    dhan_replay.build(src, dst, log=lambda *_: None)                                                  # a re-run replaces, never doubles
    assert c.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == n


@pytest.fixture
def rclient(client, replay, tmp_path, monkeypatch):  # noqa: F811
    src, dst = replay
    dhan_replay.build(src, dst, log=lambda *_: None)
    main = tmp_path / "main.db"
    c = intraday.connect(main)
    with c:
        for ts, sp in (("09:15", 24000), ("09:16", 24010)):
            c.execute("INSERT INTO candles VALUES ('NIFTY','',0.0,'IDX',?,?,?,?,?,0)", (f"2026-03-09 {ts}", sp, sp, sp, sp))
        c.execute("INSERT INTO candles VALUES ('NIFTY','2026-03-10',24000.0,'CE','2026-03-09 09:15',50,50,50,50,3)")
    monkeypatch.setattr(api, "INTRADAY_DB", main)
    monkeypatch.setattr(api, "REPLAY_INTRADAY_DB", dst)
    monkeypatch.setattr(api, "_CACHE", {})
    monkeypatch.setattr(api, "_REPLAY", {})
    return client


def test_days_lists_both_sources_and_names_them(rclient):
    days = rclient.get("/api/options/intraday/days").json()["days"]
    assert [(d["date"], d["source"]) for d in days] == [("2024-03-18", "Dhan"), ("2024-03-19", "Dhan"), ("2026-03-09", "Angel One")]


def test_an_older_day_is_replayed_from_the_dhan_file(rclient):
    assert rclient.get("/api/options/intraday/minutes?date=2024-03-18").json()["minutes"] == ["09:15", "09:16", "15:29"]
    c = rclient.get("/api/options/intraday/chain?date=2024-03-18&time=09:16").json()
    assert c["expiry"] == "2024-03-21" and c["underlying"] == 22501.0 and c["expiries"] == ["2024-03-21"]
    row = next(r for r in c["rows"] if r["strike"] == 22500.0)
    assert row["CE"]["ltp"] == 101.0 and row["PE"]["ltp"] == 101.0                                    # the 09:16 minute, base 100 + 1
    far = next(r for r in c["rows"] if r["strike"] == 22550.0)
    assert far["CE"]["ltp"] == 111.0
    assert rclient.get("/api/options/intraday/chain?date=2024-03-18&time=09:15").json()["time"] == "09:15"       # time travel back within the day
    pos = {"side": "SELL", "opt": "CE", "strike": 22500, "expiry": "2024-03-21", "lots": 1, "lot_size": 25, "entry": 100.0, "entry_date": "2024-03-18"}
    r = rclient.post("/api/options/intraday/mark", json={"date": "2024-03-18", "time": "15:29", "positions": [pos]}).json()
    assert r["positions"][0]["ltp"] == 102.0 and r["positions"][0]["pnl"] == pytest.approx(-50.0)


def test_a_day_the_angel_one_database_has_is_never_taken_from_the_dhan_file(rclient, tmp_path, monkeypatch):
    src = tmp_path / "dhan2.db"
    make_src(src, day_rows("2026-03-09", 900.0, 99999.0))                                            # a conflicting copy of the Angel One day
    dst = tmp_path / "replay2.db"
    dhan_replay.build(src, dst, log=lambda *_: None)
    monkeypatch.setattr(api, "REPLAY_INTRADAY_DB", dst)
    c = rclient.get("/api/options/intraday/chain?date=2026-03-09&time=09:16").json()
    assert c["underlying"] == 24010                                                                   # the Angel One index, not 99999
    assert [(d["date"], d["source"]) for d in rclient.get("/api/options/intraday/days").json()["days"]] == [("2026-03-09", "Angel One")]


def test_without_the_dhan_file_everything_is_as_before(rclient, monkeypatch, tmp_path):
    monkeypatch.setattr(api, "REPLAY_INTRADAY_DB", tmp_path / "does-not-exist.db")
    assert [d["date"] for d in rclient.get("/api/options/intraday/days").json()["days"]] == ["2026-03-09"]
    assert rclient.get("/api/options/intraday/minutes?date=2024-03-18").status_code == 404


def test_the_stop_loss_check_reads_the_replay_minutes(rclient):
    body = {"date": "2024-03-18", "time": "15:29", "symbol": "NIFTY",
            "positions": [{"id": "a", "side": "SELL", "opt": "CE", "strike": 22500, "expiry": "2024-03-21", "entry_date": "2024-03-18", "entry_time": "09:15", "sl": 101.5}]}
    r = rclient.post("/api/options/sim/sl_check", json=body)
    assert r.status_code == 200
    hit = r.json()["hits"]["a"]
    assert hit is not None and hit["date"] == "2024-03-18" and hit["time"] is not None            # the 1-minute candles of the replay file were used, not the EOD fallback
