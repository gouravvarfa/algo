"""Upstox as the live engine's data source: the provider answers the calls the engine makes on AngelOneProvider, from Upstox's API (faked here: no network)."""
import asyncio
import datetime as dt

import pandas as pd
import pytest

from app.paper_trading.engine import TIMEFRAMES
from app.providers import upstox_client, upstox_instruments as inst, upstox_provider as up
from app.services import upstox_credential_store as store

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
KEY = "NSE_FO|48699"
TOKEN = "upstox-token-for-tests"

ROWS = [
    {"segment": "NSE_INDEX", "instrument_type": "INDEX", "instrument_key": "NSE_INDEX|Nifty Bank", "trading_symbol": "NIFTY BANK", "name": "Nifty Bank"},
    {"segment": "NSE_INDEX", "instrument_type": "INDEX", "instrument_key": "NSE_INDEX|Nifty IT", "trading_symbol": "NIFTY IT", "name": "Nifty IT"},
    {"segment": "NSE_EQ", "instrument_type": "EQ", "instrument_key": "NSE_EQ|INE002A01018", "trading_symbol": "RELIANCE", "name": "RELIANCE INDUSTRIES LTD"},
    {"segment": "NSE_FO", "instrument_type": "FUT", "instrument_key": "NSE_FO|61466", "trading_symbol": "BANKNIFTY FUT 23 NOV 26", "name": "BANKNIFTY", "underlying_symbol": "BANKNIFTY",
     "lot_size": 30, "expiry": 1795458599000},
    {"segment": "NSE_FO", "instrument_type": "FUT", "instrument_key": KEY, "trading_symbol": "BANKNIFTY FUT 27 OCT 26", "name": "BANKNIFTY", "underlying_symbol": "BANKNIFTY",
     "lot_size": 30, "expiry": 4102444799000},      # far in the future so the test does not expire
    {"segment": "NSE_FO", "instrument_type": "FUT", "instrument_key": "NSE_FO|1000", "trading_symbol": "RELIANCE FUT 29 DEC 26", "name": "RELIANCE", "underlying_symbol": "RELIANCE",
     "lot_size": 500, "expiry": 4102444799000},
]


