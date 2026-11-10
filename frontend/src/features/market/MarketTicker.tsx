import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { createChart, type UTCTimestamp } from 'lightweight-charts'
import { useEffect, useMemo, useRef, useState } from 'react'
import { get } from '../../lib/api'
import { bollinger, rsi } from './indicators'
import './market.css'

export interface TickerItem {
  key: string; label: string; exchange: string; price: number | null; prev_close: number | null; change: number | null; pct: number | null; live: boolean; spark?: number[]
  session?: string | null; today?: boolean
}
interface TickerData { items: TickerItem[]; nse_open: boolean; mcx_open: boolean }
interface Candle { time: number; open: number; high: number; low: number; close: number; volume?: number }
interface ChartData { key: string; label: string; tf: string; candles: Candle[] }

const fmt = (v: number | null | undefined, d = 2) => (v == null ? '–' : v.toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d }))
const sgn = (v: number | null | undefined) => (v == null ? '' : v > 0 ? '+' : v < 0 ? '−' : '')
const tone = (v: number | null | undefined) => (v == null || v === 0 ? 'flat' : v > 0 ? 'up' : 'down')
const chg = (c: number | null, p: number | null) => (c == null ? '' : `${sgn(c)}${fmt(Math.abs(c))} (${sgn(p)}${fmt(Math.abs(p ?? 0))}%)`)

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

/** The scrolling market bar under the top bar: index prices and day change (display only, never part of a strategy). A click opens the index's chart. */
export function MarketTicker() {
  const q = useQuery({ queryKey: ['market-ticker'], queryFn: () => get<TickerData>('/api/market/ticker'), refetchInterval: 2000, retry: false })
  const items = useMemo(() => q.data?.items ?? [], [q.data])
  const flash = useFlash(items)
  const [open, setOpen] = useState<string | null>(null)
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
      <div className="mticker" role="region" aria-label="Market indices">
        <div className={'tk-badge ' + (nse ? 'on' : 'off')} title={nse ? 'NSE is open: prices are live' : 'NSE is closed: last session close and its change'}>
          <span className="tk-dot" /><span className="tk-bl">{nse ? 'LIVE' : 'CLOSED'}</span><span className="tk-bx">NSE</span>
        </div>
        <div className="tk-viewport">
          <div className="tk-track" style={{ animationDuration: `${Math.max(45, items.length * 5)}s` }}>{row(false)}{row(true)}</div>
        </div>
      </div>
      {current && <IndexChart item={current} onClose={close} />}
    </>
  )
}

const TFS = [['5m', '5 min'], ['15m', '15 min'], ['1D', '1 day']] as const
const chartOf = (key: string, tf: string) => get<ChartData>(`/api/market/chart/${encodeURIComponent(key)}?tf=${tf}`)
type Ind = { bb: boolean; vol: boolean; rsi: boolean }
interface Hover { c: Candle; rsi: number | null; bb: { mid: number; upper: number; lower: number } | null }

function IndexChart({ item, onClose }: { item: TickerItem; onClose: () => void }) {
  const [tf, setTf] = useState<string>('5m')
  const [ind, setInd] = useState<Ind>({ bb: true, vol: true, rsi: true })
  const q = useQuery({ queryKey: ['market-chart', item.key, tf], queryFn: () => chartOf(item.key, tf), staleTime: 25_000, placeholderData: keepPreviousData,
    refetchInterval: tf === '1D' ? 300_000 : 30_000, retry: 1 })
  const box = useRef<HTMLDivElement>(null)
  const rbox = useRef<HTMLDivElement>(null)
  const [hover, setHover] = useState<Hover | null>(null)
  const candles = useMemo(() => q.data?.candles ?? [], [q.data])
  const closes = useMemo(() => candles.map((k) => k.close), [candles])
  const bands = useMemo(() => bollinger(closes, 20, 2), [closes])
  const rs = useMemo(() => rsi(closes, 14), [closes])
  const hasVol = candles.some((k) => (k.volume ?? 0) > 0)
  const showVol = ind.vol && hasVol
  // the last session from the intraday candles: open, high, low (the range bar)
  const day = useMemo(() => {
    if (!candles.length) return null
    if (tf === '1D') { const k = candles[candles.length - 1]; return { open: k.open, high: k.high, low: k.low } }
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
      timeScale: { borderColor: line, timeVisible: tf !== '1D', secondsVisible: false, rightOffset: 4, visible: !ind.rsi },
    })
    const t = (k: Candle) => k.time as UTCTimestamp
    const s = c.addCandlestickSeries({ upColor: up, downColor: down, wickUpColor: up, wickDownColor: down, borderVisible: false })
    s.setData(candles.map((k) => ({ time: t(k), open: k.open, high: k.high, low: k.low, close: k.close })))
    if (item.prev_close != null && tf !== '1D') s.createPriceLine({ price: item.prev_close, color: muted, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'Prev close' })
    if (ind.bb) {
      const band = (pick: 'upper' | 'lower' | 'mid', color: string, style: 0 | 2) => {
        const ls = c.addLineSeries({ color, lineWidth: 1, lineStyle: style, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false })
        ls.setData(candles.flatMap((k, i) => (bands[i] ? [{ time: t(k), value: bands[i]![pick] }] : [])))
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
        timeScale: { borderColor: line, timeVisible: tf !== '1D', secondsVisible: false, rightOffset: 4 },
        handleScroll: false, handleScale: false,
      })
      const r = rc.addLineSeries({ color: purple, lineWidth: 1, priceLineVisible: false, lastValueVisible: true,
        autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }) })
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
    c.timeScale().fitContent()
    return () => { c.remove(); rc?.remove() }
  }, [candles, tf, item.prev_close, ind.bb, ind.rsi, showVol, bands, rs])
  const li = candles.length - 1
  const shown: Hover | null = hover ?? (li >= 0 ? { c: candles[li], rsi: rs[li], bb: bands[li] } : null)
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
          {q.isError && <div className="tk-msg">Chart is not available right now (Angel One did not answer). It will try again in a moment.</div>}
          {q.data && !candles.length && <div className="tk-msg">No candles for this period.</div>}
        </div>
        {ind.rsi && <div className="tk-rsi" ref={rbox}><span className="tk-rsitag">RSI 14</span></div>}
        <div className="tk-foot">
          Data: Angel One · {tf === '1D' ? 'daily candles' : `${tf} candles, refreshed every 30 s`} · display only, never traded · charts: TradingView Lightweight Charts
          {ind.vol && !hasVol && candles.length > 0 && <> · <b>Volume:</b> an index has no traded volume (the exchange does not publish one)</>}
        </div>
      </div>
    </div>
  )
}
