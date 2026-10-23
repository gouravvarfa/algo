/** The Strategy Builder's form state, the presets, and the one pure function that turns the form into the strategy JSON the server validates. */

export interface LegForm {
  side: 'BUY' | 'SELL'
  opt: 'CE' | 'PE'
  lots: number | string
  mode: string
  val: number | string
  val2: number | string
  exp: string
  re: string
  sl_t: '' | 'pct' | 'pts'
  sl_v: number | string
  tg_t: '' | 'pct' | 'pts'
  tg_v: number | string
  trail: number | string
}

export interface FormState {
  name: string; symbol: 'NIFTY' | 'BANKNIFTY'; start: string; end: string; expiry: 'weekly' | 'monthly'
  entry: string; exit: string; osl: string; otg: string; slip: string; skip: '1' | '0'
  onexit: 'partial' | 'complete'; slcost: 'off' | 'sl_legs' | 'all_legs'
  lpTrig: string; lpLock: string; lpEvery: string; lpBy: string; vixMin: string; vixMax: string
  charges: Record<string, number | string>
  legs: LegForm[]
}

export const CHARGES: Record<string, number> = { brokerage_per_order: 20, stt_sell: 0.001, stt_exercise: 0.00125, exchange: 0.0003503, sebi: 0.000001, stamp_buy: 0.00003, gst: 0.18 }

export const MODES: [string, string][] = [['ATM', 'ATM ± strikes'], ['PCT_OTM', '% OTM'], ['PREMIUM', 'Closest premium ₹'], ['PREMIUM_RANGE', 'Premium range ₹'], ['PREMIUM_GTE', 'Premium ≥ ₹'], ['PREMIUM_LTE', 'Premium ≤ ₹'], ['STRADDLE_WIDTH', 'Straddle width ×'], ['DELTA', 'Closest delta']]
export const HINT: Record<string, string> = { ATM: '+ = OTM', PCT_OTM: '% from spot', PREMIUM_RANGE: 'low – high', STRADDLE_WIDTH: '× ATM straddle, + = OTM', DELTA: '1-99 (BS est.)' }

export const L = (side: 'BUY' | 'SELL', opt: 'CE' | 'PE', offset = 0, sl: number | null = null): LegForm =>
  ({ side, opt, lots: 1, mode: 'ATM', val: offset, val2: '', exp: 'cycle', re: '0', sl_t: sl ? 'pct' : '', sl_v: sl ?? '', tg_t: '', tg_v: '', trail: '' })

