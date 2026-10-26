"""Dry-run orders: written for live journal events, never for backfilled history, and per owner. No broker is involved."""
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
    assert 668.6 <= float(entry["price"]) <= 669.0                     # a small allowance over the live price
    assert stop["transactiontype"] == "SELL" and stop["ordertype"] == "STOPLOSS_LIMIT" and stop["triggerprice"] == "661.80" and float(stop["price"]) < 661.8


def test_a_stop_exit_needs_no_new_order_and_a_breakeven_moves_the_stop():
    assert dryrun.orders_for(_ev("EXIT", exit_price=661.81, reason="ENTRY_DAY_STOP_EXIT"), NOV, 661.5)[0]["call"] == "none"
    roll = dryrun.orders_for(_ev("EXIT", exit_price=700.0, reason="ROLLED_TO_NEXT_CONTRACT"), NOV, 700.0)
    assert [x["call"] for x in roll] == ["cancelOrder", "placeOrder"] and roll[1]["payload"]["transactiontype"] == "SELL"
    assert dryrun.orders_for(_ev("SL_TO_BREAKEVEN", active_stop=668.5), NOV, 690.0)[0]["call"] == "modifyOrder"


def test_record_skips_backfill_measures_slippage_and_reads_per_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(dryrun, "DRYRUN_PATH", tmp_path / "d.jsonl")
    snap = {"instrument": NOV, "ltp": 669.0}
    dryrun.record([_ev("ENTRY", active_stop=661.81, backfill=True)], snap)
    assert not (tmp_path / "d.jsonl").exists()
    dryrun.record([_ev("ENTRY", active_stop=661.81), _ev("ENTRY", owner="u2", active_stop=661.81)], snap)
    mine = dryrun.read("u1")
    assert len(mine) == 1 and mine[0]["slippage_points"] == 0.5 and mine[0]["contract"] == "DLF23NOV26FUT"
    assert len(dryrun.read("u2")) == 1 and dryrun.read("nobody") == []
    assert all("ordertag" in o["payload"] for o in json.loads((tmp_path / "d.jsonl").read_text().splitlines()[0])["orders"])