@pytest.fixture(autouse=True)
def login(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CREDENTIAL_PATH", tmp_path / "upstox_credentials.json")
    monkeypatch.setattr(inst, "CACHE_PATH", tmp_path / "instruments.json")
    monkeypatch.setattr(inst, "_mem", None)
    now = dt.datetime.now(IST)
    store.save(store.UpstoxCredentials(api_key="k" * 12, api_secret="s" * 8, redirect_uri="http://x/y", access_token=TOKEN,
                                       token_expires_at=(now + dt.timedelta(hours=5)).isoformat(), user_id="U", user_name="T", connected_at=now.isoformat()))


class Fake:
    def __init__(self):
        self.hist_calls, self.today_calls, self.hist, self.today, self.fail = [], [], [], [], None


@pytest.fixture
def fake(monkeypatch):
    f = Fake()

    async def get_candles(token, key, unit, interval, to_date, from_date):
        f.hist_calls.append((token, key, unit, interval, to_date, from_date))
        if f.fail:
            raise f.fail
        return list(f.hist)

    async def get_intraday_candles(token, key, unit, interval):
        f.today_calls.append((token, key, unit, interval))
        if f.fail:
            raise f.fail
        return list(f.today)

    monkeypatch.setattr(upstox_client, "get_candles", get_candles)
    monkeypatch.setattr(upstox_client, "get_intraday_candles", get_intraday_candles)

    async def load():
        return inst.normalise(ROWS, dt.date(2026, 10, 27))

    monkeypatch.setattr(inst, "load", load)
    return f


def run(coro):
    return asyncio.run(coro)


# ---- candles -> the frame the engine wants -------------------------------------------------------------------------------------------------
def test_candles_become_an_oldest_first_frame_with_naive_indian_times():
    df = up.frame([["2026-10-27T09:16:00+05:30", 2, 3, 1, 2.5, 10, 0], ["2026-10-27T09:15:00+05:30", 1, 2, 1, 1.5, 0, 0]])
    assert list(df.columns) == ["open", "high", "low", "close", "volume"] and list(df.index) == [pd.Timestamp("2026-10-27 09:15"), pd.Timestamp("2026-10-27 09:16")]
    assert df.index.tz is None and df.iloc[1].tolist() == [2.0, 3.0, 1.0, 2.5, 10.0] and df.index.name == "date"


def test_a_later_list_wins_for_a_bar_in_both_and_an_odd_row_is_skipped():
    a = [["2026-10-27T09:15:00+05:30", 1, 1, 1, 1, 1, 0], ["junk", 1, 2, 3, 4, 5, 0], ["2026-10-27T09:30:00+05:30"], ["2026-10-27T09:45:00+05:30", None, 1, 1, 1, 1, 0]]
    b = [["2026-10-27T09:15:00+05:30", 9, 9, 9, 9, 9, 0]]
    df = up.frame(a, b)
    assert len(df) == 1 and df.iloc[0].tolist() == [9.0, 9.0, 9.0, 9.0, 9.0]
    assert up.frame([]).empty and list(up.frame([]).columns) == ["open", "high", "low", "close", "volume"]


def test_every_timeframe_the_engine_uses_is_known():
    for name, (angel_interval, _minutes, _days) in TIMEFRAMES.items():
        assert angel_interval in up.INTERVALS, name


def test_futures_get_the_name_shape_the_app_already_reads():
    assert up.angel_style_symbol("BANKNIFTY", dt.date(2026, 10, 27)) == "BANKNIFTY27OCT26FUT"
    assert up.angel_style_symbol("RELIANCE", dt.date(2026, 12, 29))[-8:-3] == "DEC26"        # the roll label slices these characters


def test_the_request_goes_to_upstox_with_the_right_unit_and_the_saved_login(fake):
    fake.today = [["2099-01-01T09:15:00+05:30", 1, 2, 1, 2, 3, 0]]
    run(up.UpstoxProvider().get_intraday_ohlc("NFO", KEY, "FIFTEEN_MINUTE", 5, include_partial=True))
    assert fake.hist_calls[0][:4] == (TOKEN, KEY, "minutes", "15") and fake.today_calls == [(TOKEN, KEY, "minutes", "15")]
    fake.hist_calls.clear()
    run(up.UpstoxProvider().get_intraday_ohlc("NFO", KEY, "ONE_HOUR", 5, include_partial=True))
    assert fake.hist_calls[0][2:4] == ("hours", "1")
    fake.hist_calls.clear()
    run(up.UpstoxProvider().get_intraday_ohlc("NFO", KEY, "ONE_DAY", 5, include_partial=True))
    assert fake.hist_calls[0][2:4] == ("days", "1")


@pytest.mark.parametrize("interval,days,calls", [("ONE_MINUTE", 12, 1), ("ONE_MINUTE", 60, 3), ("FIFTEEN_MINUTE", 180, 7), ("THIRTY_MINUTE", 100, 2), ("ONE_HOUR", 380, 5), ("ONE_DAY", 1900, 1)])
def test_a_long_window_is_asked_for_in_pieces_upstox_accepts(fake, interval, days, calls):
    run(up.UpstoxProvider().get_intraday_ohlc("NFO", KEY, interval, days))
    assert len(fake.hist_calls) == calls
    spans = [(dt.date.fromisoformat(c[5]), dt.date.fromisoformat(c[4])) for c in fake.hist_calls]       # (from, to) per request, newest first
    longest = up.INTERVALS[interval][2]
    assert all((b - a).days + 1 <= longest for a, b in spans)
    for (a_from, _), (_, b_to) in zip(spans[1:], spans[:-1]):                                       # no gap and no overlap between neighbours
        assert a_from <= b_to
    for older, newer in zip(spans[1:], spans[:-1]):
        assert (newer[0] - older[1]).days == 1
    assert spans[0][1] == dt.datetime.now(IST).date() and spans[-1][0] == dt.datetime.now(IST).date() - dt.timedelta(days=days)


def test_old_and_todays_candles_are_joined_in_one_frame(fake):
    fake.hist = [["2026-10-26T15:15:00+05:30", 1, 2, 1, 2, 1, 0], ["2026-10-26T09:15:00+05:30", 5, 6, 4, 5, 1, 0]]
    fake.today = [["2026-10-27T09:15:00+05:30", 7, 8, 6, 7, 2, 0]]
    df = run(up.UpstoxProvider().get_intraday_ohlc("NFO", KEY, "FIFTEEN_MINUTE", 3, include_partial=True))
    assert list(df.index) == [pd.Timestamp("2026-10-26 09:15"), pd.Timestamp("2026-10-26 15:15"), pd.Timestamp("2026-10-27 09:15")]


def test_the_forming_candle_is_dropped_unless_asked_for(fake):
    now = dt.datetime.now(IST).replace(tzinfo=None)
    forming = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
    closed = forming - dt.timedelta(minutes=15)
    fake.today = [[forming.replace(tzinfo=IST).isoformat(), 2, 3, 1, 2, 1, 0], [closed.replace(tzinfo=IST).isoformat(), 1, 2, 1, 1.5, 1, 0]]
    only_closed = run(up.UpstoxProvider().get_intraday_ohlc("NFO", KEY, "FIFTEEN_MINUTE", 1))
    with_forming = run(up.UpstoxProvider().get_intraday_ohlc("NFO", KEY, "FIFTEEN_MINUTE", 1, include_partial=True))
    assert pd.Timestamp(closed) in only_closed.index and pd.Timestamp(forming) not in only_closed.index
    assert pd.Timestamp(forming) in with_forming.index


def test_today_s_daily_bar_is_dropped_until_the_session_has_ended(fake):
    today = dt.datetime.now(IST)
    fake.today = [[today.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(), 1, 2, 1, 2, 1, 0]]
    df = run(up.UpstoxProvider().get_intraday_ohlc("NSE", "NSE_INDEX|Nifty Bank", "ONE_DAY", 5))
    ended = today.time() >= dt.time(15, 30)
    assert (len(df) == 1) == ended


def test_an_unsupported_interval_is_refused(fake):
    with pytest.raises(ValueError):
        run(up.UpstoxProvider().get_intraday_ohlc("NFO", KEY, "TWO_MINUTE", 3))


# ---- the login ----------------------------------------------------------------------------------------------------------------------------
def test_without_a_login_the_message_says_what_to_do_and_upstox_is_not_asked(fake):
    store.clear()
    with pytest.raises(up.UpstoxNotConnected, match="Connect with Upstox"):
        run(up.UpstoxProvider().get_intraday_ohlc("NFO", KEY, "ONE_DAY", 5))
    assert fake.hist_calls == []


def test_an_ended_login_is_named_as_such():
    c = store.get()
    c.token_expires_at = (dt.datetime.now(IST) - dt.timedelta(minutes=1)).isoformat()
    store.save(c)
    with pytest.raises(up.UpstoxNotConnected, match="03:30"):
        up.UpstoxProvider().access_token()


def test_when_upstox_ends_the_login_early_it_is_forgotten(fake):
    fake.fail = upstox_client.UpstoxApiError("Invalid token used to access API", "auth")
    with pytest.raises(up.UpstoxNotConnected):
        run(up.UpstoxProvider().get_intraday_ohlc("NFO", KEY, "ONE_DAY", 5))
    assert store.get().access_token is None


def test_other_upstox_trouble_passes_through_and_keeps_the_login(fake):
    fake.fail = upstox_client.UpstoxApiError("Invalid date range", "data")
    with pytest.raises(upstox_client.UpstoxApiError):
        run(up.UpstoxProvider().get_intraday_ohlc("NFO", KEY, "ONE_DAY", 5))
    assert store.get().access_token == TOKEN


# ---- symbols ------------------------------------------------------------------------------------------------------------------------------
def test_the_two_indexes_resolve_to_their_upstox_keys_and_others_by_name(fake):
    p = up.UpstoxProvider()
    assert run(p.resolve_index("BANKNIFTY")).token == "NSE_INDEX|Nifty Bank" and run(p.resolve_index("nifty")).token == "NSE_INDEX|Nifty 50"
    it = run(p.resolve_index("NIFTY IT"))
    assert it.token == "NSE_INDEX|Nifty IT" and it.exch_seg == "NSE"
    assert run(p.resolve_index("NOSUCH")) is None


def test_a_stock_resolves_to_its_cash_listing(fake):
    m = run(up.UpstoxProvider().resolve_equity("reliance"))
    assert (m.token, m.trading_symbol, m.exch_seg) == ("NSE_EQ|INE002A01018", "RELIANCE", "NSE")
    assert run(up.UpstoxProvider().resolve_equity("NOSUCH")) is None


def test_futures_contracts_are_listed_nearest_first_in_the_shape_the_engine_reads(fake):
    cs = run(up.UpstoxProvider().future_contracts("BANKNIFTY"))
    assert [c.token for c in cs] == ["NSE_FO|61466", KEY]                       # 23 Nov 26 before the far-future one
    c = cs[0]
    assert (c.underlying, c.exch_seg, c.lot_size, c.is_index) == ("BANKNIFTY", "NFO", 30, True)
    assert c.trading_symbol == "BANKNIFTY23NOV26FUT" and c.expiry == "2026-11-23"
    assert [x.is_index for x in run(up.UpstoxProvider().future_contracts("RELIANCE"))] == [False]
    assert run(up.UpstoxProvider().future_contracts("NOSUCH")) == []


def test_the_engine_reads_index_names_from_the_source():
    assert up.UpstoxProvider().index_tokens == {"BANKNIFTY": ("NSE", "NSE_INDEX|Nifty Bank"), "NIFTY": ("NSE", "NSE_INDEX|Nifty 50")}
