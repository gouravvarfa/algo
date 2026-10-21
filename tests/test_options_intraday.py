"""Intraday 1-minute store/API and the legacy Bhavcopy parser. All rows here are SYNTHETIC fixtures."""
import datetime as dt
import io
import zipfile

import pytest

from app.options import api, bhavcopy_legacy as bl, intraday
from tests.test_options_api import client  # noqa: F401

D = "2026-03-09"


# ---------------------------------------------------------------- clean_rows: reject, never repair
def test_clean_rows_keeps_real_minutes_including_1530_and_rejects_bad_ones():
    raw = [["2026-03-09T09:15:00+05:30", 10, 12, 9, 11, 5], ["2026-03-09T15:30:00+05:30", 10, 10, 10, 10, 3],
           ["2026-03-09T09:14:00+05:30", 10, 10, 10, 10, 1],          # pre-open
           ["2026-03-09T09:16:00+05:30", 10, 9, 8, 11, 1],             # high < close
           ["2026-03-09T09:17:00+05:30", 0, 0, 0, 0, 0],               # non-positive
           ["garbage"]]
    rows, bad = intraday.clean_rows(raw)
    assert [r[0] for r in rows] == ["2026-03-09 09:15", "2026-03-09 15:30"] and bad == 4


@pytest.fixture
def iclient(client, tmp_path, monkeypatch):  # noqa: F811
    db = tmp_path / "i.db"
    c = intraday.connect(db)
    put = lambda e, k, o, ts, px, v=10: c.execute("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)", ("NIFTY", e, k, o, f"{D} {ts}", px, px, px, px, v))
    with c:
        for ts, sp in (("09:15", 22500), ("09:16", 22520), ("09:17", 22540)):
            put("", 0.0, "IDX", ts, sp, 0)
        put("2026-03-10", 22500.0, "CE", "09:15", 100.0)
        put("2026-03-10", 22500.0, "CE", "09:17", 110.0)                 # no trade at 09:16
        put("2026-03-10", 22500.0, "PE", "09:16", 90.0)                  # first trade only at 09:16
    monkeypatch.setattr(api, "INTRADAY_DB", db)
    return client


def test_days_and_minutes(iclient):
    assert [d["date"] for d in iclient.get("/api/options/intraday/days").json()["days"]] == [D]
    assert iclient.get(f"/api/options/intraday/minutes?date={D}").json()["minutes"] == ["09:15", "09:16", "09:17"]
    assert iclient.get("/api/options/intraday/minutes?date=2026-03-10").status_code == 404


def test_chain_uses_only_trades_at_or_before_the_minute_and_reports_age(iclient):
    c = iclient.get(f"/api/options/intraday/chain?date={D}&time=09:16").json()
    row = next(r for r in c["rows"] if r["strike"] == 22500)
    assert c["underlying"] == 22520 and c["mode"] == "intraday"
    assert row["CE"]["ltp"] == 100.0 and row["CE"]["age_min"] == 1 and row["CE"]["ts"] == "09:15"   # last REAL trade, not 110
    assert row["PE"]["ltp"] == 90.0
    c = iclient.get(f"/api/options/intraday/chain?date={D}&time=09:15").json()
    pe = next(r for r in c["rows"] if r["strike"] == 22500).get("PE")
    assert pe is None or pe["ltp"] is None                                                          # no trade yet: nothing invented
    assert iclient.get(f"/api/options/intraday/chain?date={D}&time=9.15").status_code == 422


def test_intraday_mark_values_positions_at_the_minute(iclient):
    pos = {"side": "SELL", "opt": "CE", "strike": 22500, "expiry": "2026-03-10", "lots": 1, "lot_size": 65, "entry": 100.0, "entry_date": D}
    r = iclient.post("/api/options/intraday/mark", json={"date": D, "time": "09:17", "positions": [pos]}).json()
    assert r["positions"][0]["ltp"] == 110.0 and r["positions"][0]["pnl"] == pytest.approx(-650) and r["underlying"] == 22540
    r = iclient.post("/api/options/intraday/mark", json={"date": D, "time": "09:15", "positions": [{**pos, "opt": "PE"}]}).json()
    assert r["positions"][0]["ltp"] is None and "no trade yet" in r["positions"][0]["note"]


# ---------------------------------------------------------------- legacy Bhavcopy
def test_derive_lot_needs_consistent_futures_turnover():
    fut = lambda n, val, px: {"CONTRACTS": n, "VAL_INLAKH": val, "OPEN": px, "HIGH": px, "LOW": px, "CLOSE": px}
    assert bl.derive_lot([fut(1000, 1000 * 25 * 24000 / 1e5, 24000), fut(50, 50 * 25 * 24100 / 1e5, 24100)])[0] == 25
    assert bl.derive_lot([fut(1000, 1000 * 25 * 24000 / 1e5, 24000), fut(50, 50 * 75 * 24100 / 1e5, 24100)])[0] is None   # disagree
    assert bl.derive_lot([fut(0, 0, 24000)])[0] is None


