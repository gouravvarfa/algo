import { describe, expect, it } from 'vitest'
import { type TickerItem, withTick } from './MarketTicker'

const NOW = Date.UTC(2026, 10, 11, 5, 0, 0)                 // 11 Nov 2026 10:30 IST
const base: TickerItem = { key: 'NIFTY IT', label: 'NIFTY IT', exchange: 'NSE', token: '99926008', price: 38000, prev_close: 37900, change: 100, pct: 0.26,
  live: false, session: '2026-11-09', today: false, ref_today: 38000 }

describe('market bar price from the push line', () => {
  it('a tick of today is compared with the last close before today', () => {
    const it2 = withTick(base, { ltp: 38380, ts: NOW - 1000, at: Date.now() }, NOW)
    expect(it2.price).toBe(38380)
    expect(it2.change).toBe(380)
    expect(it2.pct).toBe(1)
    expect(it2.today).toBe(true)
  })
  it('a tick of another day, a stale tick or no reference keeps the server view', () => {
    const yesterday = NOW - 24 * 3600_000
    expect(withTick(base, { ltp: 38380, ts: yesterday, at: Date.now() }, NOW)).toBe(base)
    expect(withTick(base, { ltp: 38380, ts: NOW, at: Date.now() - 60_000 }, NOW)).toBe(base)
    expect(withTick({ ...base, ref_today: null }, { ltp: 38380, ts: NOW, at: Date.now() }, NOW).price).toBe(38000)
    expect(withTick(base, undefined, NOW)).toBe(base)
  })
  it('the IST day decides, not the browser clock zone (a tick at 23:00 IST is still today in IST)', () => {
    const late = Date.UTC(2026, 10, 11, 17, 30, 0)           // 11 Nov 23:00 IST = 17:30 UTC
    expect(withTick(base, { ltp: 38100, ts: late - 5000, at: Date.now() }, late).price).toBe(38100)
  })
})
