from app.strategy.top_bottom.swing_detection import detect_confirmed_swings, swings_confirmed_by


def test_detects_a_simple_top_and_bottom(series_factory):
    # index:      0    1    2    3    4    5    6    7    8
    closes = [100, 102, 105, 103, 101, 99, 101, 102, 103]
    dates, closes, _ = series_factory(closes)

    swings = detect_confirmed_swings(dates, closes, left_bars=2, right_bars=2)

    tops = [s for s in swings if s.kind == "TOP"]
    bottoms = [s for s in swings if s.kind == "BOTTOM"]
    assert len(tops) == 1 and tops[0].pivot_index == 2 and tops[0].price == 105
    assert len(bottoms) == 1 and bottoms[0].pivot_index == 5 and bottoms[0].price == 99


def test_top_is_confirmed_only_after_right_bars_elapse(series_factory):
    closes = [100, 102, 105, 103, 101]
    dates, closes, _ = series_factory(closes)

    swings = detect_confirmed_swings(dates, closes, left_bars=2, right_bars=2)
    top = next(s for s in swings if s.kind == "TOP")

    assert top.pivot_index == 2
    assert top.confirmed_index == 4  # pivot + right_bars, never earlier


def test_no_look_ahead_swing_invisible_before_confirmation_bar(series_factory):
    closes = [100, 102, 105, 103, 101]
    dates, closes, _ = series_factory(closes)
    swings = detect_confirmed_swings(dates, closes, left_bars=2, right_bars=2)

    # At bar 3 (one bar before confirmation), the pivot at index 2 must not
    # yet be visible to any caller using the as-of-index view.
    assert swings_confirmed_by(swings, 3) == []
    assert len(swings_confirmed_by(swings, 4)) == 1


def test_flat_run_produces_no_swing(series_factory):
    closes = [100.0] * 10
    dates, closes, _ = series_factory(closes)
    assert detect_confirmed_swings(dates, closes, 2, 2) == []


def test_exact_tie_does_not_confirm_a_pivot(series_factory):
    # pivot must be STRICTLY greater/less than both windows.
    closes = [100, 105, 105, 105, 100]
    dates, closes, _ = series_factory(closes)
    swings = detect_confirmed_swings(dates, closes, left_bars=1, right_bars=1)
    assert swings == []


def test_configurable_left_right_bars_changes_sensitivity(series_factory):
    closes = [100, 101, 102, 101, 100]
    dates, closes, _ = series_factory(closes)

    # left=right=1: the small peak at index 2 confirms.
    swings_1 = detect_confirmed_swings(dates, closes, left_bars=1, right_bars=1)
    assert any(s.kind == "TOP" and s.pivot_index == 2 for s in swings_1)
