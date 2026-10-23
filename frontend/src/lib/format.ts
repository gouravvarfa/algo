/** Number and date formatting used on every page (Indian digit grouping, tabular numerals). */
const IN = 'en-IN'

export const num = (v: number | null | undefined, d = 2): string =>
  v == null || Number.isNaN(v) ? '–' : Number(v).toLocaleString(IN, { minimumFractionDigits: d, maximumFractionDigits: d })

export const n0 = (v: number | null | undefined): string => (v == null || Number.isNaN(v) ? '–' : Number(v).toLocaleString(IN, { maximumFractionDigits: 0 }))

export const n2 = (v: number | null | undefined): string => (v == null ? '–' : Number(v).toLocaleString(IN, { maximumFractionDigits: 2 }))

export const money = (v: number | null | undefined): string => (v == null ? '–' : (v < 0 ? '−' : '') + '₹' + Math.abs(v).toLocaleString(IN, { maximumFractionDigits: 0 }))

export const moneyDec = (v: number | null | undefined): string =>
  v == null ? '–' : (v < 0 ? '−' : '') + '₹' + Math.abs(v).toLocaleString(IN, { minimumFractionDigits: 2, maximumFractionDigits: 2 })

/** CSS class for a signed number: green above zero, red below. */
export const sign = (v: number | null | undefined): string => (v == null ? '' : v > 0 ? 'pos' : v < 0 ? 'neg' : '')

/** '2026-10-21T10:12:00' -> '2026-10-21 10:12'. */
export const dt = (s: string | null | undefined): string => (s ? s.replace('T', ' ').slice(0, 16) : '–')

export const lakh = (v: number | null | undefined): string =>
  v == null ? '–' : v >= 1e7 ? (v / 1e7).toFixed(2) + 'Cr' : v >= 1e5 ? (v / 1e5).toFixed(1) + 'L' : n0(v)

export const dmy = (iso: string): string => new Date(iso + 'T00:00:00').toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' })

export const words = (s: string | null | undefined): string => (s ? s.replace(/_/g, ' ') : '')

/** Epoch seconds -> '5s ago' / '3m ago' / '2h ago'. */
export function ago(epochSeconds: number | null | undefined, now = Date.now() / 1000): string {
  if (!epochSeconds) return 'never'
  const s = Math.max(0, Math.round(now - epochSeconds))
  return s < 90 ? `${s}s ago` : s < 5400 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`
}
