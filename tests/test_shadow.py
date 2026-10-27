"""Shadow account (BankNifty): fills decided from synthetic ticks that arrive AFTER the order, method B reversals, gaps, retries, margin, restarts.
No network, no broker: every price below is a synthetic test fixture."""
import datetime as dt

import pytest

from app.live.ticks import Tick
from app.paper_trading import dryrun
from app.paper_trading.shadow import Shadow, ShadowConfig

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
T0 = dt.datetime(2026, 10, 27, 10, 0, 0)               # a Tuesday, market open
TOKEN = "9001"
C = {"trading_symbol": "BANKNIFTY24NOV26FUT", "token": TOKEN, "exch_seg": "NFO", "lot_size": 30, "tick_size": 0.05}
SNAP = {"instrument": {**C, "lots": 1}, "ltp": 55000.0, "position": None}


def sec(n):
    return dt.timedelta(seconds=n)


def tk(ltp, t, token=TOKEN):
    aware = t.replace(tzinfo=IST)
    return Tick(token, 2, ltp, aware, aware.timestamp())


def mk(tmp_path, **cfg):
    return Shadow(ShadowConfig(**cfg), state_path=tmp_path / "s.json", log_path=tmp_path / "l.jsonl")


def entry(side="BUY", price=55000.0, stop=54450.0, qty=30, at="2026-10-27 10:00", symbol="BANKNIFTY", **k):
    return {"at": at, "owner": "u1", "instrument_id": "b1", "symbol": symbol, "contract": C["trading_symbol"], "direction": side, "entry_date": at,
            "entry_price": price, "quantity": qty, "event": "ENTRY", "initial_sl": stop, "active_stop": stop, **k}


def exit_(side="BUY", reason="STOP_LOSS_REVERSAL", price=54451.0, at="2026-10-27 10:00"):
    return {"at": at, "owner": "u1", "instrument_id": "b1", "symbol": "BANKNIFTY", "contract": C["trading_symbol"], "direction": side, "entry_date": "2026-10-27 09:30",
            "entry_price": 55000.0, "quantity": 30, "event": "EXIT", "exit_date": at, "exit_price": price, "reason": reason}


def long_position(tmp_path, **cfg):
    """BUY signalled at 55000 with the price at 55001: filled at 55001 half a second later, the stop order resting at 54451 / 54441."""
    s = mk(tmp_path, **cfg)
    s.on_tick(tk(55001.0, T0))
    s.handle([entry()], SNAP, T0)
    s.on_tick(tk(55001.0, T0 + sec(0.5)))
    s.on_tick(tk(55001.0, T0 + sec(1.5)))               # the stop order has reached the exchange
    return s, s.accts["b1"]


def test_entry_waits_for_the_latency_then_fills_at_the_tick_never_better(tmp_path):
    s = mk(tmp_path)
    s.on_tick(tk(55001.0, T0))
    s.handle([entry()], SNAP, T0)
    a = s.accts["b1"]
    o = a["orders"][0]
    assert o["type"] == "LIMIT" and o["side"] == "BUY" and o["price"] == 55003.0 and o["state"] == "PLACED"      # live 55001 + 2 points
    s.on_tick(tk(55001.0, T0 + sec(0.1)))
    assert a["position"] is None                                                                                 # 0.3 s of latency not over yet
    s.on_tick(tk(55001.0, T0 + sec(0.5)))
    assert a["position"]["entry_price"] == 55001.0 and a["orders"][0]["avg_price"] == 55001.0 and a["orders"][0]["state"] == "COMPLETE"   # at the tick, not at the limit
    stop = a["orders"][-1]
    assert stop["role"] == "stop" and stop["type"] == "STOPLOSS_LIMIT" and stop["side"] == "SELL" and stop["trigger"] == 54451.0 and stop["price"] == 54441.0
    assert a["position"]["slippage_points"] == 1.0                                                              # 1 point worse than the strategy's 55000


def test_the_one_percent_stop_comes_from_the_real_fill_or_the_strategy_price_by_config(tmp_path):
    _, real = long_position(tmp_path)
    assert real["stop_target"] == 54451.0                                      # 55001 x 0.99
    _, strat = long_position(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, stop_basis="STRATEGY_PRICE")
    assert strat["stop_target"] == 54450.0                                     # 55000 x 0.99, the strategy's own stop


def test_a_limit_needs_a_tick_strictly_past_its_price(tmp_path):
    s = mk(tmp_path)
    s.on_tick(tk(55001.0, T0))
    s.handle([entry()], SNAP, T0)
    a = s.accts["b1"]
    s.on_tick(tk(55003.0, T0 + sec(1)))                   # exactly at the limit: no fill
    assert a["position"] is None
    s.on_tick(tk(55002.5, T0 + sec(2)))
    assert a["position"]["entry_price"] == 55002.5