/** lots 1:1; offsets are strikes away from ATM, + = OTM (same convention as the engine) */
export const PRESETS: Record<string, LegForm[]> = {
  'Bull Call Spread': [L('BUY', 'CE', 0), L('SELL', 'CE', 2)],
  'Bear Put Spread': [L('BUY', 'PE', 0), L('SELL', 'PE', 2)],
  'Long Straddle': [L('BUY', 'CE', 0), L('BUY', 'PE', 0)],
  'Long Strangle': [L('BUY', 'CE', 2), L('BUY', 'PE', 2)],
  'Iron Condor': [L('SELL', 'CE', 4), L('SELL', 'PE', 4), L('BUY', 'CE', 8), L('BUY', 'PE', 8)],
  'Bull Put Credit Spread': [L('SELL', 'PE', 3), L('BUY', 'PE', 6)],
  'Bear Call Credit Spread': [L('SELL', 'CE', 3), L('BUY', 'CE', 6)],
  'Iron Fly': [L('SELL', 'CE', 0), L('SELL', 'PE', 0), L('BUY', 'CE', 6), L('BUY', 'PE', 6)],
  'Short straddle': [L('SELL', 'CE', 0, 15), L('SELL', 'PE', 0, 15)],
  'Short strangle': [L('SELL', 'CE', 4, 15), L('SELL', 'PE', 4, 15)],
  'Short Strangle +2 · SL 60% (research best)': [L('SELL', 'CE', 2, 60), L('SELL', 'PE', 2, 60)],
}
export const PRESET_GROUPS: [string, string[]][] = [
  ['Spreads & volatility', ['Bull Call Spread', 'Bear Put Spread', 'Long Straddle', 'Long Strangle', 'Iron Condor', 'Bull Put Credit Spread', 'Bear Call Credit Spread', 'Iron Fly']],
  ['Premium selling', ['Short straddle', 'Short strangle', 'Short Strangle +2 · SL 60% (research best)']],
]
export const PRESET_NOTES: Record<string, string> = {
  'Bull Call Spread': 'Mildly bullish · max loss limited to the net debit',
  'Bear Put Spread': 'Mildly bearish · max loss limited to the net debit',
  'Long Straddle': 'Big move, direction unknown · max loss = total premium',
  'Long Strangle': 'Big move, cheaper than a straddle · max loss = total premium',
  'Iron Condor': 'Sideways market · loss limited by the wings',
  'Bull Put Credit Spread': 'Mildly bullish · max loss = spread width − credit',
  'Bear Call Credit Spread': 'Mildly bearish · max loss = spread width − credit',
  'Iron Fly': 'Very tight range · limited loss',
  'Short straddle': 'Range-bound · unlimited risk (leg SL 15%)',
  'Short strangle': 'Range-bound · unlimited risk (leg SL 15%)',
  'Short Strangle +2 · SL 60% (research best)': 'Use with entry 2 trading days before expiry, hold to expiry. NIFTY 2020-26 backtest, not a guarantee',
}

export const defaultForm = (): FormState => ({
  name: 'Short straddle', symbol: 'NIFTY', start: '', end: '', expiry: 'weekly', entry: '2', exit: '0', osl: '', otg: '', slip: '0.5', skip: '1',
  onexit: 'partial', slcost: 'off', lpTrig: '', lpLock: '', lpEvery: '', lpBy: '', vixMin: '', vixMax: '', charges: { ...CHARGES },
  legs: PRESETS['Short straddle'].map((l) => ({ ...l })),
})

const num = (v: string | number | null | undefined): number | null => (v === '' || v == null ? null : Number(v))

/** Form -> the strategy JSON the server validates (app/options/strategy.py). Nothing is validated here: the server refuses a bad strategy with a list of reasons. */
export function buildStrategy(f: FormState) {
  const vmin = num(f.vixMin), vmax = num(f.vixMax)
  const lpT = num(f.lpTrig), lpE = num(f.lpEvery), lpB = num(f.lpBy)
  return {
    name: f.name, symbol: f.symbol, start: f.start, end: f.end, expiry: f.expiry,
    entry_days_before_expiry: Number(f.entry), exit_days_before_expiry: Number(f.exit),
    overall_sl: num(f.osl), overall_target: num(f.otg), slippage_pct: Number(f.slip), skip_untraded: f.skip === '1',
    charges: Object.fromEntries(Object.entries(f.charges).map(([k, v]) => [k, Number(v)])),
    on_leg_exit: f.onexit, sl_to_cost: f.slcost,
    vix_filter: vmin == null && vmax == null ? null : Object.fromEntries([['min', vmin], ['max', vmax]].filter((x) => x[1] != null)),
    lock_profit: lpT == null ? null : { trigger: lpT, lock: num(f.lpLock), ...(lpE != null || lpB != null ? { trail_every: lpE, trail_by: lpB } : {}) },
    legs: f.legs.map((l) => ({
      side: l.side, opt: l.opt, lots: Number(l.lots), expiry: l.exp,
      strike: l.mode === 'ATM' ? { mode: 'ATM', offset: Math.round(Number(l.val) || 0) } : l.mode === 'PREMIUM_RANGE' ? { mode: l.mode, low: Number(l.val), high: Number(l.val2) } : { mode: l.mode, value: Number(l.val) },
      sl: l.sl_t ? { type: l.sl_t, value: Number(l.sl_v) } : null,
      target: l.tg_t ? { type: l.tg_t, value: Number(l.tg_v) } : null,
      trail_pct: l.sl_t && l.trail !== '' ? Number(l.trail) : null,
      reentry: l.sl_t && Number(l.re) > 0 ? { max: Number(l.re) } : null,
    })),
  }
}
export type Strategy = ReturnType<typeof buildStrategy>

