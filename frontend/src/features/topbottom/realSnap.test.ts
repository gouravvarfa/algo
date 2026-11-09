import { describe, expect, it } from 'vitest'
import { perQtyUnit, pnlUnits, unitsLabel } from '../../lib/pnl'
import type { Position, Snapshot } from '../../lib/types'
import { livePosition, realSnap } from './TopBottomPage'

const base = { instrument: { quantity: 30, lots: 1 }, ltp: 50100, candles: [{ time: 1000, open: 1, high: 1, low: 1, close: 1 }, { time: 2000, open: 1, high: 1, low: 1, close: 1 }], position: { direction: 'SELL' }, trades: [{ n: 1 }], markers: [],
  stats: { live_realized: 999, live_trades: 9, live_win_rate: 50, replay_trades: 5, replay_realized: 5 }, waiting: false } as unknown as Snapshot

const GOLDTEN = { quantity: 10, lots: 1, lot_size: 10, point_value: 1 }

describe('rupees per point (the server\'s pnl_units)', () => {
  it('MCX gold counts lots x point value, NSE futures the quantity', () => {
    expect(pnlUnits(GOLDTEN)).toBe(1)                                          // a 10 g lot quoted per 10 g: Rs 1 per point, not Rs 10
    expect(pnlUnits({ quantity: 200, lots: 2, point_value: 10 })).toBe(20)     // 2 Gold Mini lots
    expect(pnlUnits({ quantity: 1, lots: 1, point_value: 1 })).toBe(1)        // Silver Micro
    expect(pnlUnits({ quantity: 30, lots: 1 })).toBe(30)                       // BANKNIFTY
    expect(perQtyUnit(GOLDTEN)).toBe(0.1)
    expect(perQtyUnit({ quantity: 30 })).toBe(1)
    expect(unitsLabel(GOLDTEN)).toBe('₹1 per point')
    expect(unitsLabel({ quantity: 30 })).toBe('qty 30')
  })
  it('the GOLDTEN card: +104 points is Rs 104, as the trade list says (it showed Rs 1,040)', () => {
    const p = { direction: 'BUY', entry_price: 144425 } as unknown as Position
    expect(livePosition(p, 144529, pnlUnits(GOLDTEN)).pnl_amount).toBe(104)
  })
  it('the Real view values the real quantity at the contract\'s own rate', () => {
    const s = { ...base, instrument: GOLDTEN, ltp: 144529 } as unknown as Snapshot
    const r = realSnap(s, { real_on: true, since: 1, trades: [], state: { position: { side: 'BUY', qty: 20, contract: 'GOLDTEN30NOV26FUT', entry_price: 144425 }, stop_target: 143000 } })
    expect(r.pnl_per_point).toBe(2)                                            // 2 lots x Rs 1
    expect(r.position?.pnl_amount).toBe(208)
  })
})

describe('realSnap', () => {
  it('shows only the real fills and values the real position at the live price', () => {
    const r = realSnap(base, { real_on: true, since: 1500, state: { position: { side: 'BUY', qty: 30, contract: 'X', entry_price: 50000 }, stop_target: 49500 },
      trades: [{ direction: 'BUY', qty: 30, contract: 'X', entry_price: 50000, entry_at: 2500, exit_price: null, exit_at: null, exit_by: null, pnl_points: null, pnl_amount: null, status: 'OPEN' },
        { direction: 'SELL', qty: 30, contract: 'X', entry_price: 50200, entry_at: 1600, exit_price: 50000, exit_at: 1700, exit_by: 'stop', pnl_points: 200, pnl_amount: 6000, status: 'CLOSED' }] })
    expect(r.position?.direction).toBe('BUY')
    expect(r.position?.pnl_amount).toBe(3000)
    expect(r.position?.active_stop).toBe(49500)
    expect(r.trades).toHaveLength(2)
    expect(r.stats.live_realized).toBe(6000)
    expect(r.stats.replay_trades).toBe(0)
    expect(r.markers[0].time).toBe(2000)
  })
  it('with no real trade the page is flat, not the paper position', () => {
    const r = realSnap(base, { real_on: true, since: 1, state: null, trades: [] })
    expect(r.position).toBeNull()
    expect(r.trades).toEqual([])
  })
})
