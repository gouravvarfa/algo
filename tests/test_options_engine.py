"""Backtest engine rules. The Bhavcopy rows here are SYNTHETIC fixtures in a temp DB (real runs use NSE data)."""
import datetime as dt

import pytest

from app.options import bhavcopy, engine
from app.options.strategy import DEFAULT_CHARGES, validate

EXP = "2026-03-10"
DAYS = ["2026-03-05", "2026-03-06", "2026-03-09", "2026-03-10"]


def make_db(tmp_path, prices, und=(22500, 22500, 22500, 22500), lot=65, lots_by_day=None):
    """prices: {(strike, opt): [(o,h,l,c,vol) per day]}"""
    db = tmp_path / "t.db"
    with bhavcopy._conn(db) as c:
        for i, d in enumerate(DAYS):
            c.execute("INSERT INTO load_log VALUES (?,?,?,?,?,?)", (d, "loaded", 1, 0, "", "x"))
            for (k, o), bars in prices.items():
                op, h, l, cl, v = bars[i]
                c.execute("INSERT INTO options_eod VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (d, "NIFTY", EXP, k, o, op, h, l, cl, cl, und[i], 1000, 0, v, (lots_by_day or {}).get(d, lot), "x"))
    return db


def flat(c, n=4):
    return [(c, c, c, c, 100)] * n


def strat(**kw):
    s = {"symbol": "NIFTY", "start": "2026-03-01", "end": "2026-03-31", "entry_days_before_expiry": 2, "slippage_pct": 0,
         "legs": [{"side": "SELL", "opt": "CE", "lots": 1, "strike": {"mode": "ATM", "offset": 0}}],
         "charges": {k: 0 for k in DEFAULT_CHARGES}}
    s.update(kw)
    v, errs = validate(s)
    assert not errs, errs
    return v


def base_prices():
    return {(k, o): flat(100.0) for k in (22400.0, 22500.0, 22600.0) for o in ("CE", "PE")}


def test_validator_reports_clear_errors():
    _, errs = validate({"symbol": "FINNIFTY", "start": "2026-03-10", "end": "2026-03-01", "legs": [{"side": "X", "opt": "CE", "strike": {"mode": "FOO"}}],
                        "entry_days_before_expiry": 0, "exit_days_before_expiry": 2})
    text = " | ".join(errs)
    for frag in ("symbol", "end is before start", "side", "strike.mode", "exit_days_before_expiry"):
        assert frag in text


def test_strike_selection_modes():
    rows = {(k, o): {"close": c, "volume": 10} for k, c in ((22400, 150), (22450, 120), (22500, 95), (22550, 70), (22600, 50)) for o in ("CE", "PE")}
    assert engine.select_strike(rows, "CE", 22510, {"mode": "ATM", "offset": 0}) == 22500
    assert engine.select_strike(rows, "CE", 22510, {"mode": "ATM", "offset": 2}) == 22600       # + = OTM for calls
    assert engine.select_strike(rows, "PE", 22510, {"mode": "ATM", "offset": 2}) == 22400       # + = OTM for puts
    assert engine.select_strike(rows, "CE", 22510, {"mode": "ATM", "offset": 9}) is None        # off the listed ladder
    assert engine.select_strike(rows, "CE", 22500, {"mode": "PCT_OTM", "value": 0.4}) == 22600
    assert engine.select_strike(rows, "CE", 22500, {"mode": "PREMIUM", "value": 72}) == 22550


def test_charges_follow_configured_rates():
    ch = DEFAULT_CHARGES
    sell = engine.order_charges(100, 65, False, ch)
    turn = 6500
    exp = 20 + turn * ch["exchange"] + turn * ch["sebi"] + turn * ch["stt_sell"] + (20 + turn * ch["exchange"] + turn * ch["sebi"]) * 0.18
    assert sell == pytest.approx(exp)
    assert engine.settlement_charges(50, 65, True, ch) == pytest.approx(50 * 65 * 0.00125)
    assert engine.settlement_charges(50, 65, False, ch) == 0


def test_hold_to_expiry_settles_at_intrinsic(tmp_path):
    db = make_db(tmp_path, base_prices(), und=(22500, 22500, 22500, 22580))
    r = engine.run(strat(), db)
    t = r["trades"][0]
    assert t["entry_day"] == "2026-03-06" and t["legs"][0]["strike"] == 22500
    assert t["legs"][0]["reason"] == "EXPIRY SETTLEMENT" and t["legs"][0]["exit"] == 80.0
    assert t["gross"] == pytest.approx((100 - 80) * 65)


def test_sell_leg_sl_uses_next_day_high_and_gap_open(tmp_path):
    p = base_prices()
    p[(22500.0, "CE")] = [(100, 100, 100, 100, 9), (100, 100, 100, 100, 9), (110, 140, 105, 120, 9), (0, 0, 0, 120, 0)]
    legs = [{"side": "SELL", "opt": "CE", "lots": 1, "strike": {"mode": "ATM", "offset": 0}, "sl": {"type": "pct", "value": 25}}]
    t = engine.run(strat(legs=legs), make_db(tmp_path, p))["trades"][0]
    assert t["legs"][0]["reason"] == "SL" and t["legs"][0]["exit"] == 125.0 and t["legs"][0]["exit_day"] == "2026-03-09"
    p[(22500.0, "CE")][2] = (130, 140, 105, 120, 9)                       # opened above the SL -> filled at the open
    t = engine.run(strat(legs=legs), _db2(tmp_path, p))["trades"][0]
    assert t["legs"][0]["reason"] == "SL (gap open)" and t["legs"][0]["exit"] == 130.0


def _db2(tmp_path, p):
    d = tmp_path / "second"
    d.mkdir()
    return make_db(d, p)


def test_entry_day_high_is_not_used_no_lookahead(tmp_path):
    p = base_prices()
    p[(22500.0, "CE")] = [(100, 100, 100, 100, 9), (100, 500, 90, 100, 9), (100, 101, 99, 100, 9), (100, 101, 99, 100, 9)]
    legs = [{"side": "SELL", "opt": "CE", "lots": 1, "strike": {"mode": "ATM", "offset": 0}, "sl": {"type": "pct", "value": 25}}]
    t = engine.run(strat(legs=legs), make_db(tmp_path, p))["trades"][0]
    assert t["legs"][0]["reason"] == "EXPIRY SETTLEMENT"


def test_sl_and_target_same_day_is_conservative_and_flagged(tmp_path):
    p = base_prices()
    p[(22500.0, "CE")][2] = (100, 140, 40, 100, 9)
    legs = [{"side": "SELL", "opt": "CE", "lots": 1, "strike": {"mode": "ATM", "offset": 0}, "sl": {"type": "pct", "value": 25}, "target": {"type": "pct", "value": 50}}]
    t = engine.run(strat(legs=legs), make_db(tmp_path, p))["trades"][0]
    assert t["legs"][0]["reason"] == "SL" and any("AMBIGUOUS" in f for f in t["flags"])


def test_lot_size_is_taken_from_the_entry_day(tmp_path):
    db = make_db(tmp_path, base_prices(), lots_by_day={"2026-03-06": 75})
    t = engine.run(strat(), db)["trades"][0]
    assert t["legs"][0]["lot_size"] == 75 and t["legs"][0]["qty"] == 75


def test_untraded_entry_and_unloaded_days_are_skipped_not_filled(tmp_path):
    p = base_prices()
    p[(22500.0, "CE")][1] = (0, 0, 0, 100, 0)
    r = engine.run(strat(), make_db(tmp_path, p))
    assert not r["trades"] and "not traded" in r["skipped"][0]["reason"]
    db = _db2(tmp_path, base_prices())
    with bhavcopy._conn(db) as c:
        c.execute("DELETE FROM load_log WHERE trade_date='2026-03-09'")
    r = engine.run(strat(), db)
    assert not r["trades"] and "not loaded" in r["skipped"][0]["reason"]


def test_overall_sl_exits_all_legs_at_close(tmp_path):
    p = base_prices()
    p[(22500.0, "CE")][2] = (100, 101, 99, 100, 9)
    p[(22500.0, "PE")][2] = (150, 200, 150, 200, 9)
    legs = [{"side": "SELL", "opt": o, "lots": 1, "strike": {"mode": "ATM", "offset": 0}} for o in ("CE", "PE")]
    t = engine.run(strat(legs=legs, overall_sl=5000), make_db(tmp_path, p))["trades"][0]
    assert all(l["reason"] == "OVERALL SL (at close)" and l["exit_day"] == "2026-03-09" for l in t["legs"])


def test_payoff_short_straddle():
    pos = [{"side": "SELL", "opt": o, "strike": 25000, "entry": 100, "qty": 75} for o in ("CE", "PE")]
    p = engine.payoff(pos, 25000)
    assert p["max_profit"] == 15000 and p["max_loss"] is None and p["breakevens"] == [24800.0, 25200.0] and p["net_credit"] == 15000
    ic = pos + [{"side": "BUY", "opt": "CE", "strike": 25300, "entry": 20, "qty": 75}, {"side": "BUY", "opt": "PE", "strike": 24700, "entry": 20, "qty": 75}]
    assert engine.payoff(ic, 25000)["max_loss"] == pytest.approx((160 - 300) * 75)


def test_deterministic(tmp_path):
    db = make_db(tmp_path, base_prices())
    assert engine.run(strat(), db) == engine.run(strat(), db)
