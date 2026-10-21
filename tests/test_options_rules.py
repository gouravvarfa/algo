"""P2 strike modes / per-leg expiry and P3 risk rules. Bhavcopy rows are SYNTHETIC fixtures in a temp DB."""
import pytest

from app.options import bhavcopy, bs, engine
from app.options.strategy import DEFAULT_CHARGES, validate

DAYS = ["2026-03-05", "2026-03-06", "2026-03-09", "2026-03-10"]
E1, E2, E3 = "2026-03-10", "2026-03-17", "2026-03-31"          # cycle (weekly), next weekly, monthly
K = (22400.0, 22500.0, 22600.0)


def flat(c, v=100):
    return [(c, c, c, c, v)] * 4


def make_db(tmp_path, bars=None, und=(22500,) * 4, expiries=(E1, E2, E3)):
    """bars: {(expiry, strike, opt): [(o,h,l,c,vol) x 4 days]}; anything not given is flat 100 (traded)."""
    bars = bars or {}
    db = tmp_path / f"r{len(list(tmp_path.glob('*.db')))}.db"
    with bhavcopy._conn(db) as c:
        for i, d in enumerate(DAYS):
            c.execute("INSERT INTO load_log VALUES (?,?,?,?,?,?)", (d, "loaded", 1, 0, "", "x"))
            for e in expiries:
                for k in K:
                    for o in ("CE", "PE"):
                        op, h, l, cl, v = bars.get((e, k, o), flat(100.0 if e == E1 else 150.0))[i]
                        c.execute("INSERT INTO options_eod VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (d, "NIFTY", e, k, o, op, h, l, cl, cl, und[i], 1000, 0, v, 65, "x"))
    return db


def strat(legs, **kw):
    s = {"symbol": "NIFTY", "start": "2026-03-01", "end": "2026-03-12", "entry_days_before_expiry": 3, "slippage_pct": 0,
         "legs": legs, "charges": {k: 0 for k in DEFAULT_CHARGES}}
    s.update(kw)
    v, errs = validate(s)
    assert not errs, errs
    return v


def L(side="SELL", opt="CE", **kw):
    d = {"side": side, "opt": opt, "lots": 1, "strike": {"mode": "ATM", "offset": 0}}
    d.update(kw)
    return d


def one(db, s):
    r = engine.run(s, db)
    assert r["trades"], r["skipped"]
    return r["trades"][0]


# ================================================================ P2: strike modes
ROWS = {(k, o): {"close": c, "volume": 10} for k, c in ((22400, 150), (22450, 120), (22500, 95), (22550, 70), (22600, 50)) for o in ("CE", "PE")}


def test_premium_range_gte_lte_pick_deterministically():
    sel = lambda spec: engine.select_strike(ROWS, "CE", 22500, spec)
    assert sel({"mode": "PREMIUM_RANGE", "low": 60, "high": 100}) == 22550       # 70 and 95 inside; nearest the middle (80)
    assert sel({"mode": "PREMIUM_RANGE", "low": 200, "high": 300}) is None       # nothing in range: no approximation
    assert sel({"mode": "PREMIUM_GTE", "value": 96}) == 22450                    # smallest close >= 96 is 120
    assert sel({"mode": "PREMIUM_LTE", "value": 94}) == 22550                    # largest close <= 94 is 70
    assert sel({"mode": "PREMIUM_GTE", "value": 500}) is None


def test_untraded_strikes_are_never_selected_by_premium_rules():
    rows = dict(ROWS)
    rows[(22550, "CE")] = {"close": 70, "volume": 0}
    assert engine.select_strike(rows, "CE", 22500, {"mode": "PREMIUM_LTE", "value": 94}) == 22600


def test_straddle_width_uses_the_real_atm_straddle_and_needs_it_traded():
    rows = {(k, o): {"close": 100.0, "volume": 10} for k in (22300, 22400, 22500, 22600, 22700) for o in ("CE", "PE")}
    assert engine.select_strike(rows, "CE", 22500, {"mode": "STRADDLE_WIDTH", "value": 0.5}) == 22600      # 22500 + 0.5*200
    assert engine.select_strike(rows, "PE", 22500, {"mode": "STRADDLE_WIDTH", "value": 0.5}) == 22400
    assert engine.select_strike(rows, "CE", 22500, {"mode": "STRADDLE_WIDTH", "value": -1}) == 22300       # ITM side
    rows[(22500, "PE")] = {"close": 100.0, "volume": 0}
    assert engine.select_strike(rows, "CE", 22500, {"mode": "STRADDLE_WIDTH", "value": 0.5}) is None


def test_closest_delta_uses_black_scholes_from_each_strikes_own_close():
    t = 5.25 / 365
    rows = {}
    for k in (22300, 22400, 22500, 22600, 22700):
        rows[(k, "CE")] = {"close": round(bs.price(22500, k, t, 0.065, 0, 0.14, True), 2), "volume": 10}
    deltas = {k: abs(bs.greeks(22500, k, t, 0.065, 0, 0.14, True)["delta"]) * 100 for k in (22300, 22400, 22500, 22600, 22700)}
    want = min(deltas, key=lambda k: abs(deltas[k] - 25))
    assert engine.select_strike(rows, "CE", 22500, {"mode": "DELTA", "value": 25}, t) == want
    assert engine.select_strike(rows, "CE", 22500, {"mode": "DELTA", "value": 25}, None) is None


def test_leg_expiry_rules_use_only_listed_expiries():
    listed = ["2026-03-10", "2026-03-17", "2026-03-24", "2026-03-31", "2026-04-28"]
    assert engine.leg_expiry("cycle", "2026-03-10", listed) == "2026-03-10"
    assert engine.leg_expiry("next_weekly", "2026-03-10", listed) == "2026-03-17"
    assert engine.leg_expiry("monthly", "2026-03-10", listed) == "2026-03-31"
    assert engine.leg_expiry("next_monthly", "2026-03-10", listed) == "2026-04-28"
    assert engine.leg_expiry("next_monthly", "2026-12-29", ["2026-12-29"]) is None
    assert engine.leg_expiry("next_weekly", "2026-03-31", ["2026-03-31"]) is None


def test_next_weekly_leg_exits_at_close_while_cycle_leg_settles(tmp_path):
    db = make_db(tmp_path, und=(22500, 22500, 22500, 22560))
    t = one(db, strat([L(), L(opt="PE", expiry="next_weekly")]))
    ce, pe = t["legs"]
    assert ce["expiry"] == E1 and ce["reason"] == "EXPIRY SETTLEMENT" and ce["exit"] == 60.0
    assert pe["expiry"] == E2 and pe["reason"] == "EXIT TIME" and pe["exit"] == 150.0


def test_unavailable_leg_expiry_skips_the_cycle_with_a_reason(tmp_path):
    db = make_db(tmp_path, expiries=(E1,))
    r = engine.run(strat([L(expiry="next_weekly")]), db)
    assert not r["trades"] and "no listed expiry" in r["skipped"][0]["reason"]


def test_validator_rejects_bad_new_modes():
    _, e = validate({"start": "2026-03-01", "end": "2026-03-31", "legs": [
        {"side": "SELL", "opt": "CE", "strike": {"mode": "PREMIUM_RANGE", "low": 50, "high": 40}},
        {"side": "SELL", "opt": "CE", "strike": {"mode": "STRADDLE_WIDTH", "value": 0}},
        {"side": "SELL", "opt": "CE", "strike": {"mode": "DELTA", "value": 120}},
        {"side": "SELL", "opt": "CE", "expiry": "quarterly"}]})
    txt = " | ".join(e)
    assert "PREMIUM_RANGE" in txt and "straddle multiplier" in txt and "delta 1-99" in txt and "legs[3].expiry" in txt


# ================================================================ P3: risk rules
def test_reentry_after_sl_at_that_days_close_and_checked_from_next_day(tmp_path):
    bars = {(E1, 22500.0, "CE"): [(100, 100, 100, 100, 9), (100, 130, 95, 110, 9), (110, 112, 105, 110, 9), (110, 112, 105, 110, 9)]}
    t = one(make_db(tmp_path, bars), strat([L(sl={"type": "pct", "value": 25}, reentry={"max": 2})]))
    first, re1 = t["legs"]
    assert first["reason"] == "SL" and first["exit"] == 125.0 and first["exit_day"] == "2026-03-06"
    assert re1["n"] == "1.R1" and re1["entry_day"] == "2026-03-06" and re1["entry"] == 110.0 and re1["sl"] == 137.5
    assert re1["reason"] == "EXPIRY SETTLEMENT" and t["reentries"] == 1


def test_reentry_respects_max_count_and_never_happens_on_exit_day(tmp_path):
    bars = {(E1, 22500.0, "CE"): [(100, 100, 100, 100, 9), (100, 130, 95, 100, 9), (100, 130, 95, 100, 9), (100, 130, 95, 100, 9)]}
    t = one(make_db(tmp_path, bars), strat([L(sl={"type": "pct", "value": 25}, reentry={"max": 5})]))
    assert [l["n"] for l in t["legs"]] == [1, "1.R1", "1.R2"]        # SL on 06, 09 (re-enter both), SL on 10 = exit day: no re-entry
    assert all(l["reason"] == "SL" for l in t["legs"]) and t["reentries"] == 2
    t = one(make_db(tmp_path, bars), strat([L(sl={"type": "pct", "value": 25}, reentry={"max": 1})]))
    assert [l["n"] for l in t["legs"]] == [1, "1.R1"]


def test_sl_to_cost_moves_other_legs_from_the_next_day_only(tmp_path):
    bars = {(E1, 22500.0, "CE"): [(100, 100, 100, 100, 9), (100, 130, 95, 120, 9), (120, 120, 120, 120, 9), (120, 120, 120, 120, 9)],
            (E1, 22500.0, "PE"): [(100, 100, 100, 100, 9), (100, 104, 90, 95, 9), (95, 101, 90, 95, 9), (95, 95, 95, 95, 9)]}
    legs = [L(sl={"type": "pct", "value": 25}), L(opt="PE", sl={"type": "pct", "value": 40})]
    t = one(make_db(tmp_path, bars), strat(legs, sl_to_cost="sl_legs"))
    ce, pe = t["legs"]
    assert ce["reason"] == "SL" and ce["exit_day"] == "2026-03-06"
    assert pe["reason"] == "SL @cost" and pe["exit_day"] == "2026-03-09" and pe["exit"] == 100.0   # 104 on the 6th did NOT trigger
    t = one(make_db(tmp_path, bars), strat(legs))                                                    # rule off: original behaviour
    assert t["legs"][1]["reason"] == "EXPIRY SETTLEMENT"


def test_sl_to_cost_sl_legs_vs_all_legs(tmp_path):
    bars = {(E1, 22500.0, "CE"): [(100, 100, 100, 100, 9), (100, 130, 95, 120, 9), (120, 120, 120, 120, 9), (120, 120, 120, 120, 9)],
            (E1, 22500.0, "PE"): [(100, 100, 100, 100, 9), (100, 100, 90, 95, 9), (95, 101, 90, 95, 9), (95, 95, 95, 95, 9)]}
    legs = [L(sl={"type": "pct", "value": 25}), L(opt="PE")]                       # PE has no SL of its own
    assert one(make_db(tmp_path, bars), strat(legs, sl_to_cost="sl_legs"))["legs"][1]["reason"] == "EXPIRY SETTLEMENT"
    assert one(make_db(tmp_path, bars), strat(legs, sl_to_cost="all_legs"))["legs"][1]["reason"] == "SL @cost"


def test_complete_square_off_closes_other_legs_at_the_same_close(tmp_path):
    bars = {(E1, 22500.0, "CE"): [(100, 100, 100, 100, 9), (100, 130, 95, 120, 9)] + [(120, 120, 120, 120, 9)] * 2,
            (E1, 22500.0, "PE"): [(100, 100, 100, 100, 9), (100, 100, 80, 85, 9)] + [(85, 85, 85, 85, 9)] * 2}
    t = one(make_db(tmp_path, bars), strat([L(sl={"type": "pct", "value": 25}), L(opt="PE")], on_leg_exit="complete"))
    ce, pe = t["legs"]
    assert ce["reason"] == "SL" and pe["reason"] == "SQUARE-OFF (leg 1 SL)" and pe["exit"] == 85.0 and pe["exit_day"] == "2026-03-06"


def test_lock_profit_arms_on_a_close_and_exits_from_the_next_close(tmp_path):
    # short CE: close 60 on the 6th = +2600 MTM (arms, floor 1000); close 90 on the 9th = +650 <= floor -> exit at close
    bars = {(E1, 22500.0, "CE"): [(100, 100, 100, 100, 9), (100, 100, 55, 60, 9), (60, 95, 60, 90, 9), (90, 90, 90, 90, 9)]}
    t = one(make_db(tmp_path, bars), strat([L()], lock_profit={"trigger": 2000, "lock": 1000}))
    leg = t["legs"][0]
    assert leg["reason"].startswith("LOCK PROFIT") and leg["exit_day"] == "2026-03-09" and leg["exit"] == 90.0
    assert any("lock-profit armed" in f for f in t["flags"])


def test_lock_and_trail_raises_the_floor_with_new_closing_highs(tmp_path):
    # +2600 on the 6th (floor 1000 + floor((2600-2000)/500)*300 = 1300); +1300 on the 9th -> exit
    bars = {(E1, 22500.0, "CE"): [(100, 100, 100, 100, 9), (100, 100, 55, 60, 9), (60, 85, 60, 80, 9), (80, 80, 80, 80, 9)]}
    lp = {"trigger": 2000, "lock": 1000, "trail_every": 500, "trail_by": 300}
    t = one(make_db(tmp_path, bars), strat([L()], lock_profit=lp))
    assert t["legs"][0]["reason"] == "LOCK PROFIT (floor ₹1300) at close"
    t = one(make_db(tmp_path, bars), strat([L()], lock_profit={"trigger": 2000, "lock": 1000}))   # no trail: 1300 > 1000 floor -> stays
    assert t["legs"][0]["reason"] == "EXPIRY SETTLEMENT"


def test_rule_conflicts_and_bad_rule_values_are_rejected():
    base = {"start": "2026-03-01", "end": "2026-03-31"}
    _, e = validate({**base, "on_leg_exit": "complete", "sl_to_cost": "all_legs",
                     "legs": [{"side": "SELL", "opt": "CE", "sl": {"type": "pct", "value": 20}, "reentry": {"max": 1}}]})
    assert any("sl_to_cost needs" in x for x in e) and any("re-entry needs" in x for x in e)
    _, e = validate({**base, "legs": [{"side": "SELL", "opt": "CE", "reentry": {"max": 1}}], "lock_profit": {"trigger": 100, "lock": 200}})
    assert any("needs a leg SL" in x for x in e) and any("lock_profit" in x for x in e)
    _, e = validate({**base, "legs": [{"side": "SELL", "opt": "CE"}], "lock_profit": {"trigger": 100, "lock": 50, "trail_every": 10}})
    assert any("trail_every and trail_by" in x for x in e)


def test_defaults_keep_the_original_rules(tmp_path):
    v, _ = validate({"start": "2026-03-01", "end": "2026-03-31", "legs": [{"side": "SELL", "opt": "CE"}]})
    assert v["on_leg_exit"] == "partial" and v["sl_to_cost"] == "off" and v["lock_profit"] is None
    assert v["legs"][0]["expiry"] == "cycle" and v["legs"][0]["reentry"] is None


# ================================================================ P4: report metrics
def T(exit_day, net, gross=None, charges=0.0):
    return {"expiry": exit_day, "exit_day": exit_day, "entry_day": exit_day, "net": net, "gross": gross if gross is not None else net,
            "charges": charges, "legs": [{"reason": "EXPIRY SETTLEMENT"}]}


def test_expectancy_streaks_and_period_tables():
    ts = [T("2025-12-30", 100), T("2026-01-06", 200), T("2026-01-06", -50), T("2026-01-13", -30), T("2026-01-20", -20), T("2026-01-27", 300)]
    m = engine.metrics(ts)
    assert m["expectancy"] == pytest.approx(500 / 6, abs=0.01) and m["avg_trade"] == m["expectancy"]
    assert m["expectancy_ratio"] == pytest.approx((500 / 6) / (100 / 3), abs=0.001)
    assert m["max_consecutive_wins"] == 2 and m["max_consecutive_losses"] == 3
    assert [r["period"] for r in m["yearly"]] == ["2025", "2026"] and m["yearly"][1]["net"] == 400 and m["yearly"][1]["trades"] == 5
    day = {r["period"]: r for r in m["daily"]}
    assert day["2026-01-06"]["trades"] == 2 and day["2026-01-06"]["wins"] == 1 and day["2026-01-06"]["net"] == 150


def test_metrics_without_losses_and_on_empty_input():
    m = engine.metrics([T("2026-01-06", 10), T("2026-01-13", 20)])
    assert m["expectancy_ratio"] is None and m["profit_factor"] is None and m["max_consecutive_losses"] == 0
    assert engine.metrics([]) == {"trades": 0}


def test_trades_on_legacy_days_are_flagged_with_lot_and_underlying_source(tmp_path):
    db = make_db(tmp_path)
    with bhavcopy._conn(db) as c:
        c.execute("UPDATE load_log SET note=? WHERE trade_date=?", ("legacy format; NIFTY: lot 65 derived from 3 futures month(s); underlying: Angel One daily index close", DAYS[0]))
    t = one(db, strat([L()]))
    assert any("legacy NSE Bhavcopy" in f and "derived" in f and "Angel One" in f for f in t["flags"])


# ================================================================ India VIX entry filter
def _vix(db, day, v):
    with bhavcopy._conn(db) as c:
        c.execute("CREATE TABLE IF NOT EXISTS vix_close (trade_date TEXT PRIMARY KEY, close REAL NOT NULL, source TEXT NOT NULL)")
        c.execute("INSERT OR REPLACE INTO vix_close VALUES (?,?, 'test')", (day, v))


def test_vix_filter_skips_low_vix_entries_and_trades_inside_the_range(tmp_path):
    db = make_db(tmp_path)
    _vix(db, DAYS[0], 11.5)
    r = engine.run(strat([L()], vix_filter={"min": 13}), db)
    assert not r["trades"] and "VIX filter" in r["skipped"][0]["reason"] and "11.50" in r["skipped"][0]["reason"]
    _vix(db, DAYS[0], 15.0)
    t = one(db, strat([L()], vix_filter={"min": 13, "max": 25}))
    assert t["vix_entry"] == 15.0


def test_vix_filter_never_guesses_a_missing_vix(tmp_path):
    r = engine.run(strat([L()], vix_filter={"min": 12}), make_db(tmp_path))
    assert not r["trades"] and "India VIX not available" in r["skipped"][0]["reason"]
    assert engine.run(strat([L()]), make_db(tmp_path))["trades"]          # no filter: unaffected


def test_vix_filter_validation():
    base = {"start": "2026-03-01", "end": "2026-03-31", "legs": [{"side": "SELL", "opt": "CE"}]}
    for bad in ({}, {"min": 20, "max": 10}, {"min": -1}, {"max": 500}):
        assert any("vix_filter" in e for e in validate({**base, "vix_filter": bad})[1]), bad
    v, e = validate({**base, "vix_filter": {"min": 12}})
    assert not e and v["vix_filter"] == {"min": 12}
