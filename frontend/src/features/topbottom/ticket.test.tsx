import { describe, expect, it } from 'vitest'
import { estCharges } from './RealSwitch'

describe('estCharges', () => {
  it('is close to what Angel One shows for one NIFTY futures lot', () => {
    const c = estCharges(25032.4 * 65)                 // Angel One showed about 81 for this order
    expect(c).toBeGreaterThan(70)
    expect(c).toBeLessThan(100)
  })
  it('never drops below the flat brokerage', () => {
    expect(estCharges(0)).toBeCloseTo(23.6, 1)
  })
})
