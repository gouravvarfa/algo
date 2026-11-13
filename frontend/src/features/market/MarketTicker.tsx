import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { createChart, type ISeriesApi, type LogicalRange, type UTCTimestamp } from 'lightweight-charts'
import { useEffect, useMemo, useRef, useState } from 'react'
import { get } from '../../lib/api'
import { fresh, type Tick, useLiveTicks } from '../../lib/stream'
import { bollinger, rsi } from './indicators'
import './market.css'

export interface TickerItem {
  key: string; label: string; exchange: string; price: number | null; prev_close: number | null; change: number | null; pct: number | null; live: boolean; spark?: number[]
  session?: string | null; today?: boolean
  token?: string; ref_today?: number | null      // the index's feed token, and the last close before today (the reference for today's ticks)
}
interface TickerData { items: TickerItem[]; nse_open: boolean; mcx_open: boolean }
export interface Candle { time: number; open: number; high: number; low: number; close: number; volume?: number }
interface ChartData { key: string; label: string; tf: string; candles: Candle[]; partial?: boolean }   // partial: only today's candles (from live ticks) so far

const fmt = (v: number | null | undefined, d = 2) => (v == null ? '–' : v.toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d }))
const sgn = (v: number | null | undefined) => (v == null ? '' : v > 0 ? '+' : v < 0 ? '−' : '')
const tone = (v: number | null | undefined) => (v == null || v === 0 ? 'flat' : v > 0 ? 'up' : 'down')
const chg = (c: number | null, p: number | null) => (c == null ? '' : `${sgn(c)}${fmt(Math.abs(c))} (${sgn(p)}${fmt(Math.abs(p ?? 0))}%)`)

const IST_MS = 5.5 * 3600_000
const istDay = (ms: number) => new Date(ms + IST_MS).toISOString().slice(0, 10)

/** The price pushed tick by tick (SSE, the same line as the sidebar's NIFTY / BANKNIFTY) against the last close before today. Without a fresh tick of
 * TODAY (before the open, a holiday, the line down) the server's own view stays: the last session and its change. */
export function withTick(it: TickerItem, t: Tick | undefined, nowMs = Date.now()): TickerItem {
  if (!t || !fresh(t) || it.ref_today == null || !it.ref_today || istDay(t.ts) !== istDay(nowMs)) return it
  const change = Math.round((t.ltp - it.ref_today) * 100) / 100
  return { ...it, price: t.ltp, prev_close: it.ref_today, change, pct: Math.round((change / it.ref_today) * 10000) / 100, today: true, live: true }
}

/** "today", or the date of the last session when nothing traded today (holiday, weekend, before the open). */
function sessionLabel(it: TickerItem): string {
  if (it.today !== false || !it.session) return 'today'
  return new Date(it.session + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })
}

/** A tiny line of today's prices (built on the server from live ticks: no extra API call). */
function Spark({ pts, cls }: { pts?: number[]; cls: string }) {
  if (!pts || pts.length < 3) return null
  const lo = Math.min(...pts), hi = Math.max(...pts), span = hi - lo || 1
  const d = pts.map((v, i) => `${i ? 'L' : 'M'}${((i / (pts.length - 1)) * 56).toFixed(1)},${(18 - ((v - lo) / span) * 16 - 1).toFixed(1)}`).join(' ')
  return <svg className={'tk-spark ' + cls} viewBox="0 0 56 18" width="56" height="18" aria-hidden="true"><path d={d} /></svg>
}

/** Remember each index's last price; a new price flashes green (up) or red (down) for a moment, like an exchange ticker tape. */
function useFlash(items: TickerItem[]) {
  const last = useRef<Record<string, number | null>>({})
  const [flash, setFlash] = useState<Record<string, 'fu' | 'fd'>>({})
  useEffect(() => {
    const next: Record<string, 'fu' | 'fd'> = {}
    for (const it of items) {
      const prev = last.current[it.key]
      if (prev != null && it.price != null && it.price !== prev) next[it.key] = it.price > prev ? 'fu' : 'fd'
      last.current[it.key] = it.price
    }
    if (Object.keys(next).length) {
      setFlash(next)
      const t = setTimeout(() => setFlash({}), 700)
      return () => clearTimeout(t)
    }
  }, [items])
  return flash
}

