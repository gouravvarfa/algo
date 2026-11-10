/** Chart indicators for the index chart, computed in the browser from the candles (no extra data). */

/** Bollinger Bands: middle = simple average of the last `n` closes, upper / lower = middle +/- k standard deviations (population). null until n closes. */
export function bollinger(closes: number[], n = 20, k = 2): ({ mid: number; upper: number; lower: number } | null)[] {
  return closes.map((_, i) => {
    if (i < n - 1) return null
    const w = closes.slice(i - n + 1, i + 1)
    const mid = w.reduce((a, b) => a + b, 0) / n
    const sd = Math.sqrt(w.reduce((a, b) => a + (b - mid) ** 2, 0) / n)
    return { mid, upper: mid + k * sd, lower: mid - k * sd }
  })
}

/** RSI (Wilder): the first average gain / loss is the simple average of the first `n` changes, then each is smoothed as (prev x (n-1) + now) / n.
 * 100 when there was no loss at all. null until n changes. */
export function rsi(closes: number[], n = 14): (number | null)[] {
  const out: (number | null)[] = closes.map(() => null)
  if (closes.length <= n) return out
  let gain = 0, loss = 0
  for (let i = 1; i <= n; i++) {
    const d = closes[i] - closes[i - 1]
    if (d > 0) gain += d; else loss -= d
  }
  gain /= n; loss /= n
  const val = () => (loss === 0 ? 100 : 100 - 100 / (1 + gain / loss))
  out[n] = val()
  for (let i = n + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1]
    gain = (gain * (n - 1) + Math.max(d, 0)) / n
    loss = (loss * (n - 1) + Math.max(-d, 0)) / n
    out[i] = val()
  }
  return out
}