export function strikeText(st: { mode: string; offset?: number; value?: number; low?: number; high?: number }): string {
  const v = st.value ?? 0
  switch (st.mode) {
    case 'ATM': return st.offset ? `ATM${st.offset > 0 ? '+' : ''}${st.offset}` : 'ATM'
    case 'PCT_OTM': return `${st.value}% OTM`
    case 'PREMIUM': return `≈₹${st.value} premium`
    case 'PREMIUM_RANGE': return `premium ₹${st.low}–₹${st.high}`
    case 'PREMIUM_GTE': return `premium ≥ ₹${st.value}`
    case 'PREMIUM_LTE': return `premium ≤ ₹${st.value}`
    case 'STRADDLE_WIDTH': return `ATM ${v > 0 ? '+' : ''}${v}× straddle`
    case 'DELTA': return `Δ≈${st.value}`
    default: return String(st.mode)
  }
}

/** A saved state from an older version lacks newer fields: they get the defaults, never the previous strategy's values. */
export function mergeForm(saved: Partial<FormState> | null | undefined): FormState {
  const d = defaultForm()
  if (!saved) return d
  return { ...d, ...saved, charges: { ...d.charges, ...(saved.charges ?? {}) }, legs: (saved.legs?.length ? saved.legs : d.legs).map((l) => ({ ...L('SELL', 'CE'), ...l })) }
}

/** The saved-strategy / draft shape used by the first version of this page (keys are the old input ids). It is kept so strategies saved earlier still open. */
export type LegacyState = Record<string, unknown> & { legs?: LegForm[]; charges?: Record<string, number | string> }

export function toLegacyState(f: FormState): LegacyState {
  return {
    'f-name': f.name, 'f-sym': f.symbol, 'f-start': f.start, 'f-end': f.end, 'f-exp': f.expiry, 'f-entry': f.entry, 'f-exit': f.exit, 'f-osl': f.osl, 'f-otg': f.otg,
    'f-slip': f.slip, 'f-skip': f.skip, 'f-onexit': f.onexit, 'f-slcost': f.slcost, 'f-lp-trig': f.lpTrig, 'f-lp-lock': f.lpLock, 'f-lp-every': f.lpEvery,
    'f-lp-by': f.lpBy, 'f-vix-min': f.vixMin, 'f-vix-max': f.vixMax, legs: f.legs, charges: f.charges,
  }
}

export function fromLegacyState(s: LegacyState | null | undefined): FormState {
  if (!s) return defaultForm()
  const str = (k: string, d: string) => (s[k] == null ? d : String(s[k]))
  const d = defaultForm()
  return mergeForm({
    name: str('f-name', d.name), symbol: str('f-sym', d.symbol) as FormState['symbol'], start: str('f-start', ''), end: str('f-end', ''), expiry: str('f-exp', d.expiry) as FormState['expiry'],
    entry: str('f-entry', d.entry), exit: str('f-exit', d.exit), osl: str('f-osl', ''), otg: str('f-otg', ''), slip: str('f-slip', d.slip), skip: str('f-skip', d.skip) as FormState['skip'],
    onexit: str('f-onexit', d.onexit) as FormState['onexit'], slcost: str('f-slcost', d.slcost) as FormState['slcost'], lpTrig: str('f-lp-trig', ''), lpLock: str('f-lp-lock', ''),
    lpEvery: str('f-lp-every', ''), lpBy: str('f-lp-by', ''), vixMin: str('f-vix-min', ''), vixMax: str('f-vix-max', ''), charges: s.charges as Record<string, number | string>, legs: s.legs,
  })
}
