"""BANKNIFTY expiry calendar for the Dhan replay: checked against the data (every day the ATM straddle collapses is an expiry by this calendar)."""
import datetime as dt

from app.options import dhan_replay as d


def days(a, b):
    x, out = dt.date.fromisoformat(a), []
    while x <= dt.date.fromisoformat(b):
        if x.weekday() < 5:
            out.append(x.isoformat())
        x += dt.timedelta(days=1)
    return out


def test_banknifty_calendar_eras():
    e = set(d.banknifty_expiries(days("2022-01-03", "2026-06-30")))
    assert dt.date(2022, 1, 6) in e                       # Thursday weeklies
    assert dt.date(2023, 9, 6) in e                       # Wednesday weeklies from Sep 2023
    assert dt.date(2023, 9, 28) in e and dt.date(2023, 9, 27) not in e     # the monthly week kept the last Thursday until Feb 2024
    assert dt.date(2024, 3, 27) in e                      # monthly on the last Wednesday
    assert dt.date(2024, 11, 13) in e and dt.date(2024, 11, 20) not in e   # weeklies ended
    assert dt.date(2025, 6, 26) in e                      # monthly last Thursday in 2025
    assert dt.date(2025, 9, 30) in e                      # monthly last Tuesday from Sep 2025


def test_holiday_moves_the_expiry_to_the_previous_session():
    ds = [x for x in days("2024-12-01", "2025-01-10") if x != "2024-12-25"]
    assert dt.date(2024, 12, 24) in d.banknifty_expiries(ds)


def test_flag_switches_to_monthly_after_the_weeklies():
    assert d.flag_for("BANKNIFTY", "2024-11-19") == "WEEK" and d.flag_for("BANKNIFTY", "2024-11-21") == "MONTH" and d.flag_for("NIFTY", "2026-01-01") == "WEEK"
