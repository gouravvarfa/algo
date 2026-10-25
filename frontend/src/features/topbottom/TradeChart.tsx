import { createChart, type IChartApi, type IPriceLine, type ISeriesApi, type SeriesMarker, type Time, type UTCTimestamp } from 'lightweight-charts'
import { useEffect, useRef, useState } from 'react'
import { PAGE_FRIENDLY_CHART } from '../../lib/chartScroll'
import { cssVar } from '../../lib/theme'
import { money, n2, num } from '../../lib/format'
import type { Snapshot } from '../../lib/types'

type Kind = 'line' | 'candle'

/** The live chart: close line (or candles), BUY/SELL arrows, roll marker, the latest TOP and BOTTOM, and two dashed horizontal lines for the
 * OPEN position only: entry (green) and stop-loss (red, with the rupee loss if it is hit). The chart is rebuilt when the theme changes. */
export function TradeChart({ snap, kind, fitSignal }: { snap: Snapshot; kind: Kind; fitSignal: number }) {
  const host = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const line = useRef<ISeriesApi<'Line'> | null>(null)
  const candle = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const levels = useRef<{ series: ISeriesApi<'Line'> | ISeriesApi<'Candlestick'>; pl: IPriceLine }[]>([])
  const [themeTick, setThemeTick] = useState(0)
  const [ohlc, setOhlc] = useState('')
  const fitted = useRef(false)

  useEffect(() => {
    const on = () => setThemeTick((t) => t + 1)
    window.addEventListener('themechange', on)
    return () => window.removeEventListener('themechange', on)
  }, [])

  // (re)build the chart on mount and on theme change
  useEffect(() => {
    if (!host.current) return
    const c = { bg: cssVar('--bg-surface'), text: cssVar('--text-secondary'), grid: cssVar('--border'), up: cssVar('--success'), down: cssVar('--danger') }
    const ch = createChart(host.current, {
      ...PAGE_FRIENDLY_CHART,
      autoSize: true,
      layout: { background: { color: c.bg }, textColor: c.text, fontFamily: cssVar('--font-sans'), fontSize: 11 },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      crosshair: { mode: 0 },
      timeScale: { timeVisible: false, borderColor: c.grid, rightOffset: 6 },
      rightPriceScale: { borderColor: c.grid, scaleMargins: { top: 0.08, bottom: 0.08 } },
      localization: { priceFormatter: (p: number) => p.toLocaleString('en-IN', { maximumFractionDigits: 0 }) },
    })
    line.current = ch.addLineSeries({ color: cssVar('--accent'), lineWidth: 2, lastValueVisible: true, priceLineVisible: true, priceLineStyle: 2 })
    candle.current = ch.addCandlestickSeries({ upColor: c.up, downColor: c.down, borderUpColor: c.up, borderDownColor: c.down, wickUpColor: c.up, wickDownColor: c.down, priceLineStyle: 2, visible: false })
    ch.subscribeCrosshairMove((p) => {
      const c = p.seriesData.get(candle.current!) as { open: number; high: number; low: number; close: number } | undefined
      const l = p.seriesData.get(line.current!) as { value: number } | undefined
      if (c && 'open' in c) setOhlc(`O ${n2(c.open)}  H ${n2(c.high)}  L ${n2(c.low)}  C ${n2(c.close)}`)
      else if (l) setOhlc(`Close ${n2(l.value)}`)
      else setOhlc('')
    })
    chart.current = ch
    levels.current = []
    fitted.current = false
    return () => { ch.remove(); chart.current = null }
  }, [themeTick])

  // data, markers, levels
  useEffect(() => {
    const ch = chart.current, ls = line.current, cs = candle.current
    if (!ch || !ls || !cs) return
    ls.setData(snap.candles.map((k) => ({ time: k.time as UTCTimestamp, value: k.close })))
    cs.setData(snap.candles.map((k) => ({ time: k.time as UTCTimestamp, open: k.open, high: k.high, low: k.low, close: k.close })))
    ls.applyOptions({ visible: kind === 'line' })
    cs.applyOptions({ visible: kind === 'candle' })

    // only trades, the roll and the System Points the rules use now (the latest TOP and the latest BOTTOM); other events live in the tables
    const pts = snap.system_points ?? []
    const last = (k: 'TOP' | 'BOTTOM') => pts.filter((p) => p.kind === k).slice(-1)
    const sp: SeriesMarker<Time>[] = [...last('TOP'), ...last('BOTTOM')].map((p) => ({
      time: p.time as UTCTimestamp, position: p.kind === 'TOP' ? 'aboveBar' : 'belowBar', shape: 'circle', size: 0.6, color: cssVar('--warning'), text: `${p.kind === 'TOP' ? 'TOP' : 'BOTTOM'} ${num(p.price)}`,
    }))
    const trades: SeriesMarker<Time>[] = snap.markers
      .filter((m) => /^(BUY|SELL) |^ROLL [A-Z]{3}→/.test(m.text))
      .map((m) => ({ time: m.time as UTCTimestamp, position: m.position, shape: m.shape as SeriesMarker<Time>['shape'], color: m.color, text: m.text.replace(/\.\d+$/, '') }))
    const markers = [...trades, ...sp].sort((a, b) => (a.time as number) - (b.time as number))
    ls.setMarkers(kind === 'line' ? markers : [])
    cs.setMarkers(kind === 'candle' ? markers : [])

    for (const { series, pl } of levels.current) { try { series.removePriceLine(pl) } catch { /* chart rebuilt */ } }
    levels.current = []
    const p = snap.position
    if (p) {
      const loss = Math.abs(p.entry_price - p.active_stop) * snap.instrument.quantity
      const worse = p.direction === 'BUY' ? p.active_stop < p.entry_price : p.active_stop > p.entry_price
      for (const s of [ls, cs]) {
        levels.current.push({ series: s, pl: s.createPriceLine({ price: p.entry_price, color: cssVar('--success'), lineWidth: 2, lineStyle: 2, axisLabelVisible: true, title: `ENTRY ${p.direction} ${num(p.entry_price)}` }) })
        levels.current.push({ series: s, pl: s.createPriceLine({ price: p.active_stop, color: cssVar('--danger'), lineWidth: 2, lineStyle: 2, axisLabelVisible: true, title: worse ? `STOP ${num(p.active_stop)} · loss ${money(loss)}` : `STOP ${num(p.active_stop)} · locks +${money(loss)}` }) })
      }
    }
    if (!fitted.current && snap.candles.length) {
      ch.timeScale().setVisibleLogicalRange({ from: Math.max(0, snap.candles.length - 45), to: snap.candles.length + 6 })
      fitted.current = true
    }
  }, [snap, kind, themeTick])

  useEffect(() => {
    if (fitSignal && chart.current) chart.current.timeScale().setVisibleLogicalRange({ from: Math.max(0, snap.candles.length - 45), to: snap.candles.length + 6 })
  }, [fitSignal]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="chartbox">
      <div ref={host} className="chartcanvas" />
      {ohlc && <div className="ohlc num">{ohlc}</div>}
    </div>
  )
}