def test_an_entry_beyond_the_thirty_point_band_is_missed_not_chased(tmp_path):
    s = mk(tmp_path)
    s.on_tick(tk(55031.0, T0))                            # trigger 55000 + 30 = 55030: already past it
    s.handle([entry()], SNAP, T0)
    a = s.accts["b1"]
    assert a["position"] is None and not a["orders"] and a["pending_entry"] is None
    assert "past the trigger" in a["missed"][-1]["reason"] and "not chasing" in a["missed"][-1]["reason"]


def test_an_unfilled_entry_is_repriced_twice_then_given_up(tmp_path):
    s = mk(tmp_path)
    s.on_tick(tk(55001.0, T0))
    s.handle([entry()], SNAP, T0)
    a = s.accts["b1"]
    for n, (px, when) in enumerate([(55010.0, 30.8), (55020.0, 61.1)]):       # the price keeps running away from our limit
        s.on_tick(tk(px, T0 + sec(when)))
        s.poll(T0 + sec(when + 0.2))
        assert a["pending_entry"]["attempts"] == n + 2
    assert [o["price"] for o in a["orders"]] == [55003.0, 55012.0, 55022.0]
    s.on_tick(tk(55028.0, T0 + sec(92.2)))
    s.poll(T0 + sec(93))
    assert a["position"] is None and a["pending_entry"] is None
    assert "3 attempts" in a["missed"][-1]["reason"] and [o["state"] for o in a["orders"]] == ["CANCELLED"] * 3


def test_a_stop_that_gaps_past_its_limit_does_not_fill_and_is_reported_loudly(tmp_path):
    s, a = long_position(tmp_path)
    stop = a["orders"][-1]
    s.on_tick(tk(54000.0, T0 + sec(5)))                    # gap down through the trigger 54451 AND the limit 54441
    assert stop["state"] == "OPEN" and stop["triggered_at"] and a["position"] is not None
    assert any("STOP NOT FILLED" in f for f in s.view(owner="u1")["accounts"][0]["flags"])
    s.on_tick(tk(54445.0, T0 + sec(6)))                    # back above the limit: now it fills
    assert a["position"] is None and a["orders"][-1]["avg_price"] == 54445.0
    t = a["trades"][0]
    assert t["gross"] == round((54445.0 - 55001.0) * 30, 2) and t["charges"] > 0 and t["net"] == round(t["gross"] - t["charges"], 2)
    assert a["gross"] == t["gross"] and round(a["gross"] - a["charges"], 2) == t["net"]


def test_method_b_the_opposite_entry_goes_out_only_after_the_stop_has_filled(tmp_path):
    s, a = long_position(tmp_path)
    s.handle([exit_(), entry("SELL", price=54451.0, stop=55540.0, at="2026-10-27 10:00")], SNAP, T0 + sec(10))
    assert a["position"]["side"] == "BUY" and a["pending_entry"]["side"] == "SELL"
    assert [o["role"] for o in a["orders"]] == ["entry", "stop"]                       # no entry order yet: the position is still open
    s.on_tick(tk(54450.0, T0 + sec(11)))                                               # the stop order triggers and fills
    stop = a["orders"][1]
    assert stop["state"] == "COMPLETE" and a["position"] is None
    new_entry = a["orders"][2]
    assert new_entry["role"] == "entry" and new_entry["side"] == "SELL" and new_entry["price"] == 54448.0
    assert _p(new_entry["placed_at"]) >= _p(stop["fill_at"])                           # placed only after the exit filled
    s.on_tick(tk(54450.0, T0 + sec(12)))
    assert a["position"]["side"] == "SELL" and a["position"]["entry_price"] == 54450.0
    assert a["orders"][-1]["role"] == "stop" and a["orders"][-1]["side"] == "BUY" and a["orders"][-1]["trigger"] == 55540.0


def _p(s):
    return dt.datetime.fromisoformat(s)


def test_a_gap_exit_is_made_by_the_app_with_a_limit_order(tmp_path):
    s, a = long_position(tmp_path)
    s.handle([exit_(reason="GAP_EXIT_15M")], SNAP, T0 + sec(20))
    s.on_tick(tk(55100.0, T0 + sec(20.5)))
    s.poll(T0 + sec(21))
    ex = [o for o in a["orders"] if o["role"] == "exit"][0]
    assert ex["side"] == "SELL" and ex["type"] == "LIMIT" and ex["price"] == 55098.0
    assert a["orders"][1]["state"] == "CANCELLED"                                      # the resting stop is replaced by the exit order
    s.on_tick(tk(55100.0, T0 + sec(22)))
    assert a["position"] is None and a["trades"][0]["exit_by"] == "exit"


