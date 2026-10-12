from __future__ import annotations

import datetime as dt

import pandas as pd


def drop_incomplete_trailing_bars(df: pd.DataFrame, interval_minutes: int, now: dt.datetime) -> pd.DataFrame:
    """
    Keeps only bars whose full interval has already elapsed as of `now`
    (bar index = bar START time, matching Angel One's candle format).
    This is what makes "never use an incomplete candle" true regardless of
    what moment during the session the scan runs.
    """
    if df.empty:
        return df
    closed_mask = df.index + pd.Timedelta(minutes=interval_minutes) <= now
    return df[closed_mask]


def drop_incomplete_trailing_daily_bar(
    df: pd.DataFrame, now: dt.datetime, market_close_time: dt.time = dt.time(15, 30)
) -> pd.DataFrame:
    """
    Daily bars need a different rule than the minute-elapsed one above: a
    day "closes" only at market close (15:30 IST), not some fixed number of
    elapsed minutes from midnight (the exchange isn't open 24h). Drops
    today's bar, if present, when today's session hasn't ended yet.
    """
    if df.empty:
        return df
    last_date = df.index[-1].date()
    if last_date == now.date() and now.time() < market_close_time:
        return df.iloc[:-1]
    return df
