"""Dry-run orders: written for live journal events, never for backfilled history, and per owner. No broker is involved."""
import datetime as dt
import json

from app.paper_trading import dryrun

NOV = {"trading_symbol": "DLF23NOV26FUT", "token": "61561", "exch_seg": "NFO"}


def _ev(event, **k):
    base = {"at": "2026-10-26 10:00", "owner": "u1", "instrument_id": "i1", "symbol": "DLF", "direction": "BUY", "entry_price": 668.5, "quantity": 950, "event": event}
    return {**base, **k}


def test_entry_sends_a_limit_entry_then_its_stop_order():
    o = dryrun.orders_for(_ev("ENTRY", active_stop=661.81), NOV, 668.6)
    assert [x["call"] for x in o] == ["placeOrder", "placeOrder"]
    entry, stop = o[0]["payload"], o[1]["payload"]
    assert entry["transactiontype"] == "BUY" and entry["ordertype"] == "LIMIT" and entry["tradingsymbol"] == "DLF23NOV26FUT" and entry["producttype"] == "CARRYFORWARD"
    assert entry["price"] == "669.60"                                  # live price + the stock allowance of 1.0 point, on the 0.05 grid
    assert stop["transactiontype"] == "SELL" and stop["ordertype"] == "STOPLOSS_LIMIT" and stop["triggerprice"] == "661.80" and float(stop["price"]) < 661.8


def test_allowances_are_in_points_and_differ_by_symbol():
    bank = {"trading_symbol": "BANKNIFTY24NOV26FUT", "token": "9001", "exch_seg": "NFO", "tick_size": 0.05}
    e = _ev("ENTRY", symbol="BANKNIFTY", entry_price=55000.0, quantity=30, active_stop=54450.0)
    o = dryrun.orders_for(e, bank, 55001.0)
    assert o[0]["payload"]["price"] == "55003.00"                      # 2 points, not 0.05% (= 27 points)
    assert float(o[1]["payload"]["triggerprice"]) - float(o[1]["payload"]["price"]) == 10.0     # the stop's limit sits 10 points past its trigger


def test_a_stop_exit_needs_no_new_order_and_a_breakeven_moves_the_stop():
    assert dryrun.orders_for(_ev("EXIT", exit_price=661.81, reason="ENTRY_DAY_STOP_EXIT"), NOV, 661.5)[0]["call"] == "none"
    roll = dryrun.orders_for(_ev("EXIT", exit_price=700.0, reason="ROLLED_TO_NEXT_CONTRACT"), NOV, 700.0)
    assert [x["call"] for x in roll] == ["cancelOrder", "placeOrder"] and roll[1]["payload"]["transactiontype"] == "SELL"
    assert dryrun.orders_for(_ev("SL_TO_BREAKEVEN", active_stop=668.5), NOV, 690.0)[0]["call"] == "modifyOrder"


def test_every_exit_reason_is_classified_a_stop_reversal_makes_no_extra_exit_order():
    for reason in ("STOP_LOSS_REVERSAL", "TRAILING_STOP_REVERSAL", "BREAKEVEN_EXIT_AFTER_2_PERCENT_PROFIT", "ENTRY_DAY_STOP_EXIT", "CAPPED_1_PERCENT_STOP_EXIT"):
        o = dryrun.orders_for(_ev("EXIT", exit_price=661.81, reason=reason), NOV, 661.5)
        assert [x["call"] for x in o] == ["none"], reason              # the resting stop order closed it: sending an exit too would close the position twice
    assert [x["call"] for x in dryrun.orders_for(_ev("EXIT", exit_price=661.81, reason="END_OF_BACKTEST"), NOV, 661.5)] == ["none"]
    gap = dryrun.orders_for(_ev("EXIT", exit_price=661.81, reason="GAP_EXIT_15M"), NOV, 661.5)
    assert [x["call"] for x in gap] == ["cancelOrder", "placeOrder"]   # a gap exit is made by the app