def test_a_stop_that_did_not_fill_is_replaced_by_an_exit_after_the_grace_time(tmp_path):
    s, a = long_position(tmp_path)
    s.handle([exit_()], SNAP, T0 + sec(10))                # the paper position closed, but the price never reached our stop
    s.on_tick(tk(54900.0, T0 + sec(40.2)))
    s.poll(T0 + sec(40.5))                                 # more than 30 s of grace since the paper exit
    assert [o["role"] for o in a["orders"] if o["state"] in ("PLACED", "OPEN", "TRIGGER_PENDING")] == ["exit"]


def test_a_margin_shortfall_rejects_the_entry_and_records_it_as_missed(tmp_path):
    s = mk(tmp_path, capital=150000.0, margin_per_lot=100000.0)
    s.on_tick(tk(55001.0, T0))
    snap = {"instrument": {**C, "lots": 2}, "ltp": 55000.0}
    s.handle([entry(qty=60)], snap, T0)                    # 2 lots need 2,00,000
    a = s.accts["b1"]
    assert a["orders"][0]["state"] == "REJECTED" and "margin" in a["orders"][0]["reject_reason"]
    assert a["position"] is None and a["missed"][-1]["reason"].startswith("order rejected: margin")
    assert s.view(owner="u1")["config"]["margin_check"] is True


def test_with_no_margin_configured_the_check_is_off_and_says_so(tmp_path):
    assert mk(tmp_path).view(everyone=True)["config"]["margin_check"] is False


def test_an_order_outside_market_hours_is_rejected(tmp_path):
    s = mk(tmp_path)
    late = dt.datetime(2026, 10, 27, 16, 0, 0)
    s.on_tick(tk(55001.0, late))
    s.handle([entry(at="2026-10-27 15:59")], SNAP, late)
    a = s.accts["b1"]
    assert a["orders"][0]["state"] == "REJECTED" and "market closed" in a["orders"][0]["reject_reason"] and a["position"] is None


def test_an_old_signal_is_ignored_and_other_symbols_are_not_traded(tmp_path):
    s = mk(tmp_path)
    s.on_tick(tk(55001.0, T0))
    s.handle([entry(at="2026-10-27 09:00")], SNAP, T0)               # noticed an hour late
    assert not s.accts["b1"]["orders"] and "ignored" in s.accts["b1"]["events"][-1]["text"]
    s.handle([entry(symbol="DLF")], SNAP, T0)
    s.handle([entry(backfill=True)], SNAP, T0)
    assert list(s.accts) == ["b1"] and not s.accts["b1"]["orders"]
    s.sync("d1", "u1", "DLF", SNAP, T0)
    assert "d1" not in s.accts


def test_a_day_order_expires_at_the_close_and_the_stop_is_placed_again_at_the_next_open(tmp_path):
    s, a = long_position(tmp_path)
    first = a["orders"][-1]
    nxt = dt.datetime(2026, 10, 28, 9, 16, 0)
    s.on_tick(tk(55100.0, nxt - sec(0.5)))
    s.poll(nxt)
    assert first["state"] == "EXPIRED"
    fresh = [o for o in a["orders"] if o["role"] == "stop" and o["state"] in ("PLACED", "OPEN", "TRIGGER_PENDING")]
    assert len(fresh) == 1 and fresh[0]["trigger"] == 54451.0 and fresh[0]["placed_at"].startswith("2026-10-28 09:16:00")


def test_the_stop_follows_the_strategy_when_it_moves(tmp_path):
    s, a = long_position(tmp_path)
    snap = {**SNAP, "position": {"direction": "BUY", "entry_price": 55000.0, "active_stop": 55000.0}}   # breakeven: the strategy's stop is no longer the 1% stop
    s.sync("b1", "u1", "BANKNIFTY", snap, T0 + sec(30))
    s.on_tick(tk(55200.0, T0 + sec(31)))
    stop = a["orders"][-1]
    assert stop["trigger"] == 55000.0 and stop["price"] == 54990.0 and a["stop_target"] == 55000.0


def test_state_survives_a_restart_without_duplicating_orders(tmp_path):
    s, a = long_position(tmp_path)
    again = mk(tmp_path)
    b = again.accts["b1"]
    assert [o["id"] for o in b["orders"]] == [o["id"] for o in a["orders"]] and b["position"] == a["position"] and again.seq == s.seq
    again.on_tick(tk(54445.0, T0 + sec(6)))               # the restored stop order still works
    assert again.accts["b1"]["position"] is None


