import { describe, expect, it } from 'vitest'
import { bollinger, rsi } from './indicators'

describe('Bollinger Bands (20, 2)', () => {
  it('is empty until 20 closes, then mean +/- 2 standard deviations', () => {
    const c = Array.from({ length: 25 }, (_, i) => 100 + (i % 2 === 0 ? 1 : -1))      // 101, 99, 101, ...
    const b = bollinger(c)
    expect(b[18]).toBeNull()
    expect(b[19]!.mid).toBeCloseTo(100, 10)
    expect(b[19]!.upper).toBeCloseTo(102, 10)                                         // sd = 1
    expect(b[19]!.lower).toBeCloseTo(98, 10)
  })
  it('a flat market has the bands on the price', () => {
    const b = bollinger(Array(20).fill(50))
    expect(b[19]).toEqual({ mid: 50, upper: 50, lower: 50 })
  })
})

describe('RSI (14, Wilder)', () => {
  it('only rises -> 100, only falls -> 0, empty until 14 changes', () => {
    const up = rsi(Array.from({ length: 20 }, (_, i) => 100 + i))
    expect(up[13]).toBeNull()
    expect(up[14]).toBe(100)
    const down = rsi(Array.from({ length: 20 }, (_, i) => 100 - i))
    expect(down[19]).toBeCloseTo(0, 10)
  })
  it('matches the textbook example (Wilder, 14 periods)', () => {
    // the classic StockCharts example closes; exact (unrounded) Wilder values 70.46 and 66.25, as TA-Lib gives (the article's 70.53 / 66.32 used rounded averages)
    const c = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00]
    const r = rsi(c)
    expect(r[14]).toBeCloseTo(70.464, 2)
    expect(r[15]).toBeCloseTo(66.250, 2)
  })
})
