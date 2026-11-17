"""Multi-contract chain: roll at the cutoff, fresh Nov entry with Nov's own rules, waiting state carried over. Candles are SYNTHETIC fixtures."""
import asyncio
import datetime as dt

import pandas as pd

from app.live import chain_eval as ce
from app.strategy.top_bottom.intraday import Minute

D = dt.date
OCT = ce.ContractRef("BANKNIFTYOCT", "1", "NFO", D(2026, 10, 27), 30)
NOV = ce.ContractRef("BANKNIFTYNOV", "2", "NFO", D(2026, 11, 23), 30)
DEC = ce.ContractRef("BANKNIFTYDEC", "3", "NFO", D(2026, 12, 29), 30)
CLOSES = [100, 110, 100, 90, 100, 108, 109, 109, 109, 109]       # Top 110 and Bottom 90 confirmed (Nov: +30)


def daily(shift):
    days = list(pd.bdate_range("2026-10-01", periods=len(CLOSES)))
    cl = [c + shift for c in CLOSES]
    return pd.DataFrame({"open": cl, "high": [c + 1 for c in cl], "low": [c - 1 for c in cl], "close": cl, "volume": 0.0}, index=days)


def minutes(day, shift, prices, start=(9, 15)):
    out, t = [], dt.datetime(2026, 10, day, *start)
    for p in prices:
        out.append(Minute(t, p + shift, p + shift, p + shift, p + shift))
        t += dt.timedelta(minutes=1)
    return out


def flat(day, shift, price, n=40):
    return minutes(day, shift, [price] * n)


def run(contracts, dailies, mins, now=dt.datetime(2026, 10, 21, 11, 0)):
    async def gd(c):
        return dailies[c.token]

    async def gm(c):
        return mins[c.token]

    return asyncio.run(ce.evaluate(contracts=contracts, from_dt=dt.datetime(2026, 10, 1), now=now, symbol="BANKNIFTY", get_daily=gd, get_minutes=gm,
                                   daily_engine=lambda c, df: [], reconstruct=lambda r, d: (None, []), entry_day_stop_pct=0.01, struct_cap_distance=0.02))


def oct_with_open_buy():
    """12 Oct: price trades up through the Top (110) -> BUY at 110, then stays at 111 until the cutoff (19 Oct); 19-21 Oct at 112."""
    day12 = [Minute(dt.datetime(2026, 10, 12, 9, 15), 109.0, 109.0, 109.0, 109.0), Minute(dt.datetime(2026, 10, 12, 9, 16), 109.95, 111.0, 109.95, 110.5)] \
        + minutes(12, 0, [111.0] * 40, (9, 17))
    return day12 + flat(13, 0, 111.0) + flat(14, 0, 111.0) + flat(15, 0, 111.0) + flat(16, 0, 111.0) + flat(19, 0, 112.0) + flat(20, 0, 112.0) + flat(21, 0, 112.0)


def test_plan_segments():
    segs = ce.plan([NOV, OCT, DEC], D(2026, 1, 1), D(2026, 10, 21))
    assert [(s.contract.trading_symbol, s.start, s.end) for s in segs] == [("BANKNIFTYOCT", D(2026, 1, 1), D(2026, 10, 19)), ("BANKNIFTYNOV", D(2026, 10, 19), None)]
    assert len(ce.plan([OCT, NOV, DEC], D(2026, 1, 1), D(2026, 10, 18))) == 1                                   # before the first cutoff: a single contract
    late = ce.plan([OCT, NOV, DEC], D(2026, 1, 1), D(2026, 11, 16))
    assert late[-1].contract.trading_symbol == "BANKNIFTYDEC" and late[1].end == D(2026, 11, 16)


