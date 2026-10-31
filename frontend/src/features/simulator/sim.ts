/** Types and pure helpers of the Options Simulator. Everything that talks to the server lives in useSimulator.ts. */

export type Sym = 'NIFTY' | 'BANKNIFTY'
export type Side = 'BUY' | 'SELL'
export type Opt = 'CE' | 'PE'

export interface Leg {
  token?: string | null      // the contract's exchange token (live chain only): lets the screen follow its pushed price
  ltp: number | null
  oi: number | null
  chg_oi: number | null
  volume: number | null
  iv: number | null
  delta: number | null
  age_min?: number | null
  ts?: string | null
  src?: string | null
}
export interface ChainRow { strike: number; CE: Leg | null; PE: Leg | null }
export interface Chain {
  mode: 'eod' | 'intraday' | 'live'
  date: string
  time?: string
  expiry: string
  expiries: string[]
  underlying: number
  lot_size: number | null
  atm: number
  dte: number
  pcr: number | null
  max_pain: number | null
  straddle: number | null
  atm_iv: number | null
  oi_day?: string | null
  live_legs?: number
  feed_connected?: boolean
  spot_change?: number | null
  spot_pct?: number | null
  rows: ChainRow[]
}

export interface SimPos {
  id: number | string
  side: Side
  opt: Opt
  strike: number
  expiry: string
  lots: number
  lot_size: number
  entry: number
  entry_date: string
  entry_time?: string | null
  sl?: number | null
  sl_pct?: number | null
  closed?: boolean
  exit?: number
  exit_date?: string
  exit_time?: string | null
  realized?: number | null
  exit_reason?: string
  untraded_entry?: boolean
  stale_entry?: number | null
  live?: boolean
}
export interface Valued { ltp: number | null; pnl: number | null; note: string }

export interface Payoff { curve: [number, number][]; max_profit: number | null; max_loss: number | null; breakevens: number[]; net_credit: number }
export interface Mark { underlying: number | null; positions: Valued[]; total: number; payoff: Payoff | null }
export interface WhatIf {
  error?: string
  curve?: [number, number][]
  greeks?: { delta: number; gamma: number; theta: number; vega: number }
  target_date?: string
  what_spot?: number
  spot_shift_pct?: number
  pnl_at_what_spot?: number
  legs?: { iv: number | null; iv_used: number | null; iv_source: string }[]
  warnings?: string[]
}
export interface Margin {
  margin: number | null
  has_sell: boolean
  premium_received: number
  hedge_benefit: number | null
  note: string | null
  unlisted: string[]
}

export const SIM_PRESETS: Record<string, [Side, Opt, number][]> = {
  'Bull Call Spread': [['BUY', 'CE', 0], ['SELL', 'CE', 2]],
  'Bear Put Spread': [['BUY', 'PE', 0], ['SELL', 'PE', 2]],
  'Long Straddle': [['BUY', 'CE', 0], ['BUY', 'PE', 0]],
  'Long Strangle': [['BUY', 'CE', 2], ['BUY', 'PE', 2]],
  'Iron Condor': [['SELL', 'CE', 4], ['SELL', 'PE', 4], ['BUY', 'CE', 8], ['BUY', 'PE', 8]],
  'Bull Put Credit Spread': [['SELL', 'PE', 3], ['BUY', 'PE', 6]],
  'Bear Call Credit Spread': [['SELL', 'CE', 3], ['BUY', 'CE', 6]],
  'Iron Fly': [['SELL', 'CE', 0], ['SELL', 'PE', 0], ['BUY', 'CE', 6], ['BUY', 'PE', 6]],
  'Short Straddle': [['SELL', 'CE', 0], ['SELL', 'PE', 0]],
  'Short Strangle': [['SELL', 'CE', 4], ['SELL', 'PE', 4]],
  '⭐ Short Strangle +2 · SL 60% · T-2 close (research best)': [['SELL', 'CE', 2], ['SELL', 'PE', 2]],
}
/** presets that carry their own recipe: entry day relative to expiry (at the close) and a stop-loss per leg */
export const PRESET_RULES: Record<string, { sl: number; entryDaysBefore: number }> = {
  '⭐ Short Strangle +2 · SL 60% · T-2 close (research best)': { sl: 60, entryDaysBefore: 2 },
}

export const slPrice = (side: Side, price: number, pct: number): number => +(side === 'SELL' ? price * (1 + pct / 100) : price * (1 - pct / 100)).toFixed(2)

/** The legs of a preset at their strikes (offsets are strikes from ATM, + = OTM), or the ones that have no real price. */
export function planPreset(chain: Chain, name: string): { plan: [Side, Opt, number][]; missing: string[] } {
  const strikes = chain.rows.map((r) => r.strike)
  const ai = strikes.indexOf(chain.atm)
  const plan: [Side, Opt, number][] = []
  const missing: string[] = []
  for (const [side, opt, off] of SIM_PRESETS[name]) {
    const k = strikes[ai + (opt === 'CE' ? off : -off)]
    const row = k == null ? undefined : chain.rows.find((r) => r.strike === k)
    const px = row?.[opt]?.ltp
    if (k == null || !(px != null && px > 0)) missing.push(`${k ?? 'out-of-range'} ${opt}`)
    else plan.push([side, opt, k])
  }
  return { plan, missing }
}

export const kfmt = (v: number): string =>
  Math.abs(v) >= 1e5 ? (v / 1e5).toFixed(1) + 'L' : Math.abs(v) >= 1e3 ? (v / 1e3).toFixed(0) + 'k' : Math.round(v).toLocaleString('en-IN')

export const daysBetween = (from: string, to: string): number => Math.round((new Date(to).getTime() - new Date(from).getTime()) / 864e5)
