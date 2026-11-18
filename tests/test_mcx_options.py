"""MCX gold / silver options in the options tools (owner, 18 Nov 2026): the contract list, the futures each expiry is an option on, the P&L lot,
MCX hours and the morning collector window. Instrument rows are SYNTHETIC copies of the Angel One master's shape."""
import datetime as dt

from app.options import collector, intraday, master
from app.options.master import IST


def row(sym, token, exch, itype, name, expiry, strike=-1, lot="100", tick="50.000000"):
    return {"symbol": sym, "token": token, "exch_seg": exch, "instrumenttype": itype, "name": name, "expiry": expiry,
            "strike": str(strike), "lotsize": lot, "tick_size": tick}


ROWS = [
    row("GOLDM05DEC26FUT", "f1", "MCX", "FUTCOM", "GOLDM", "05DEC2026"),
    row("GOLDM05JAN27FUT", "f2", "MCX", "FUTCOM", "GOLDM", "05JAN2027"),
    row("GOLDM27NOV26131000CE", "o1", "MCX", "OPTFUT", "GOLDM", "27NOV2026", 13100000),
    row("GOLDM27NOV26131000PE", "o2", "MCX", "OPTFUT", "GOLDM", "27NOV2026", 13100000),
    row("GOLDM29DEC26132000CE", "o3", "MCX", "OPTFUT", "GOLDM", "29DEC2026", 13200000),
    row("NIFTY", "99926000", "NSE", "AMXIDX", "NIFTY", "", 0, "1", "0"),
    row("NIFTY24NOV2625300CE", "n1", "NFO", "OPTIDX", "NIFTY", "24NOV2026", 2530000, "65", "5.000000"),
]


def info_for(name, monkeypatch):
    monkeypatch.setattr(master, "_load_rows", lambda day, force: (ROWS, "test"))
    return master.load_underlying(name, today=dt.date(2026, 11, 18))


def test_goldm_options_sit_on_the_futures_of_their_month_with_a_rs10_lot(monkeypatch):
    info = info_for("GOLDM", monkeypatch)
    assert (info.exch, info.spot_exch, info.black76) == ("MCX", "MCX", True)
    assert info.underlying_for(dt.date(2026, 11, 27)) == ("f1", "GOLDM05DEC26FUT")       # the Nov options are on the Dec futures
    assert info.underlying_for(dt.date(2026, 12, 29)) == ("f2", "GOLDM05JAN27FUT")
    assert info.spot_token == "f1"
    c = info.contracts[0]
    assert (c.lot_size, c.exch_lot, c.strike, c.tick_size, c.option_type) == (10, 100, 131000.0, 0.5, "CE")   # Rs 10 a point for one 100 g lot
    assert info.carry(0.065) == 0.065                                                      # Black-76


def test_an_mcx_expiry_stays_tradable_until_its_evening_close(monkeypatch):
    info = info_for("GOLDM", monkeypatch)
    assert master.expiries(info, dt.datetime(2026, 11, 27, 20, 0, tzinfo=IST))[0] == dt.date(2026, 11, 27)    # NSE would have ended it at 15:30
    assert master.expiries(info, dt.datetime(2026, 11, 27, 23, 31, tzinfo=IST))[0] == dt.date(2026, 12, 29)


def test_the_index_path_is_unchanged(monkeypatch):
    info = info_for("NIFTY", monkeypatch)
    assert (info.exch, info.spot_exch, info.black76, info.spot_token) == ("NFO", "NSE", False, "99926000")
    assert info.contracts[0].lot_size == 65 and info.carry(0.065) == 0.0 and info.underlying_for(dt.date(2026, 11, 24)) == ("99926000", "NIFTY")


def test_mcx_minutes_keep_the_evening_session():
    raw = [["2026-11-17T09:00:00+05:30", 1, 1, 1, 1, 1], ["2026-11-17T21:30:00+05:30", 2, 2, 2, 2, 1], ["2026-11-17T23:56:00+05:30", 3, 3, 3, 3, 1]]
    rows, bad = intraday.clean_rows(raw, hours=("09:00", "23:55"))
    assert [r[0][11:] for r in rows] == ["09:00", "21:30"] and bad == 1
    rows, bad = intraday.clean_rows(raw)                                                   # the NSE default drops the evening
    assert rows == [] and bad == 3


def test_the_mcx_collector_runs_only_in_the_quiet_morning():
    assert collector.due_mcx(dt.datetime(2026, 11, 18, 5, 30), None)                       # Wednesday 05:30
    assert not collector.due_mcx(dt.datetime(2026, 11, 18, 8, 0), None)                    # too close to the 09:00 open
    assert not collector.due_mcx(dt.datetime(2026, 11, 18, 5, 30), "2026-11-18")           # already done today
    assert collector.due_mcx(dt.datetime(2026, 11, 21, 6, 0), None)                        # Saturday collects Friday
    assert not collector.due_mcx(dt.datetime(2026, 11, 22, 6, 0), None)                    # Sunday: nothing new
    assert not collector.due_mcx(dt.datetime(2026, 11, 16, 6, 0), None)                    # Monday: the weekend had no session
