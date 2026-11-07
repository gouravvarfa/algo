"""IV holes of a chain are filled from the other side (parity) or from the neighbouring strikes, and real IVs are never touched."""
from app.options import iv_fill

T, R, SPOT = 5 / 365, 0.065, 24000.0


def leg(iv=None):
    return {"ltp": 1.0, "iv": iv, "delta": None}


def test_a_leg_without_iv_takes_the_other_side_of_the_same_strike():
    rows = [{"strike": 23000.0, "CE": leg(None), "PE": leg(18.0)}]
    iv_fill.fill(rows, SPOT, T, R)
    ce = rows[0]["CE"]
    assert ce["iv"] == 18.0 and ce["iv_from_other"] is True and ce["delta"] is not None and 0.9 < ce["delta"] <= 1.0
    assert rows[0]["PE"]["iv"] == 18.0 and "iv_from_other" not in rows[0]["PE"]


def test_a_strike_with_no_iv_on_either_side_is_interpolated_between_neighbours():
    rows = [{"strike": 23800.0, "CE": leg(14.0), "PE": leg(14.0)},
            {"strike": 24000.0, "CE": leg(None), "PE": leg(None)},
            {"strike": 24200.0, "CE": leg(12.0), "PE": leg(12.0)}]
    iv_fill.fill(rows, SPOT, T, R)
    mid = rows[1]
    assert mid["CE"]["iv"] == 13.0 and mid["CE"]["iv_est"] is True and mid["PE"]["iv"] == 13.0
    assert abs(mid["CE"]["delta"] - 0.5) < 0.1


def test_beyond_the_last_known_strike_the_nearest_value_is_used_and_missing_contracts_stay_empty():
    rows = [{"strike": 24000.0, "CE": leg(13.0), "PE": leg(13.0)},
            {"strike": 25000.0, "CE": leg(None), "PE": None}]
    iv_fill.fill(rows, SPOT, T, R)
    assert rows[1]["CE"]["iv"] == 13.0 and rows[1]["CE"]["iv_est"] is True and rows[1]["PE"] is None


def test_real_ivs_are_not_changed_and_an_all_empty_chain_is_left_alone():
    rows = [{"strike": 24000.0, "CE": leg(13.0), "PE": leg(15.0)}]
    iv_fill.fill(rows, SPOT, T, R)
    assert rows[0]["CE"]["iv"] == 13.0 and rows[0]["PE"]["iv"] == 15.0
    empty = [{"strike": 24000.0, "CE": leg(None), "PE": leg(None)}]
    iv_fill.fill(empty, SPOT, T, R)
    assert empty[0]["CE"]["iv"] is None
