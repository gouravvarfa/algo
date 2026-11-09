"""MCX gold is quoted per 10 g while the order quantity is in grams: a Gold Ten lot (quantity 10) gains Rs 1 per point, not Rs 10. Seen live on 9 Nov 2026:
the Top-Bottom card showed Rs 1,040 for +104 points on GOLDTEN while the trade list (the server) showed Rs 104. Real trades must count the same way."""
import time
import uuid

from app.broker import tb_real
from app.paper_trading import engine as em


def _inst(symbol, qty, lots, point_value):
    return em.Instrument(id="i", symbol=symbol, instrument_type="FUTURE", timeframe="1D", quantity=qty, lots=lots, lot_size=qty // lots, token="1",
                         trading_symbol=f"{symbol}30NOV26FUT", exch_seg="MCX" if point_value else "NFO", expiry="2026-11-30", from_date="2026-01-01",
                         added_at="2026-01-01", session="MCX" if point_value else "NSE", point_value=point_value)


def test_rupees_per_point_follow_the_point_value_not_the_quantity():
    assert _inst("GOLDTEN", 10, 1, 1).pnl_units == 1            # 10 g lot, quoted per 10 g
    assert _inst("GOLDM", 200, 2, 10).pnl_units == 20           # 2 x 100 g lots
    assert _inst("SILVERMIC", 1, 1, 1).pnl_units == 1           # 1 kg lot, quoted per kg
    assert _inst("BANKNIFTY", 30, 1, None).pnl_units == 30      # NSE: points x quantity


def _fill(db, uid, iid, role, side, qty, price, at):
    db.execute("INSERT INTO real_tb_orders (id, user_id, instrument_id, symbol, contract, role, side, qty, order_type, status, filled_avg, created_at, updated_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, uid, iid, "GOLDTEN", "GOLDTEN30NOV26FUT", role, side, qty, "LIMIT", "filled", price, at, at))
    db.commit()


def test_real_trades_count_mcx_gold_per_lot_not_per_gram(db):
    uid, iid, t = uuid.uuid4().hex, uuid.uuid4().hex, time.time()
    _fill(db, uid, iid, "entry", "BUY", 20, 144425.0, t)        # 2 lots of Gold Ten (quantity 20 g)
    _fill(db, uid, iid, "stop", "SELL", 20, 144529.0, t + 60)
    per_qty = _inst("GOLDTEN", 10, 1, 1).pnl_units / 10          # what the API passes: pnl_units / quantity = 0.1
    [tr] = tb_real.RealTB.real_trades(db, uid, iid, per_qty)
    assert tr["pnl_points"] == 104.0 and tr["pnl_amount"] == 208.0         # Rs 1 per point per lot x 2 lots (it said Rs 2,080)
    [nse] = tb_real.RealTB.real_trades(db, uid, iid)                       # the default (NSE): points x quantity
    assert nse["pnl_amount"] == 104.0 * 20