def test_open_position_is_closed_in_the_old_contract_and_continued_in_the_next_with_its_own_rules():
    r = run([OCT, NOV], {"1": daily(0), "2": daily(30)}, {"1": oct_with_open_buy(), "2": flat(19, 30, 112.0) + flat(20, 30, 112.0) + flat(21, 30, 112.0)})
    t_old, t_new = r.trades
    assert (t_old.contract, t_old.direction, t_old.exit_reason) == ("BANKNIFTYOCT", "BUY", ce.ROLLED)
    assert t_old.entry_price == 110.0 and t_old.exit_price == 112.0 and t_old.exit_date == dt.datetime(2026, 10, 19, 9, 15)      # old leg priced in OCT
    assert (t_new.contract, t_new.direction, t_new.rolled_in) == ("BANKNIFTYNOV", "BUY", True)
    assert t_new.entry_price == 142.0 and t_new.entry_date == dt.datetime(2026, 10, 19, 9, 15)                                   # Nov's own price
    assert t_new.initial_sl == 120.0 and t_new.entry_day_sl == round(142 * 0.99, 2)                                              # Nov's OWN Bottom (90 + 30), 1% entry-day stop
    assert r.rolls[0]["old_price"] == 112.0 and r.rolls[0]["new_price"] == 142.0 and r.active.trading_symbol == "BANKNIFTYNOV"


