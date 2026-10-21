import datetime as dt

from app.options.collector import due


def test_due_only_after_close_on_weekdays_once_per_day():
    wed = dt.datetime(2026, 10, 21, 15, 50)
    assert due(wed, None) and due(wed, "2026-10-20")
    assert not due(wed, "2026-10-21")                                  # already done today
    assert not due(wed.replace(hour=15, minute=30), None)              # market still open
    assert not due(dt.datetime(2026, 10, 24, 16, 0), None)             # Saturday