def test_the_view_compares_with_paper_and_names_the_divergence(tmp_path):
    s = mk(tmp_path)
    s.on_tick(tk(55031.0, T0))
    s.handle([entry()], SNAP, T0)                          # missed: price ran away
    s.sync("b1", "u1", "BANKNIFTY", {**SNAP, "position": {"direction": "BUY", "entry_price": 55000.0, "active_stop": 54450.0}}, T0 + sec(1))
    v = s.view(owner="u1", paper_exits=[{"instrument_id": "b1", "at": "2026-10-27 10:30", "pnl": 1234.5}])["accounts"][0]
    assert v["status"] == "FLAT" and "MISSED" in v["divergence"] and v["summary"]["paper_net"] == 1234.5 and v["summary"]["missed_entries"] == 1
    assert s.view(owner="someone else")["accounts"] == []


def test_a_failure_in_the_dry_run_or_the_shadow_never_reaches_the_paper_engine():
    from app.paper_trading.engine import PaperEngine
    PaperEngine._shadow_call(lambda: 1 / 0)                # swallowed and logged


@pytest.mark.parametrize("side,price,expect", [("BUY", 55001.0, 55003.0), ("SELL", 54999.0, 54997.0)])
def test_the_entry_buffer_is_two_points_on_the_aggressive_side(tmp_path, side, price, expect):
    s = mk(tmp_path)
    s.on_tick(tk(price, T0))
    s.handle([entry(side=side, price=55000.0, stop=54450.0 if side == "BUY" else 55550.0)], SNAP, T0)
    assert s.accts["b1"]["orders"][0]["price"] == expect


def test_the_json_the_dry_run_page_reads_has_every_field_it_uses(tmp_path):
    s, a = long_position(tmp_path)
    s.on_tick(tk(54000.0, T0 + sec(5)))
    s.on_tick(tk(54445.0, T0 + sec(6)))                    # one closed trade
    s.on_tick(tk(55040.0, T0 + sec(7)))
    s2, _ = long_position(tmp_path / "second") if (tmp_path / "second").mkdir() is None else (None, None)
    v = s2.view(owner="u1", paper_exits=[])
    acct = v["accounts"][0]
    assert {"instrument_id", "symbol", "started", "status", "position", "stop", "paper", "divergence", "flags", "pending_entry", "summary", "orders", "trades", "missed", "events"} <= set(acct)
    assert {"gross", "charges", "net", "closed_trades", "capital_now", "paper_net", "missed_entries", "unrealised"} <= set(acct["summary"])
    assert {"id", "role", "type", "side", "qty", "price", "trigger", "state", "placed_at", "avg_price", "fill_at", "charges", "why"} <= set(acct["orders"][0])
    assert {"side", "qty", "entry_price", "entry_time", "contract", "strategy_entry", "slippage_points"} <= set(acct["position"])
    assert {"id", "trigger", "price", "state"} <= set(acct["stop"])
    cfg = v["config"]
    assert {"capital", "margin_check", "latency_s", "max_entry_slippage", "entry_buffer_points", "retry_after_s", "max_attempts", "stop_grace_s", "through_tick",
            "stop_basis", "charges_as_of", "charges_verified", "assumptions"} <= set(cfg) and "BANKNIFTY" in cfg["max_entry_slippage"] and "BANKNIFTY" in cfg["entry_buffer_points"]
    closed = s.view(owner="u1")["accounts"][0]["trades"][0]
    assert {"side", "qty", "entry_time", "entry", "exit_time", "exit", "gross", "charges", "net", "exit_by", "slippage_points"} <= set(closed)


def test_the_engine_adds_the_paper_pnl_and_filters_by_owner(tmp_path, monkeypatch):
    from app.paper_trading import engine as em
    monkeypatch.setattr(em, "JOURNAL_PATH", tmp_path / "j.jsonl")
    monkeypatch.setattr(em, "LEGACY_OWNER_PATH", tmp_path / "legacy.txt")
    (tmp_path / "j.jsonl").write_text('{"at": "2026-10-27 10:30", "owner": "u1", "instrument_id": "b1", "symbol": "BANKNIFTY", "event": "EXIT", "pnl_amount": 500.0}\n')
    e = em.PaperEngine.__new__(em.PaperEngine)
    e.shadow = mk(tmp_path)
    e.shadow.sync("b1", "u1", "BANKNIFTY", SNAP, T0)
    assert e.shadow_view("u1")["accounts"][0]["summary"]["paper_net"] == 500.0
    assert e.shadow_view("someone else")["accounts"] == [] and len(e.shadow_view()["accounts"]) == 1


def test_the_open_position_shows_its_unrealised_pnl_at_the_last_price(tmp_path):
    s, a = long_position(tmp_path)
    s.on_tick(tk(55101.0, T0 + sec(3)))
    assert s.view(owner="u1")["accounts"][0]["summary"]["unrealised"] == round((55101.0 - 55001.0) * 30, 2)
    s.on_tick(tk(54445.0, T0 + sec(6)))                     # the stop fills: flat, nothing unrealised
    assert s.view(owner="u1")["accounts"][0]["summary"]["unrealised"] is None