def test_a_roll_exit_closes_the_old_contract_not_the_active_one(tmp_path, monkeypatch):
    path = tmp_path / "contracts.json"
    path.write_text(json.dumps({"DLF": [{"trading_symbol": "DLF27OCT26FUT", "token": "48833", "exch_seg": "NFO", "expiry": "2026-10-27", "lot_size": 950},
                                        {"trading_symbol": "DLF23NOV26FUT", "token": "61561", "exch_seg": "NFO", "expiry": "2026-11-23", "lot_size": 950}]}))
    monkeypatch.setattr(dryrun, "CONTRACTS_PATH", path)
    e = _ev("EXIT", exit_price=700.0, reason="ROLLED_TO_NEXT_CONTRACT", contract="DLF27OCT26FUT")
    contract, unresolved = dryrun.event_contract(e, {"instrument": NOV})          # the snapshot's instrument is the NEW contract
    assert not unresolved and contract["token"] == "48833"
    order = dryrun.orders_for(e, contract, 700.0)[1]["payload"]
    assert order["tradingsymbol"] == "DLF27OCT26FUT" and order["symboltoken"] == "48833"


def test_an_unknown_old_contract_is_flagged_never_guessed(tmp_path, monkeypatch):
    monkeypatch.setattr(dryrun, "CONTRACTS_PATH", tmp_path / "none.json")
    contract, unresolved = dryrun.event_contract(_ev("EXIT", contract="DLF27OCT26FUT"), {"instrument": NOV})
    assert unresolved and contract["token"] is None and contract["trading_symbol"] == "DLF27OCT26FUT"


def test_record_skips_backfill_measures_slippage_and_reads_per_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(dryrun, "DRYRUN_PATH", tmp_path / "d.jsonl")
    snap = {"instrument": NOV, "ltp": 669.0, "feed": {"live": True, "age_s": 0.4}}
    dryrun.record([_ev("ENTRY", active_stop=661.81, backfill=True)], snap)
    assert not (tmp_path / "d.jsonl").exists()
    dryrun.record([_ev("ENTRY", active_stop=661.81), _ev("ENTRY", owner="u2", active_stop=661.81)], snap)
    mine = dryrun.read("u1")
    assert len(mine) == 1 and mine[0]["slippage_points"] == 0.5 and mine[0]["contract"] == "DLF23NOV26FUT"
    assert len(dryrun.read("u2")) == 1 and dryrun.read("nobody") == []
    assert all("ordertag" in o["payload"] for o in json.loads((tmp_path / "d.jsonl").read_text().splitlines()[0])["orders"])


def test_a_late_signal_and_a_stale_price_are_flagged_and_kept_out_of_the_statistics(tmp_path, monkeypatch):
    monkeypatch.setattr(dryrun, "DRYRUN_PATH", tmp_path / "d.jsonl")
    now = dt.datetime(2026, 10, 27, 9, 51, 0)
    live = {"instrument": NOV, "ltp": 674.85, "feed": {"live": True, "age_s": 0.2}}
    dryrun.record([_ev("ENTRY", entry_date="2026-10-27 09:22", active_stop=661.81)], live, now=now)      # noticed 28 minutes after its candle closed
    dryrun.record([_ev("ENTRY", owner="u3", entry_date="2026-10-27 09:50", active_stop=661.81)], {"instrument": NOV, "ltp": 669.0, "feed": {"live": False}}, now=now)
    dryrun.record([_ev("ENTRY", owner="u4", entry_date="2026-10-27 09:50", active_stop=661.81)], live, now=now)
    late, stale, good = dryrun.read("u1")[0], dryrun.read("u3")[0], dryrun.read("u4")[0]
    assert late["late"] and late["delay_seconds"] > 1000 and not late["slippage_valid"] and late["slippage_points"] == 6.35
    assert not stale["late"] and stale["feed_live"] is False and not stale["slippage_valid"]
    assert not good["late"] and good["slippage_valid"] and good["detected_at"] == "2026-10-27 09:51:00"


def test_rule_of_the_grid_rounding_leans_toward_the_aggressive_side():
    assert dryrun.rnd(100.01, 0.05, "up") == 100.05 and dryrun.rnd(100.04, 0.05, "down") == 100.0 and dryrun.rnd(100.02, 0.05) == 100.0