/** At most one new set of prices a second on the moving bar: the ticks of 19 indices arrive many times a second, and every repaint of the moving
 * strip costs (on an iPad most of all). A new list from the server shows at once. */
function useThrottled(next: TickerItem[], base: TickerItem[], ms: number): TickerItem[] {
  const [shown, setShown] = useState(next)
  const at = useRef(0)
  const lastBase = useRef(base)
  useEffect(() => {
    const wait = lastBase.current !== base ? 0 : Math.max(0, ms - (Date.now() - at.current))
    lastBase.current = base
    const t = window.setTimeout(() => { at.current = Date.now(); setShown(next) }, wait)
    return () => window.clearTimeout(t)
  }, [next, base, ms])
  return shown
}

/** The strip is moved by script, frame by frame, not by a CSS animation: iPad Safari left most of a CSS-animated strip unpainted once its prices
 * changed every second (only the item just repainted showed, 11 Nov 2026). Paused while `held()` (the mouse on it, a chart open); still for people
 * who prefer reduced motion. */
function useMarquee(track: React.RefObject<HTMLDivElement>, run: boolean, secondsPerLoop: number, held: () => boolean) {
  const heldRef = useRef(held)
  heldRef.current = held
  useEffect(() => {
    const el = track.current
    if (!el || !run || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return
    let x = 0, last = performance.now(), raf = 0
    const frame = (now: number) => {
      const dt = Math.min(64, now - last)
      last = now
      const half = el.scrollWidth / 2
      if (!heldRef.current() && half > 0) {
        x = (x + (half / secondsPerLoop) * (dt / 1000)) % half
        el.style.transform = `translate3d(${(-x).toFixed(1)}px, 0, 0)`
      }
      raf = window.requestAnimationFrame(frame)
    }
    raf = window.requestAnimationFrame(frame)
    return () => window.cancelAnimationFrame(raf)
  }, [track, run, secondsPerLoop])
}

/** The scrolling market bar under the top bar: index prices and day change (display only, never part of a strategy). A click opens the index's chart. */
export function MarketTicker() {
  // prices are PUSHED (SSE) as each tick arrives; the server's list (names, previous closes, today's small line) is read every 30 s, or every 2 s
  // while the push line is not up (switched off, reconnecting)
  const pushed = useRef(false)
  const q = useQuery({ queryKey: ['market-ticker'], queryFn: () => get<TickerData>('/api/market/ticker'), refetchInterval: () => (pushed.current ? 30_000 : 2000), retry: false })
  const base = useMemo(() => q.data?.items ?? [], [q.data])
  const specs = useMemo(() => base.filter((i) => i.token).map((i) => `${i.exchange}:${i.token}`), [base])
  const { ticks, status } = useLiveTicks(specs, !!q.data?.nse_open)
  pushed.current = status === 'live'
  const sig = base.map((i) => { const t = i.token ? ticks[i.token] : undefined; return fresh(t) ? `${t.ltp}@${t.ts}` : '' }).join('|')
  const merged = useMemo(() => base.map((it) => withTick(it, it.token ? ticks[it.token] : undefined)), [base, sig]) // eslint-disable-line react-hooks/exhaustive-deps
  const items = useThrottled(merged, base, 1000)
  const flash = useFlash(items)
  const [open, setOpen] = useState<string | null>(null)
  const track = useRef<HTMLDivElement>(null)
  const mouseOn = useRef(false)
  const keyFocus = useRef(false)
  useMarquee(track, items.length > 0, Math.max(45, items.length * 5), () => mouseOn.current || keyFocus.current || open != null)
  const qc = useQueryClient()
  // start loading an index's chart as soon as the pointer / finger is on it, so it is usually ready by the time the click lands
  const warm = (key: string) => { void qc.prefetchQuery({ queryKey: ['market-chart', key, '5m'], queryFn: () => chartOf(key, '5m'), staleTime: 25_000 }) }
  const close = () => { setOpen(null); (document.activeElement as HTMLElement | null)?.blur?.() }
  if (!items.length) return null
  const nse = q.data!.nse_open
  const row = (dup: boolean) => items.map((it) => (
    <button key={(dup ? 'b-' : 'a-') + it.key} className={`tk-item ${tone(it.change)} ${flash[it.key] ?? ''}`} onClick={() => setOpen(it.key)} onPointerEnter={() => warm(it.key)} onTouchStart={() => warm(it.key)}
      tabIndex={dup ? -1 : 0} aria-hidden={dup || undefined} aria-label={dup ? undefined : `${it.label} ${fmt(it.price)} ${chg(it.change, it.pct)}. Open chart`}>
      <span className="tk-name">{it.label}</span>
      <span className="tk-price">{fmt(it.price)}</span>
      <span className="tk-chg">{it.change == null ? '' : <>{it.change > 0 ? '▲' : it.change < 0 ? '▼' : '•'} {chg(it.change, it.pct)}</>}</span>
      <Spark pts={it.spark} cls={tone(it.change)} />
    </button>
  ))
  const current = open ? items.find((i) => i.key === open) : undefined
  return (
    <>
      <div className="mticker" role="region" aria-label="Market indices"
        onPointerEnter={(e) => { if (e.pointerType === 'mouse') mouseOn.current = true }} onPointerLeave={() => { mouseOn.current = false }}
        onFocus={() => { keyFocus.current = true }} onBlur={() => { keyFocus.current = false }}>
        <div className={'tk-badge ' + (nse ? 'on' : 'off')} title={nse ? 'NSE is open: prices are live' : 'NSE is closed: last session close and its change'}>
          <span className="tk-dot" /><span className="tk-bl">{nse ? 'LIVE' : 'CLOSED'}</span><span className="tk-bx">NSE</span>
        </div>
        <div className="tk-viewport">
          <div className="tk-track" ref={track}>{row(false)}{row(true)}</div>
        </div>
      </div>
      {current && <IndexChart item={current} onClose={close} />}
    </>
  )
}

const TFS = [['5m', '5 min'], ['15m', '15 min'], ['1D', '1 day'], ['1W', '1 week'], ['1M', '1 month']] as const
const DAILY = (tf: string) => tf === '1D' || tf === '1W' || tf === '1M'      // candles of a day or longer
const chartOf = (key: string, tf: string) => get<ChartData>(`/api/market/chart/${encodeURIComponent(key)}?tf=${tf}`)

/** The candle a tick belongs to, as the server writes candle times (IST wall clock encoded as UTC seconds): the day (1D) or the 5 / 15-minute slot
 * counted from 09:15, like Angel One's. null before the open. */
export function bucketTime(tickMs: number, tf: string): number | null {
  const d = new Date(tickMs + IST_MS)
  const y = d.getUTCFullYear(), mo = d.getUTCMonth(), day = d.getUTCDate()
  if (tf === '1D') return Date.UTC(y, mo, day) / 1000
  if (tf === '1M') return Date.UTC(y, mo, 1) / 1000
  if (tf === '1W') { const wd = (new Date(Date.UTC(y, mo, day)).getUTCDay() + 6) % 7; return Date.UTC(y, mo, day - wd) / 1000 }   // the week's Monday
  const size = tf === '15m' ? 15 : 5
  const mins = d.getUTCHours() * 60 + d.getUTCMinutes() - (9 * 60 + 15)
  if (mins < 0) return null
  const start = 9 * 60 + 15 + Math.floor(mins / size) * size
  return Date.UTC(y, mo, day, Math.floor(start / 60), start % 60) / 1000
}

/** The forming candle after one more tick: the server's last candle (or the one already being built) stretched to the price, or a new candle when
 * the tick opens the next slot. null when the tick is older than the server's last candle. */
export function liveBar(candles: Candle[], built: Candle | null, tick: { ltp: number; ts: number }, tf: string): Candle | null {
  const bucket = bucketTime(tick.ts, tf)
  const last = candles[candles.length - 1]
  if (bucket == null || !last || bucket < last.time) return null
  const base = bucket === last.time ? last : null
  const seed = built && built.time === bucket ? built : base
  if (!seed) return { time: bucket, open: tick.ltp, high: tick.ltp, low: tick.ltp, close: tick.ltp, volume: 0 }
  return { ...seed, open: (base ?? seed).open, high: Math.max(seed.high, base?.high ?? -Infinity, tick.ltp), low: Math.min(seed.low, base?.low ?? Infinity, tick.ltp), close: tick.ltp }
}
type Ind = { bb: boolean; vol: boolean; rsi: boolean }
interface Hover { c: Candle; rsi: number | null; bb: { mid: number; upper: number; lower: number } | null }

export function IndexChart({ item, onClose }: { item: TickerItem; onClose: () => void }) {
  const [tf, setTf] = useState<string>('5m')
  const [ind, setInd] = useState<Ind>({ bb: true, vol: true, rsi: true })
  // not available yet (Angel One busy, nothing kept on the server): asked again every 5 s instead of waiting for the usual 30 s / 5 min refresh
  const q = useQuery({ queryKey: ['market-chart', item.key, tf], queryFn: () => chartOf(item.key, tf), staleTime: 25_000, placeholderData: keepPreviousData,
    refetchInterval: (query) => (query.state.status === 'error' || !query.state.data?.candles.length || query.state.data?.partial ? 5000 : DAILY(tf) ? 300_000 : 30_000), retry: 1 })
  const box = useRef<HTMLDivElement>(null)
  const rbox = useRef<HTMLDivElement>(null)
  const [hover, setHover] = useState<Hover | null>(null)
  // live: every tick of the index (pushed, SSE) moves the forming candle up / down, and the next 5 / 15-minute slot opens a new candle
  const { ticks } = useLiveTicks([item.token ? `${item.exchange}:${item.token}` : null])
  const tick = item.token ? ticks[item.token] : undefined
  const ser = useRef<{ s: ISeriesApi<'Candlestick'>; bb: Partial<Record<'upper' | 'lower' | 'mid', ISeriesApi<'Line'>>> | null; r: ISeriesApi<'Line'> | null } | null>(null)
  const built = useRef<{ tf: string; bar: Candle } | null>(null)
  const view = useRef<{ tf: string; range: LogicalRange } | null>(null)        // the zoom / scroll a person chose survives the 30 s refresh
  const candles = useMemo(() => q.data?.candles ?? [], [q.data])
  const closes = useMemo(() => candles.map((k) => k.close), [candles])
  const bands = useMemo(() => bollinger(closes, 20, 2), [closes])
  const rs = useMemo(() => rsi(closes, 14), [closes])
  const hasVol = candles.some((k) => (k.volume ?? 0) > 0)
  const showVol = ind.vol && hasVol
  // the last session from the intraday candles: open, high, low (the range bar)
  const day = useMemo(() => {
    if (!candles.length) return null
    if (DAILY(tf)) { const k = candles[candles.length - 1]; return { open: k.open, high: k.high, low: k.low } }
    const lastDay = new Date(candles[candles.length - 1].time * 1000).toISOString().slice(0, 10)
    const today = candles.filter((k) => new Date(k.time * 1000).toISOString().slice(0, 10) === lastDay)
    return { open: today[0].open, high: Math.max(...today.map((k) => k.high)), low: Math.min(...today.map((k) => k.low)) }
  }, [candles, tf])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    document.body.style.overflow = 'hidden'
    return () => { window.removeEventListener('keydown', onKey); document.body.style.overflow = '' }
  }, [onClose])
  useEffect(() => {
    const el = box.current
    if (!el || !candles.length) return
    const css = getComputedStyle(document.documentElement)
    const v = (n: string, f: string) => css.getPropertyValue(n).trim() || f
    const line = v('--border', '#dfe4ec'), muted = v('--text-muted', '#66748a'), purple = v('--purple', '#6d4aff'), accent = v('--accent', '#1456f0')
    const up = v('--success', '#0a8a58'), down = v('--danger', '#d02d3c')
    // price on top (volume as a band at its bottom); RSI in its own pane below, on the same time axis
    const lowerPanes = showVol ? 0.16 : 0
    const base = {
      autoSize: true,
      layout: { background: { color: 'transparent' }, textColor: muted, fontFamily: v('--font-sans', 'system-ui'), fontSize: 11, attributionLogo: false },
      grid: { vertLines: { color: line }, horzLines: { color: line } },
    }
    const c = createChart(el, {
      ...base,
      rightPriceScale: { borderColor: line, minimumWidth: 78, scaleMargins: { top: 0.06, bottom: lowerPanes + 0.03 } },
      timeScale: { borderColor: line, timeVisible: !DAILY(tf), secondsVisible: false, rightOffset: 4, visible: !ind.rsi },
    })
    const t = (k: Candle) => k.time as UTCTimestamp
    const s = c.addCandlestickSeries({ upColor: up, downColor: down, wickUpColor: up, wickDownColor: down, borderVisible: false })
    const bbs: Partial<Record<'upper' | 'lower' | 'mid', ISeriesApi<'Line'>>> = {}
    let rsiLine: ISeriesApi<'Line'> | null = null
    s.setData(candles.map((k) => ({ time: t(k), open: k.open, high: k.high, low: k.low, close: k.close })))
    if (item.prev_close != null && !DAILY(tf)) s.createPriceLine({ price: item.prev_close, color: muted, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'Prev close' })
    if (ind.bb) {
      const band = (pick: 'upper' | 'lower' | 'mid', color: string, style: 0 | 2) => {
        const ls = c.addLineSeries({ color, lineWidth: 1, lineStyle: style, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false })
        ls.setData(candles.flatMap((k, i) => (bands[i] ? [{ time: t(k), value: bands[i]![pick] }] : [])))
        bbs[pick] = ls
      }
      band('upper', purple, 0)
      band('lower', purple, 0)
      band('mid', accent, 2)
    }
    if (showVol) {
      const vs = c.addHistogramSeries({ priceScaleId: 'vol', priceFormat: { type: 'volume' }, priceLineVisible: false, lastValueVisible: false })
      c.priceScale('vol').applyOptions({ scaleMargins: { top: 1 - lowerPanes, bottom: 0 } })
      vs.setData(candles.map((k) => ({ time: t(k), value: k.volume ?? 0, color: (k.close >= k.open ? up : down) + '88' })))
    }
    let rc: ReturnType<typeof createChart> | null = null
    if (ind.rsi && rbox.current) {
      rc = createChart(rbox.current, {
        ...base,
        rightPriceScale: { borderColor: line, minimumWidth: 78, scaleMargins: { top: 0.1, bottom: 0.1 } },
        timeScale: { borderColor: line, timeVisible: !DAILY(tf), secondsVisible: false, rightOffset: 4 },
        handleScroll: false, handleScale: false,
      })
      const r = rc.addLineSeries({ color: purple, lineWidth: 1, priceLineVisible: false, lastValueVisible: true,
        autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }) })
      rsiLine = r
      // whitespace before the first RSI value keeps both panes on the same bar index
      r.setData(candles.map((k, i) => (rs[i] != null ? { time: t(k), value: rs[i]! } : { time: t(k) })))
      r.createPriceLine({ price: 70, color: down, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '' })
      r.createPriceLine({ price: 30, color: up, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '' })
      const rsiChart = rc
      c.timeScale().subscribeVisibleLogicalRangeChange((range) => { if (range) rsiChart.timeScale().setVisibleLogicalRange(range) })
    }
    const at = new Map(candles.map((k, i) => [k.time, i]))
    c.subscribeCrosshairMove((p) => {
      const i = p.time != null ? at.get(p.time as number) : undefined
      setHover(i == null ? null : { c: candles[i], rsi: rs[i], bb: bands[i] })
    })
    if (view.current?.tf === tf) c.timeScale().setVisibleLogicalRange(view.current.range)
    else c.timeScale().fitContent()
    ser.current = { s, bb: ind.bb ? bbs : null, r: rsiLine }
    paintLive()                                                                  // the candle built from ticks since the last refresh
    return () => {
      const r = c.timeScale().getVisibleLogicalRange()
      if (r) view.current = { tf, range: r }
      ser.current = null
      c.remove(); rc?.remove()
    }
  }, [candles, tf, item.prev_close, ind.bb, ind.rsi, showVol, bands, rs]) // eslint-disable-line react-hooks/exhaustive-deps

  // draws built.current (the forming candle) and the indicators' last points; the rest of the chart stays as the server gave it
  function paintLive() {
    const sr = ser.current, b = built.current?.tf === tf ? built.current.bar : null
    const last = candles[candles.length - 1]
    if (!sr || !b || !last || b.time < last.time) return
    const t = b.time as UTCTimestamp
    sr.s.update({ time: t, open: b.open, high: b.high, low: b.low, close: b.close })
    const cl = b.time === last.time ? [...closes.slice(0, -1), b.close] : [...closes, b.close]
    const x = sr.bb ? bollinger(cl, 20, 2)[cl.length - 1] : null
    if (sr.bb && x) for (const k of ['upper', 'lower', 'mid'] as const) sr.bb[k]?.update({ time: t, value: x[k] })
    if (sr.r) { const v = rsi(cl, 14)[cl.length - 1]; sr.r.update(v != null ? { time: t, value: v } : { time: t }) }
  }
  useEffect(() => {
    if (!tick || !fresh(tick) || !candles.length) return
    const bar = liveBar(candles, built.current?.tf === tf ? built.current.bar : null, tick, tf)
    if (!bar) return
    built.current = { tf, bar }
    paintLive()
  }, [tick?.ltp, tick?.ts, candles, tf]) // eslint-disable-line react-hooks/exhaustive-deps
  const li = candles.length - 1
  const lb = built.current?.tf === tf && li >= 0 && built.current.bar.time >= candles[li].time ? built.current.bar : null   // the forming candle, live
  const shown: Hover | null = hover ?? (li >= 0 ? { c: lb ?? candles[li], rsi: rs[li], bb: bands[li] } : null)
  const pos = day && item.price != null && day.high > day.low ? Math.min(100, Math.max(0, ((item.price - day.low) / (day.high - day.low)) * 100)) : null
  const toggle = (k: keyof Ind) => setInd((x) => ({ ...x, [k]: !x[k] }))
  return (
    <div className="tk-modal" role="dialog" aria-modal="true" aria-label={`${item.label} chart`} onClick={onClose}>
      <div className="tk-sheet" onClick={(e) => e.stopPropagation()}>
        <div className="tk-head">
          <div className="tk-id">
            <div className="tk-ex">{item.exchange} · INDEX {item.live ? <span className="tk-livepill"><span className="tk-dot" />LIVE</span> : <span className="tk-closedpill">CLOSED</span>}</div>
            <div className="tk-title">{item.label}</div>
          </div>
          <div className={'tk-quote ' + tone(item.change)}>
            <div className="tk-big">{fmt(item.price)}</div>
            <div className="tk-chg">{item.change == null ? 'previous close loading…' : <>{item.change > 0 ? '▲' : item.change < 0 ? '▼' : ''} {chg(item.change, item.pct)} <span className="tk-today">{sessionLabel(item)}</span></>}</div>
          </div>
          <button className="tk-close" onClick={onClose} aria-label="Close chart">✕</button>
        </div>
        <div className="tk-stats">
          <div><span>Open</span><b>{fmt(day?.open)}</b></div>
          <div><span>High</span><b>{fmt(day?.high)}</b></div>
          <div><span>Low</span><b>{fmt(day?.low)}</b></div>
          <div><span>Prev close</span><b>{fmt(item.prev_close)}</b></div>
          <div className="tk-range">
            <span>Day range</span>
            <div className="tk-rbar"><i style={{ left: pos == null ? '50%' : `${pos}%`, opacity: pos == null ? 0 : 1 }} /></div>
            <div className="tk-rlab"><em>{fmt(day?.low)}</em><em>{fmt(day?.high)}</em></div>
          </div>
        </div>
        <div className="tk-tabs" role="tablist">
          {TFS.map(([k, l]) => <button key={k} role="tab" aria-selected={tf === k} className={tf === k ? 'on' : ''} onClick={() => setTf(k)}>{l}</button>)}
          <span className="tk-sep" />
          <button className={'tk-ind bb' + (ind.bb ? ' on' : '')} onClick={() => toggle('bb')} aria-pressed={ind.bb}>BB 20,2</button>
          <button className={'tk-ind vol' + (ind.vol ? ' on' : '')} onClick={() => toggle('vol')} aria-pressed={ind.vol}>Volume</button>
          <button className={'tk-ind rsi' + (ind.rsi ? ' on' : '')} onClick={() => toggle('rsi')} aria-pressed={ind.rsi}>RSI 14</button>
        </div>
        {shown && (
          <div className={'tk-ohlc ' + tone(shown.c.close - shown.c.open)}>
            O <b>{fmt(shown.c.open)}</b> H <b>{fmt(shown.c.high)}</b> L <b>{fmt(shown.c.low)}</b> C <b>{fmt(shown.c.close)}</b>
            {ind.bb && shown.bb && <span className="tk-lg bb">BB <b>{fmt(shown.bb.upper)}</b> / <b>{fmt(shown.bb.mid)}</b> / <b>{fmt(shown.bb.lower)}</b></span>}
            {ind.vol && hasVol && <span className="tk-lg">Vol <b>{fmt(shown.c.volume ?? 0, 0)}</b></span>}
            {ind.rsi && shown.rsi != null && <span className={'tk-lg rsi' + (shown.rsi >= 70 ? ' hi' : shown.rsi <= 30 ? ' lo' : '')}>RSI <b>{fmt(shown.rsi)}</b></span>}
          </div>
        )}
        <div className={'tk-chart' + (ind.rsi ? ' with-rsi' : '')} ref={box}>
          {q.isLoading && <div className="tk-msg"><span className="tk-spin" />Loading chart…</div>}
          {q.isError && <div className="tk-msg">Chart is not available right now (Angel One did not answer). Trying again every 5 seconds…</div>}
          {q.data?.partial && candles.length > 0 && <div className="tk-partial" role="status"><span className="tk-spin" />Earlier candles are still loading from Angel One (busy in market hours) · today&apos;s candles are from live ticks</div>}
          {q.data && !candles.length && <div className="tk-msg">No candles for this period.</div>}
        </div>
        {ind.rsi && <div className="tk-rsi" ref={rbox}><span className="tk-rsitag">RSI 14</span></div>}
        <div className="tk-foot">
          Data: Angel One · {DAILY(tf) ? `${tf === '1D' ? 'daily' : tf === '1W' ? 'weekly' : 'monthly'} candles` : `${tf} candles, refreshed every 30 s`} · display only, never traded · charts: TradingView Lightweight Charts
          {ind.vol && !hasVol && candles.length > 0 && <> · <b>Volume:</b> an index has no traded volume (the exchange does not publish one)</>}
        </div>
      </div>
    </div>
  )
}
