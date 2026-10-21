import datetime as dt

import pandas as pd

from app.live import candle_store as cs


def df(rows):
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"]).assign(date=lambda d: pd.to_datetime(d.date)).set_index("date")


def test_put_get_roundtrip_and_overwrite(tmp_path):
    db = tmp_path / "c.db"
    a = df([("2026-10-19 09:15", 100, 101, 99, 100.5, 10), ("2026-10-19 09:16", 100.5, 102, 100, 101, 5)])
    assert cs.put("48699", "ONE_MINUTE", a, db) == 2
    got = cs.get("48699", "ONE_MINUTE", db=db)
    assert list(got.index) == list(a.index) and got.close.tolist() == [100.5, 101.0]
    cs.put("48699", "ONE_MINUTE", df([("2026-10-19 09:16", 100.5, 103, 100, 102, 6)]), db)           # same bar fetched again: the later fetch wins
    assert cs.get("48699", "ONE_MINUTE", db=db).close.tolist() == [100.5, 102.0] and len(cs.get("48699", "ONE_MINUTE", db=db)) == 2


def test_tokens_and_intervals_are_separate_and_since_filters(tmp_path):
    db = tmp_path / "c.db"
    cs.put("1", "ONE_DAY", df([("2026-10-01", 1, 2, 1, 2, 0), ("2026-10-02", 2, 3, 2, 3, 0)]), db)
    cs.put("2", "ONE_DAY", df([("2026-10-01", 9, 9, 9, 9, 0)]), db)
    assert len(cs.get("1", "ONE_DAY", db=db)) == 2 and len(cs.get("2", "ONE_DAY", db=db)) == 1 and cs.get("1", "ONE_MINUTE", db=db).empty
    assert len(cs.get("1", "ONE_DAY", dt.datetime(2026, 10, 2), db)) == 1


def test_non_positive_prices_are_never_stored(tmp_path):
    db = tmp_path / "c.db"
    assert cs.put("1", "ONE_DAY", df([("2026-10-01", 0, 0, 0, 0, 0), ("2026-10-02", 2, 3, 2, 3, 0)]), db) == 1


def test_merge_prefers_fresh_and_keeps_older_stored_bars():
    old = df([("2026-10-01", 1, 1, 1, 1, 0), ("2026-10-02", 2, 2, 2, 2, 0)])
    new = df([("2026-10-02", 2, 2.5, 2, 2.4, 0), ("2026-10-03", 3, 3, 3, 3, 0)])
    m = cs.merge(old, new)
    assert m.close.tolist() == [1.0, 2.4, 3.0] and cs.merge(old, df([])).equals(old) and cs.merge(pd.DataFrame(), new).equals(new)
