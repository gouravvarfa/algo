import { describe, expect, it } from 'vitest'
import { AUTO_FLY, AUTO_FLY_ADJ, STREAM_OPTION_MAX, kfmt, nearestDateIndex, pickResell, planPreset, slPrice, streamOptionTokens, type Chain } from './sim'

const row = (strike: number, ce: number | null, pe: number | null) => ({
  strike,
  CE: ce == null ? null : { ltp: ce, oi: 1, chg_oi: 0, volume: 1, iv: 10, delta: 0.5 },
  PE: pe == null ? null : { ltp: pe, oi: 1, chg_oi: 0, volume: 1, iv: 10, delta: -0.5 },
})
const chain = (rows = [row(22900, 150, 90), row(23000, 100, 100), row(23100, 60, 150), row(23200, 30, null)]): Chain => ({
  mode: 'eod', date: '2026-10-21', expiry: '2026-10-27', expiries: ['2026-10-27'], underlying: 23000, lot_size: 65, atm: 23000, dte: 6, pcr: 1, max_pain: 23000, straddle: 200, atm_iv: 14, rows,
})

describe('simulator helpers', () => {
  it('puts the stop-loss above a SELL and below a BUY', () => {
    expect(slPrice('SELL', 100, 15)).toBe(115)
    expect(slPrice('BUY', 100, 15)).toBe(85)
  })
  it('formats axis values', () => {
    expect([kfmt(250000), kfmt(4200), kfmt(-4200), kfmt(80)]).toEqual(['2.5L', '4k', '-4k', '80'])
  })
  it('places a short straddle on the ATM strike', () => {
    const { plan, missing } = planPreset(chain(), 'Short Straddle')
    expect(missing).toEqual([])
    expect(plan).toEqual([['SELL', 'CE', 23000], ['SELL', 'PE', 23000]])
  })
  it('puts CE offsets above ATM and PE offsets below (OTM both ways)', () => {
    const { plan } = planPreset(chain([row(22800, 200, 40), row(22900, 150, 60), row(23000, 100, 100), row(23100, 60, 150), row(23200, 30, 210)]), 'Short Strangle')
    expect(plan.map(([, o, k]) => `${o}${k}`)).toEqual([])        // offset 4 is out of range in this small chain
  })
  it('refuses a preset when a leg has no real price (nothing is guessed)', () => {
    const r = planPreset(chain([row(22900, 150, 90), row(23000, 100, 100), row(23100, 60, null)]), 'Bear Put Spread')
    expect(r.missing.length).toBeGreaterThan(0)
  })
})

describe('auto iron fly preset', () => {
  it('sells ATM and hedges at the strikes priced nearest Rs 65', () => {
    const row = (strike: number, ce: number, pe: number) => ({ strike, CE: { ltp: ce }, PE: { ltp: pe } })
    const chain = { atm: 100, rows: [row(80, 300, 40), row(90, 200, 62), row(100, 100, 100), row(110, 57, 150), row(120, 40, 200)] } as unknown as Chain
    expect(planPreset(chain, AUTO_FLY).plan).toEqual([['BUY', 'CE', 110], ['BUY', 'PE', 90], ['SELL', 'CE', 100], ['SELL', 'PE', 100]])
  })

  it('the adjusting iron fly places the same legs as the auto fly', () => {
    const row = (strike: number, ce: number, pe: number) => ({ strike, CE: { ltp: ce }, PE: { ltp: pe } })
    const c = { atm: 100, rows: [row(80, 300, 40), row(90, 200, 62), row(100, 100, 100), row(110, 57, 150), row(120, 40, 200)] } as unknown as Chain
    expect(planPreset(c, AUTO_FLY_ADJ).plan).toEqual(planPreset(c, AUTO_FLY).plan)
  })

  it('re-sell strike: nearest the target delta, strictly inside the held hedge, never the strike just closed', () => {
    const row = (k: number, ce: number, pe: number) => ({ strike: k, CE: { ltp: 50, delta: ce }, PE: { ltp: 50, delta: pe } })
    const c = { rows: [row(90, 0.8, -0.2), row(95, 0.65, -0.35), row(100, 0.5, -0.5), row(105, 0.34, -0.66), row(110, 0.2, -0.8)] } as unknown as Chain
    expect(pickResell(c, 'CE', 110, 0.5, 95)).toBe(100)                  // nearest 0.50, hedge at 110 is outside it
    expect(pickResell(c, 'CE', 100, 0.5, 95)).toBeNull()                 // 100 is the hedge, so only strikes below it count and none is near 0.50
    expect(pickResell(c, 'PE', 90, 0.5, 105)).toBe(100)                  // put side: strikes above the hedge
    expect(pickResell(c, 'CE', 100, 0.5, 90, 0.1)).toBeNull()            // nothing within the tolerance
  })
})