def test_index_closes_accepts_the_historic_index_names():
    t = "Index Name,Index Date,Open,High,Low,Closing Index Value\nCNX Nifty,02-01-2015,1,1,1,8395.45\nCNX Bank,02-01-2015,1,1,1,19057.8\nNifty 100,x,1,1,1,5\n"
    assert bl.index_closes(t) == {"NIFTY": 8395.45, "BANKNIFTY": 19057.8}


def _zip(text):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("fo.csv", text)
    return b.getvalue()


HDR = "INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP\n"


def test_legacy_parse_rows_lot_underlying_and_rejections():
    txt = HDR + ("FUTIDX,NIFTY,25-Jul-2024,0,XX,24000,24000,24000,24000,24000,100,600,0,0,05-JUL-2024\n"
                 "OPTIDX,NIFTY,11-Jul-2024,24300,CE,100,120,90,110,111,30,1,500,10,05-JUL-2024\n"
                 "OPTIDX,NIFTY,11-Jul-2024,24300,PE,100,90,95,92,92,3,1,5,1,05-JUL-2024\n"          # high < open: rejected
                 "OPTIDX,BANKNIFTY,10-Jul-2024,52000,CE,1,1,1,1,1,1,1,1,1,05-JUL-2024\n")           # no BN futures -> not loaded
    rows, bad, notes = bl.parse(dt.date(2024, 7, 5), _zip(txt), {"NIFTY": 24323.85, "BANKNIFTY": 52660.35}, {"NIFTY", "BANKNIFTY"})
    assert len(rows) == 1 and len(bad) == 1
    r = rows[0]
    assert r[1:5] == ("NIFTY", "2024-07-11", 24300.0, "CE") and r[10] == 24323.85 and r[13] == 30 and r[14] == 25
    assert any(n.startswith("BANKNIFTY: NOT loaded") for n in notes)


def test_legacy_parse_without_an_index_close_loads_nothing():
    txt = HDR + "FUTIDX,NIFTY,25-Jul-2024,0,XX,24000,24000,24000,24000,24000,100,600,0,0,05-JUL-2024\nOPTIDX,NIFTY,11-Jul-2024,24300,CE,1,1,1,1,1,1,1,1,1,05-JUL-2024\n"
    rows, _, notes = bl.parse(dt.date(2024, 7, 5), _zip(txt), {}, {"NIFTY"})
    assert rows == [] and "index close missing" in notes[0]


# ---------------------------------------------------------------- simulator stop-loss
def test_sl_hit_uses_minutes_after_entry_and_stops_at_the_viewed_minute():
    p = api.SLPos(id="a", side="SELL", opt="CE", strike=100, expiry="2026-03-10", entry_date="2026-03-09", entry_time="09:16", sl=115.0)
    m = {"2026-03-09": [("09:15", 100, 130, 99, 100), ("09:16", 100, 101, 99, 100), ("09:17", 101, 116, 100, 112), ("09:18", 112, 120, 110, 118)]}
    one = lambda upto_t: api.first_sl_hit(p, "2026-03-09", upto_t, lambda d: m.get(d, []), lambda d: None, ["2026-03-09"])
    assert one("09:16") is None                                     # the 09:15 spike was before entry
    assert one("09:20") == {"price": 115.0, "date": "2026-03-09", "time": "09:17", "how": "SL", "source": "1-min"}


def test_sl_hit_gap_and_eod_fallback_and_buy_side():
    p = api.SLPos(id="b", side="SELL", opt="PE", strike=100, expiry="2026-03-12", entry_date="2026-03-09", sl=115.0)   # entered at close
    eod = {"2026-03-09": (90, 200, 80), "2026-03-10": (100, 110, 95), "2026-03-11": (120, 125, 118)}
    hit = api.first_sl_hit(p, "2026-03-11", None, lambda d: [], lambda d: eod.get(d), sorted(eod))
    assert hit["date"] == "2026-03-11" and hit["how"] == "SL (gap open)" and hit["price"] == 120 and hit["source"] == "EOD high/low"
    q = api.SLPos(id="c", side="BUY", opt="CE", strike=100, expiry="2026-03-12", entry_date="2026-03-09", sl=85.0)
    assert api.first_sl_hit(q, "2026-03-11", None, lambda d: [], lambda d: {"2026-03-10": (100, 101, 84)}.get(d), ["2026-03-09", "2026-03-10"])["price"] == 85.0
