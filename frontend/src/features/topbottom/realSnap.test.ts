import { describe, expect, it } from 'vitest'
import type { Snapshot } from '../../lib/types'
import { realSnap } from './TopBottomPage'

const base = { ltp: 50100, candles: [{ time: 1000, open: 1, high: 1, low: 1, close: 1 }, { time: 2000, open: 1, high: 1, low: 1, close: 1 }], position: { direction: 'SELL' }, trades: [{ n: 1 }], markers: [],
  stats: { live_realized: 999, live_trades: 9, live_win_rate: 50, replay_trades: 5, replay_realized: 5 }, waiting: false } as unknown as Snapshot

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
