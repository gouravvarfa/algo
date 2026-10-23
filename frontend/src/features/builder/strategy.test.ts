import { describe, expect, it } from 'vitest'
import { buildStrategy, defaultForm, fromLegacyState, L, strikeText, toLegacyState } from './strategy'

describe('strategy builder', () => {
  it('builds the JSON the server validates from the form', () => {
    const f = { ...defaultForm(), start: '2026-01-01', end: '2026-10-19', osl: '5000', lpTrig: '3000', lpLock: '1000', vixMax: '18' }
    const s = buildStrategy(f)
    expect(s).toMatchObject({ name: 'Short straddle', symbol: 'NIFTY', expiry: 'weekly', entry_days_before_expiry: 2, exit_days_before_expiry: 0, overall_sl: 5000, overall_target: null, skip_untraded: true })
    expect(s.vix_filter).toEqual({ max: 18 })                                                        // a missing bound is left out, not sent as null
    expect(s.lock_profit).toEqual({ trigger: 3000, lock: 1000 })
    expect(s.legs[0]).toMatchObject({ side: 'SELL', opt: 'CE', lots: 1, strike: { mode: 'ATM', offset: 0 }, sl: { type: 'pct', value: 15 }, target: null, reentry: null })
  })

  it('sends a premium range as low / high and other modes as a value', () => {
    const f = { ...defaultForm(), legs: [{ ...L('SELL', 'CE'), mode: 'PREMIUM_RANGE', val: 40, val2: 80 }, { ...L('BUY', 'PE'), mode: 'DELTA', val: 30 }] }
    const [a, b] = buildStrategy(f).legs
    expect(a.strike).toEqual({ mode: 'PREMIUM_RANGE', low: 40, high: 80 })
    expect(b.strike).toEqual({ mode: 'DELTA', value: 30 })
  })

  it('only sends trail and re-entry when the leg has a stop-loss', () => {
    const withSl = { ...L('SELL', 'CE', 0, 20), trail: 10, re: '2' }
    const noSl = { ...L('SELL', 'PE'), trail: 10, re: '2' }
    const [x, y] = buildStrategy({ ...defaultForm(), legs: [withSl, noSl] }).legs
    expect(x).toMatchObject({ trail_pct: 10, reentry: { max: 2 } })
    expect(y).toMatchObject({ trail_pct: null, reentry: null })
  })

  it('round-trips through the saved-strategy shape of the first version of the page', () => {
    const f = { ...defaultForm(), name: 'My plan', start: '2026-02-01', end: '2026-03-01', slip: '1', legs: [L('BUY', 'CE', 2), L('SELL', 'CE', 4, 30)] }
    const back = fromLegacyState(toLegacyState(f))
    expect(back).toEqual(f)
    expect(toLegacyState(f)['f-name']).toBe('My plan')
  })

  it('fills fields an older saved strategy does not have with the defaults', () => {
    const f = fromLegacyState({ 'f-name': 'Old', 'f-sym': 'BANKNIFTY', legs: [{ side: 'SELL', opt: 'PE', lots: 1, mode: 'ATM', val: 0, sl_t: '', sl_v: '', tg_t: '', tg_v: '', trail: '' } as never] })
    expect(f.symbol).toBe('BANKNIFTY')
    expect(f.entry).toBe('2')
    expect(f.legs[0]).toMatchObject({ exp: 'cycle', re: '0', val2: '' })
  })

  it('describes strike rules in words', () => {
    expect(strikeText({ mode: 'ATM', offset: 0 })).toBe('ATM')
    expect(strikeText({ mode: 'ATM', offset: 2 })).toBe('ATM+2')
    expect(strikeText({ mode: 'PREMIUM_RANGE', low: 40, high: 80 })).toBe('premium ₹40–₹80')
    expect(strikeText({ mode: 'DELTA', value: 30 })).toBe('Δ≈30')
  })
})
