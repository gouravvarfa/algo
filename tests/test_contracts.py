"""Contract selection and roll cutoffs (BankNifty monthly futures)."""
import datetime as dt

from app.live import contracts as c

D = dt.date
OCT, NOV, DEC = D(2026, 10, 27), D(2026, 11, 23), D(2026, 12, 29)


def test_cutoff_is_eight_days_before_expiry_and_weekends_move_to_monday():
    assert c.cutoff(OCT) == D(2026, 10, 19)            # Monday
    assert c.cutoff(NOV) == D(2026, 11, 16)            # 15 Nov is a Sunday -> Monday
    assert c.cutoff(DEC) == D(2026, 12, 21)
    assert c.cutoff(D(2026, 11, 21)) == D(2026, 11, 13) and c.cutoff(D(2026, 11, 22)) == D(2026, 11, 16)   # Saturday 14 -> Monday 16 (Sunday expiry-8)


def test_new_entries_before_the_cutoff_use_the_near_contract_and_from_the_cutoff_day_the_next():
    ex = [OCT, NOV, DEC]
    assert c.active_expiry(D(2026, 10, 14), ex) == OCT
    assert c.active_expiry(D(2026, 10, 16), ex) == OCT
    assert c.active_expiry(D(2026, 10, 18), ex) == OCT        # Sunday before the cutoff
    assert c.active_expiry(D(2026, 10, 19), ex) == NOV        # the cutoff day itself already belongs to November
    assert c.active_expiry(D(2026, 10, 21), ex) == NOV
    assert c.active_expiry(D(2026, 10, 28), ex) == NOV        # October has expired: the next contract, never the one after
    assert c.active_expiry(D(2026, 11, 13), ex) == NOV
    assert c.active_expiry(D(2026, 11, 16), ex) == DEC
    assert c.active_expiry(D(2026, 12, 30), ex) is None       # nothing live to trade


def test_chain_lists_each_contract_with_the_day_it_becomes_active():
    ch = c.chain([DEC, OCT, NOV], D(2026, 10, 7))
    assert ch == [(OCT, D(2026, 10, 7)), (NOV, D(2026, 10, 19)), (DEC, D(2026, 11, 16))]
    assert c.chain([OCT, NOV, DEC], D(2026, 10, 21)) == [(NOV, D(2026, 10, 21)), (DEC, D(2026, 11, 16))]
    assert c.chain([OCT], D(2026, 10, 25)) == []


def test_custom_roll_days():
    assert c.cutoff(OCT, 8) == D(2026, 10, 19)
    assert c.cutoff(OCT, 10) == D(2026, 10, 19)            # 17 Oct is a Saturday -> Monday