def test_close_structural_stop_in_the_new_contract_reverses_inside_the_new_contract_only():
    closes = [120, 140, 150, 140.5, 145, 146, 146, 146, 146, 146]                                                                 # Nov: Top 150, Bottom 140.5 (1.1% under a 142 entry)
    days = list(pd.bdate_range("2026-10-01", periods=10))
    nov_daily = pd.DataFrame({"open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes], "close": closes, "volume": 0.0}, index=days)
    nov = flat(19, 30, 112.0) + minutes(20, 30, [112.0, 111.0, 109.5, 108.0] + [108.0] * 40) + flat(21, 30, 108.0)                 # 142 -> 139.5 on day 2: through 140.5
    r = run([OCT, NOV], {"1": daily(0), "2": nov_daily}, {"1": oct_with_open_buy(), "2": nov})
    assert [(t.contract, t.direction) for t in r.trades][:3] == [("BANKNIFTYOCT", "BUY"), ("BANKNIFTYNOV", "BUY"), ("BANKNIFTYNOV", "SELL")]
    assert r.trades[1].exit_reason == "STOP_LOSS_REVERSAL" and r.trades[1].exit_price == 139.5 and r.trades[2].entry_price == 139.5      # the minute opened below the stop: filled at its open
    assert all(t.contract == "BANKNIFTYNOV" for t in r.trades[1:])


def test_waiting_state_carries_over_and_old_points_do_not_re_enter():
    oct_m = [Minute(dt.datetime(2026, 10, 12, 9, 15), 109.2, 109.2, 109.2, 109.2), Minute(dt.datetime(2026, 10, 12, 9, 16), 109.95, 111.0, 109.95, 110.5)] \
        + minutes(12, 0, [107.0] * 40, (9, 17)) + flat(13, 0, 105.0) + flat(14, 0, 105.0) + flat(15, 0, 105.0) + flat(16, 0, 105.0) + flat(19, 0, 105.0) + flat(20, 0, 105.0) + flat(21, 0, 105.0)
    nov_m = minutes(19, 30, [141.0, 141.0, 142.0] + [141.0] * 40) + flat(20, 30, 105.0) + flat(21, 30, 105.0)                      # crosses the OLD Nov Top (140)
    r = run([OCT, NOV], {"1": daily(0), "2": daily(30)}, {"1": oct_m, "2": nov_m})
    assert [t.exit_reason for t in r.trades if t.contract == "BANKNIFTYOCT"] == ["ENTRY_DAY_STOP_EXIT"]
    assert not [t for t in r.trades if t.contract == "BANKNIFTYNOV"]                                                              # that Top existed at the exit: consumed


def test_chain_waits_until_the_next_contract_has_minutes_on_its_cutoff_day():
    assert run([OCT, NOV], {"1": daily(0), "2": daily(30)}, {"1": oct_with_open_buy(), "2": []}) is None


def test_single_contract_before_the_first_cutoff_returns_none():
    assert run([OCT, NOV], {"1": daily(0), "2": daily(30)}, {"1": oct_with_open_buy(), "2": []}, now=dt.datetime(2026, 10, 16, 11, 0)) is None


def test_flat_old_contract_enters_the_new_one_only_on_a_genuine_crossing():
    oct_m = flat(12, 0, 105.0) + flat(13, 0, 105.0) + flat(14, 0, 105.0) + flat(15, 0, 105.0) + flat(16, 0, 105.0) + flat(19, 0, 105.0) + flat(20, 0, 105.0) + flat(21, 0, 105.0)
    nov_m = flat(19, 30, 105.0) + [Minute(dt.datetime(2026, 10, 20, 9, 15), 139.0, 139.0, 139.0, 139.0), Minute(dt.datetime(2026, 10, 20, 9, 16), 139.9, 141.0, 139.9, 140.5)] \
        + minutes(20, 30, [110.5] * 40, (9, 17)) + flat(21, 30, 110.5)
    r = run([OCT, NOV], {"1": daily(0), "2": daily(30)}, {"1": oct_m, "2": nov_m})
    nov_trades = [t for t in r.trades if t.contract == "BANKNIFTYNOV"]
    assert nov_trades and nov_trades[0].direction == "BUY" and not nov_trades[0].rolled_in and nov_trades[0].entry_price == 140.0


# ---------------------------------------------------------------- hold rule (owner, 17 Nov 2026)
import pytest  # noqa: E402

from app.live import contracts as ct  # noqa: E402


@pytest.fixture
def hold(monkeypatch):
    monkeypatch.setattr(ct, "HOLD_ROLL_FROM", D(2026, 1, 1))       # the synthetic Oct contract (cutoff 19 Oct, expiry 27 Oct) follows the hold rule


def run_h(mins, now, cap=None):
    async def gd(c):
        return {"1": daily(0), "2": daily(30)}[c.token]

    async def gm(c):
        return mins[c.token]

    return asyncio.run(ce.evaluate(contracts=[OCT, NOV], from_dt=dt.datetime(2026, 10, 1), now=now, symbol="BANKNIFTY", get_daily=gd, get_minutes=gm,
                                   daily_engine=lambda c, df: [], reconstruct=lambda r, d: (None, []), entry_day_stop_pct=0.01, struct_cap_distance=cap))


def days(shift, price, ds):
    out = []
    for d in ds:
        out += flat(d, shift, price)
    return out


def test_hold_limit_dates():
    assert ct.hold_limit(D(2026, 12, 29), "NFO", "BANKNIFTY") == D(2026, 12, 29)                 # NFO: the expiry day itself
    assert ct.hold_limit(D(2026, 11, 30), "MCX", "GOLDTEN") == D(2026, 11, 25)                   # tender 26, 27, 30 Nov: held until the 25th's open
    assert ct.hold_limit(D(2026, 11, 30), "MCX", "SILVERMIC") == D(2026, 11, 25)
    assert ct.hold_limit(D(2026, 11, 30), "MCX", "CRUDEOIL") == D(2026, 11, 23)                  # unknown MCX symbol: 5-day window -> the cutoff itself
    assert ct.hold_limit(D(2026, 12, 31), "MCX", "GOLDTEN") == D(2026, 12, 28)
    assert ct.hold_rule(D(2026, 11, 23)) is False and ct.hold_rule(D(2026, 11, 30)) is True      # NFO Nov (cutoff 16 Nov) stays as it rolled


def test_open_position_is_held_past_the_cutoff_in_its_own_contract(hold):
    nov = days(30, 112.0, (19, 20, 21))
    r = run_h({"1": oct_with_open_buy(), "2": nov}, dt.datetime(2026, 10, 21, 11, 0))
    assert [(t.contract, t.direction, t.exit_reason) for t in r.trades] == [("BANKNIFTYOCT", "BUY", "END_OF_BACKTEST")]
    assert r.active.trading_symbol == "BANKNIFTYOCT" and r.rolls == [] and r.segments[-1].end is None
    assert any("HELD PAST THE CUTOFF" in e[1] for e in r.trades[0].events)


def test_a_system_point_in_the_window_closes_the_old_contract_and_reverses_into_the_next(hold):
    oct_m = oct_with_open_buy()
    oct_m = [m for m in oct_m if m.ts.date() != D(2026, 10, 20)] + minutes(20, 0, [112.0] * 5 + [89.0] * 40)
    oct_m.sort(key=lambda m: m.ts)
    nov = flat(19, 30, 112.0) + minutes(20, 30, [112.0] * 5 + [89.0] * 40) + flat(21, 30, 89.0)
    r = run_h({"1": oct_m, "2": nov}, dt.datetime(2026, 10, 21, 11, 0))
    old, new = r.trades[0], r.trades[1]
    assert (old.contract, old.direction, old.exit_reason, old.exit_date) == ("BANKNIFTYOCT", "BUY", "STOP_LOSS_REVERSAL", dt.datetime(2026, 10, 20, 9, 20))
    assert (new.contract, new.direction, new.rolled_in, new.entry_date) == ("BANKNIFTYNOV", "SELL", False, dt.datetime(2026, 10, 20, 9, 20))
    assert new.entry_price == round(old.exit_price + 30, 2) and new.entry_day_sl == round(new.entry_price * 1.01, 2)     # Nov's price at that minute, 1% stop
    assert new.initial_sl == 140.0                                                                                          # Nov's own Top
    assert not [t for t in r.trades if t.contract == "BANKNIFTYOCT" and t.entry_date.date() >= D(2026, 10, 19)]           # nothing new in Oct after its cutoff
    assert r.rolls[-1]["kind"] == "REVERSE" and r.active.trading_symbol == "BANKNIFTYNOV"


def test_no_system_point_until_the_hold_limit_rolls_at_that_open(hold):
    ds = (19, 20, 21, 22, 23, 26, 27)
    oct_m = [m for m in oct_with_open_buy() if m.ts.date() < D(2026, 10, 19)] + days(0, 112.0, ds)
    r = run_h({"1": oct_m, "2": days(30, 112.0, ds)}, dt.datetime(2026, 10, 27, 11, 0))
    old, new = r.trades[0], r.trades[1]
    assert (old.exit_reason, old.exit_date, old.exit_price) == (ce.ROLLED, dt.datetime(2026, 10, 27, 9, 15), 112.0)
    assert (new.contract, new.direction, new.rolled_in, new.entry_price) == ("BANKNIFTYNOV", "BUY", True, 142.0)
    assert new.entry_day_sl == round(142 * 0.99, 2) and r.rolls[-1]["kind"] == "ROLL"


def test_before_the_hold_limit_opens_the_position_is_still_held(hold):
    ds = (19, 20, 21, 22, 23, 26)
    oct_m = [m for m in oct_with_open_buy() if m.ts.date() < D(2026, 10, 19)] + days(0, 112.0, ds)
    r = run_h({"1": oct_m, "2": days(30, 112.0, ds)}, dt.datetime(2026, 10, 27, 8, 0))
    assert r.trades[-1].exit_reason == "END_OF_BACKTEST" and r.active.trading_symbol == "BANKNIFTYOCT"


def test_a_one_percent_stop_in_the_window_leaves_the_strategy_flat_with_no_entry_that_day(hold):
    oct_m = [m for m in oct_with_open_buy() if m.ts.date() < D(2026, 10, 19)] + flat(19, 0, 112.0) + minutes(20, 0, [112.0] * 5 + [108.0] * 40) + flat(21, 0, 108.0)
    nov = flat(19, 30, 112.0) + minutes(20, 30, [112.0] * 5 + [108.0] * 3 + [141.0] * 37) + flat(21, 30, 108.0)          # Nov crosses its Top 140 the same day
    r = run_h({"1": oct_m, "2": nov}, dt.datetime(2026, 10, 21, 11, 0), cap=0.02)
    assert r.trades[0].exit_reason == "CAPPED_1_PERCENT_STOP_EXIT"
    assert not [t for t in r.trades if t.contract == "BANKNIFTYNOV" and t.entry_date.date() == D(2026, 10, 20)]
    assert r.active.trading_symbol == "BANKNIFTYNOV"


def test_flat_at_the_cutoff_the_next_contract_starts_that_day_as_before(hold):
    test_flat_old_contract_enters_the_new_one_only_on_a_genuine_crossing()
