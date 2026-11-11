import { describe, expect, it } from 'vitest'
import { bucketTime, liveBar, type Candle } from './MarketTicker'

const ist = (h: number, m: number, s = 0) => Date.UTC(2026, 10, 11, h, m, s) - 5.5 * 3600_000     // 11 Nov 2026, IST wall time -> epoch ms
const wall = (h: number, m: number) => Date.UTC(2026, 10, 11, h, m) / 1000                         // how the server writes a candle's time

describe('live index candles', () => {
  it('a tick falls into the 5 / 15-minute slot counted from 09:15, or into the day', () => {
    expect(bucketTime(ist(9, 15, 0), '5m')).toBe(wall(9, 15))
    expect(bucketTime(ist(9, 19, 59), '5m')).toBe(wall(9, 15))
    expect(bucketTime(ist(9, 20, 1), '5m')).toBe(wall(9, 20))
    expect(bucketTime(ist(10, 44, 0), '15m')).toBe(wall(10, 30))
    expect(bucketTime(ist(14, 0, 0), '1D')).toBe(Date.UTC(2026, 10, 11) / 1000)
    expect(bucketTime(ist(9, 10, 0), '5m')).toBeNull()                                              // before the open
  })
  it('the forming candle stretches to each tick; the next slot opens a new candle', () => {
    const candles: Candle[] = [{ time: wall(9, 15), open: 100, high: 105, low: 99, close: 104 }, { time: wall(9, 20), open: 104, high: 106, low: 103, close: 105 }]
    let b = liveBar(candles, null, { ltp: 108, ts: ist(9, 22) }, '5m')!
    expect(b).toMatchObject({ time: wall(9, 20), open: 104, high: 108, low: 103, close: 108 })
    b = liveBar(candles, b, { ltp: 102, ts: ist(9, 23) }, '5m')!
    expect(b).toMatchObject({ open: 104, high: 108, low: 102, close: 102 })
    const n = liveBar(candles, b, { ltp: 101, ts: ist(9, 25, 3) }, '5m')!
    expect(n).toMatchObject({ time: wall(9, 25), open: 101, high: 101, low: 101, close: 101 })
    expect(liveBar(candles, null, { ltp: 99, ts: ist(9, 16) }, '5m')).toBeNull()                    // older than the server's last candle
  })
})
