"""Simulator what-if (target-date curve, IV/spot shift, net Greeks). Inputs are SYNTHETIC fixtures."""
import datetime as dt

import pytest

from app.options import bs, whatif
from tests.test_options_api import client, EXP1, EXP2  # noqa: F401  (reuse the temp-DB API fixture)

ON, EXP = dt.date(2026, 3, 5), dt.date(2026, 3, 10)
XS = [22000 + 10 * i for i in range(101)]


def leg(side="SELL", opt="CE", strike=22500, entry=150.0, iv=14.0, src="own", qty=65, settled=None, last=150.0):
    return {"side": side, "opt": opt, "strike": strike, "expiry": EXP, "qty": qty, "entry": entry, "iv": iv, "iv_source": src,
            "settled": settled, "last": last}


def at(res, x):
    return dict((round(a), b) for a, b in res["curve"])[x]


def test_on_expiry_day_target_curve_converges_to_intrinsic_payoff():
    r = whatif.analyse([leg()], 22500, EXP, EXP, XS)
    assert at(r, 22000) == pytest.approx(150 * 65, abs=1)                  # far OTM: whole premium kept
    assert at(r, 23000) == pytest.approx((150 - 500) * 65, abs=2 * 65)     # deep ITM: intrinsic + <Rs2 for the last quarter day


def test_short_straddle_greeks_have_the_textbook_signs():
    g = whatif.analyse([leg(), leg(opt="PE")], 22500, ON, ON, XS)["greeks"]
    assert abs(g["delta"]) < 0.15 * 65 and g["gamma"] < 0 and g["theta"] > 0 and g["vega"] < 0


def test_greeks_match_black_scholes_times_quantity():
    g = whatif.analyse([leg(side="BUY")], 22500, ON, ON, XS)["greeks"]
    ref = bs.greeks(22500, 22500, whatif.years_to_expiry(ON, EXP), whatif.RATE, whatif.DIV, 0.14, True)
    assert g["delta"] == pytest.approx(ref["delta"] * 65, abs=0.01) and g["vega"] == pytest.approx(ref["vega"] * 65, abs=0.01)


def test_iv_shift_and_time_move_short_option_pnl_in_the_right_direction():
    base = whatif.analyse([leg()], 22500, ON, ON, XS)
    hi_iv = whatif.analyse([leg()], 22500, ON, ON, XS, iv_shift=5)
    later = whatif.analyse([leg()], 22500, ON, dt.date(2026, 3, 9), XS)
    assert at(hi_iv, 22500) < at(base, 22500) < at(later, 22500)           # short vega loses, theta earns
    assert hi_iv["legs"][0]["iv_used"] == 19.0


def test_spot_shift_reports_pnl_at_the_what_if_spot_on_the_same_curve():
    r = whatif.analyse([leg()], 22500, ON, ON, XS, spot_shift_pct=1)
    assert r["what_spot"] == 22725.0 and r["pnl_at_what_spot"] < 0


def test_settled_leg_is_flat_and_leg_without_iv_is_held_at_its_last_real_price():
    r = whatif.analyse([leg(settled=40.0), leg(opt="PE", iv=None, src="none", last=90.0)], 22500, ON, ON, XS)
    flat = (40 - 150) * -65 + (90 - 150) * -65                            # both legs are SELL
    assert at(r, 22000) == at(r, 23000) == pytest.approx(flat)
    assert r["greeks"] == {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    assert [l["iv_source"] for l in r["legs"]] == ["settled", "none"]


def test_leg_iv_uses_own_close_then_atm_fallback_never_invents():
    t = whatif.years_to_expiry(ON, EXP)
    px = bs.price(22500, 22600, t, whatif.RATE, whatif.DIV, 0.15, True)
    assert whatif.leg_iv(px, 100, 22500, 22600, t, True, 13.0)[0] == pytest.approx(15.0, abs=0.01)
    assert whatif.leg_iv(px, 0, 22500, 22600, t, True, 13.0) == (13.0, "atm_fallback")      # untraded
    assert whatif.leg_iv(None, None, 22500, 22600, t, True, None) == (None, "none")


# ---------------------------------------------------------------- API
def pos(**kw):
    p = {"side": "SELL", "opt": "CE", "strike": 22500, "expiry": EXP1, "lots": 1, "lot_size": 65, "entry": 120.0, "entry_date": "2026-03-05"}
    p.update(kw)
    return p


def test_whatif_api_returns_curve_greeks_and_labels(client):  # noqa: F811
    r = client.post("/api/options/sim/whatif", json={"date": "2026-03-05", "positions": [pos(), pos(opt="PE")], "iv_shift": 2}).json()
    assert len(r["curve"]) == 241 and set(r["greeks"]) == {"delta", "gamma", "theta", "vega"}
    assert "estimate" in r["note"].lower() and r["target_date"] == "2026-03-05"


def test_whatif_api_validates_target_date_and_ranges(client):  # noqa: F811
    body = {"date": "2026-03-06", "positions": [pos()]}
    assert client.post("/api/options/sim/whatif", json={**body, "target_date": "2026-03-05"}).status_code == 422
    assert client.post("/api/options/sim/whatif", json={**body, "target_date": "2026-03-12"}).status_code == 422   # past EXP1
    assert client.post("/api/options/sim/whatif", json={**body, "iv_shift": 80}).status_code == 422
    assert client.post("/api/options/sim/whatif", json={**body, "positions": []}).status_code == 422
    assert client.post("/api/options/sim/whatif", json={"date": "2026-03-07", "positions": [pos()]}).status_code == 404


def test_whatif_api_untraded_leg_uses_atm_fallback_with_a_warning(client):  # noqa: F811
    r = client.post("/api/options/sim/whatif", json={"date": "2026-03-06", "positions": [pos(opt="PE", strike=22600)]}).json()
    assert r["legs"][0]["iv_source"] in ("atm_fallback", "none") and r["warnings"]


def test_whatif_api_expired_leg_is_settled(client):  # noqa: F811
    r = client.post("/api/options/sim/whatif", json={"date": "2026-03-11", "positions": [pos(), pos(expiry=EXP2, strike=22600)],
                                                       "target_date": "2026-03-12"}).json()
    assert r["legs"][0]["iv_source"] == "settled"
