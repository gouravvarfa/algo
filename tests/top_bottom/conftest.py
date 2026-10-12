import datetime as dt

import pytest


def make_series(closes: list[float], start: str = "2025-01-01"):
    """Business-day dates + matching opens (= previous close, i.e. no gap
    unless a test explicitly overrides an open) for a list of closes."""
    base = dt.datetime.strptime(start, "%Y-%m-%d")
    dates = [base + dt.timedelta(days=i) for i in range(len(closes))]
    opens = [closes[0]] + closes[:-1]
    return dates, closes, opens


@pytest.fixture
def series_factory():
    return make_series
