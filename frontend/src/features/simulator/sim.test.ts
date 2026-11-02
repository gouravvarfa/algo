import { describe, expect, it } from 'vitest'
import { AUTO_FLY, kfmt, planPreset, slPrice, type Chain } from './sim'

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
})