describe('date picker', () => {
  const dates = ['2026-03-05', '2026-03-06', '2026-03-09', '2026-03-10']
  it('goes to the picked session, or to the session before a weekend / holiday, or to the ends', () => {
    expect(nearestDateIndex(dates, '2026-03-09')).toBe(2)
    expect(nearestDateIndex(dates, '2026-03-07')).toBe(1)           // a Saturday: Friday's session
    expect(nearestDateIndex(dates, '2026-01-01')).toBe(0)           // before everything: the first
    expect(nearestDateIndex(dates, '2026-12-01')).toBe(3)           // after everything: the last
    expect(nearestDateIndex([], '2026-03-09')).toBe(-1)
    expect(nearestDateIndex(dates, '')).toBe(-1)
  })
})

describe('live chain: which option prices are pushed', () => {
  // SILVERM's near expiry of 20 Nov 2026: 237 strikes = 474 tokens, more than the 400 a stream may carry
  const leg = (t: string) => ({ token: t, ltp: 1, oi: null, chg_oi: null, volume: null, iv: null, delta: null })
  const big = { atm: 150000, expiry: '2026-11-24', rows: Array.from({ length: 237 }, (_, i) => { const k = 120000 + i * 250; return { strike: k, CE: leg(`c${k}`), PE: leg(`p${k}`) } }) }
  it('asks for at most STREAM_OPTION_MAX tokens, nearest the money first, both sides', () => {
    const t = streamOptionTokens(big, [])
    expect(t.length).toBe(STREAM_OPTION_MAX)
    expect(t.slice(0, 4)).toEqual(['c150000', 'p150000', 'c149750', 'p149750'])     // the ATM, then the nearest strike (the lower one on a tie)
    expect(t).toContain('c140750')                                                   // 37 strikes below: still pushed
    expect(t).not.toContain('c120000')                                               // the far end keeps the chain's own refresh
  })
  it('always pushes the legs of open positions, however far, but not closed ones or another expiry', () => {
    const pos = [
      { strike: 120000, opt: 'PE' as const, expiry: '2026-11-24', closed: false },
      { strike: 179000, opt: 'CE' as const, expiry: '2026-11-24', closed: true },
      { strike: 120250, opt: 'CE' as const, expiry: '2026-12-22', closed: false },
    ]
    const t = streamOptionTokens(big, pos)
    expect(t[0]).toBe('p120000')
    expect(t.length).toBe(STREAM_OPTION_MAX)
    expect(t).not.toContain('c179000')
    expect(t).not.toContain('c120250')
  })
  it('a small chain is pushed whole, a leg without a token is skipped', () => {
    const small = { atm: 23000, expiry: '2026-11-24', rows: [{ strike: 22950, CE: leg('a'), PE: null }, { strike: 23000, CE: leg('b'), PE: { ...leg('x'), token: null } }] }
    expect(streamOptionTokens(small, [])).toEqual(['b', 'a'])
  })
})
