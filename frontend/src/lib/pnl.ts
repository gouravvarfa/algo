/** How many rupees a 1.00 price move is worth, counted exactly as the server counts it (Instrument.pnl_units in app/paper_trading/engine.py).

MCX gold is quoted per 10 g and silver per kg, while the order quantity is in the contract's own unit, so for them the quantity is NOT the
multiplier: a Gold Ten lot (10 g, quantity 10) gains Rs 1 per point, a Gold Mini lot (100 g, quantity 100) Rs 10. The server stores this as
`point_value` (rupees per lot per point). Everything else (NSE futures) is points x quantity. */
export interface PnlInstrument { quantity: number; lots?: number | null; point_value?: number | null }

/** Rupees per point of the strategy's (paper) position. */
export function pnlUnits(inst: PnlInstrument): number {
  return inst.point_value ? Math.round((inst.lots || 1) * inst.point_value) : inst.quantity
}

/** Rupees per point for ONE unit of quantity of this contract: turns a real order's quantity (lots x the scrip master's lot size) into rupees per point. */
export function perQtyUnit(inst: PnlInstrument): number {
  return inst.quantity ? pnlUnits(inst) / inst.quantity : 1
}

/** A short label for the multiplier: "qty 30" for an NSE future, "Rs 1 per point" (with the rupee sign) for MCX gold / silver. */
export function unitsLabel(inst: PnlInstrument): string {
  return inst.point_value ? `₹${pnlUnits(inst)} per point` : `qty ${inst.quantity}`
}
