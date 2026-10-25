import { createChart, type IChartApi, type UTCTimestamp } from 'lightweight-charts'
import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { get } from '../../lib/api'
import { PAGE_FRIENDLY_CHART } from '../../lib/chartScroll'
import { cssVar, useTheme } from '../../lib/theme'
import { n0 } from '../../lib/format'
import type { BtMetrics } from './ResultView'
import './builder.css'

interface Row { id: string; created?: string; name: string; symbol?: string; range: string; trades: number; total: number; max_drawdown: number | null; win_rate: number | null }
interface Run { id: string; strategy: Record<string, unknown> & { name: string; legs?: Leg[]; charges?: Record<string, number> }; metrics: BtMetrics; skipped: number }
interface Leg { side: string; lots: number; opt: string; strike: { mode: string; offset?: number; value?: number }; sl?: { value: number; type: string }; target?: { value: number; type: string }; trail_pct?: number }
interface Compare { runs: Run[]; differences: string[]; same_range: boolean }

const rs = (v: number | null | undefined) => (v == null ? '–' : <span className={v >= 0 ? 'pos' : 'neg'}>₹{n0(Math.round(v) || 0)}</span>)
const COLS = ['#2563eb', '#16a34a', '#d97706', '#7c3aed']

function legsText(ls: Leg[] | undefined) {
  return (ls ?? []).map((l) => `${l.side} ${l.lots}×${l.opt} ${l.strike.mode}${l.strike.offset ? (l.strike.offset > 0 ? '+' : '') + l.strike.offset : ''}${l.strike.value ? ' ' + l.strike.value : ''}${l.sl ? ` SL ${l.sl.value}${l.sl.type === 'pct' ? '%' : 'pts'}` : ''}${l.target ? ` T ${l.target.value}${l.target.type === 'pct' ? '%' : 'pts'}` : ''}${l.trail_pct ? ` trail ${l.trail_pct}%` : ''}`).join(' | ')
}

function CompareChart({ runs }: { runs: Run[] }) {
  const host = useRef<HTMLDivElement>(null)
  const { dark } = useTheme()
  useEffect(() => {
    if (!host.current) return
    const ch: IChartApi = createChart(host.current, { ...PAGE_FRIENDLY_CHART, autoSize: true, height: 280, layout: { background: { color: 'transparent' }, textColor: cssVar('--text-secondary') }, grid: { vertLines: { visible: false }, horzLines: { color: 'rgba(128,128,128,.15)' } } })
    runs.forEach((r, i) => {
      if (!r.metrics.trades) return
      const by: Record<string, number> = {}
      r.metrics.equity.forEach((p) => { by[p.date] = p.equity })
      ch.addLineSeries({ color: COLS[i], lineWidth: 2, title: r.strategy.name }).setData(Object.entries(by).map(([t, v]) => ({ time: t as unknown as UTCTimestamp, value: v })))
    })
    ch.timeScale().fitContent()
    return () => ch.remove()
  }, [runs, dark])
  return <div ref={host} style={{ height: 280 }} />
}

export function HistoryView() {
  const nav = useNavigate()
  const q = useQuery({ queryKey: ['bt', 'history'], queryFn: () => get<Row[]>('/api/options/backtests') })
  const [sel, setSel] = useState<string[]>([])
  const [cmp, setCmp] = useState<Compare | null>(null)
  const [err, setErr] = useState('')
  const toggle = (id: string) => setSel((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]))
  const compare = async () => {
    setErr('')
    try { setCmp(await get<Compare>(`/api/options/backtests/compare?ids=${sel.join(',')}`)) } catch (e) { setErr((e as Error).message) }
  }
  const R = cmp?.runs ?? []
  const M = (label: string, f: (m: BtMetrics) => React.ReactNode) => <tr><td className="l">{label}</td>{R.map((r) => <td key={r.id}>{r.metrics.trades ? f(r.metrics) : '–'}</td>)}</tr>
  return (
    <div className="page">
      <h1>Backtest history</h1>
      <div className="card">
        <div className="row" style={{ alignItems: 'center', marginBottom: 8 }}>
          <button className="btn primary" disabled={sel.length < 2 || sel.length > 4} onClick={() => void compare()}>Compare selected</button>
          <span className="sub">{sel.length ? `${sel.length} selected (2-4)` : 'select 2-4 runs to compare'}</span>
        </div>
        <div className="scroll">
          <table>
            <thead><tr><th className="l">Cmp</th><th className="l">Created</th><th className="l">Strategy</th><th className="l">Instrument</th><th className="l">Range</th><th>Trades</th><th>Net P&amp;L</th><th>Max DD</th><th>Win rate</th><th /></tr></thead>
            <tbody>
              {(q.data ?? []).map((x) => (
                <tr key={x.id}>
                  <td className="l"><input type="checkbox" checked={sel.includes(x.id)} onChange={() => toggle(x.id)} /></td>
                  <td className="l">{x.created ?? x.id}</td><td className="l">{x.name}</td><td className="l">{x.symbol ?? '–'}</td><td className="l">{x.range}</td><td>{x.trades}</td><td>{rs(x.total)}</td>
                  <td>{x.max_drawdown == null ? '–' : rs(x.max_drawdown)}</td><td>{x.win_rate == null ? '–' : `${x.win_rate}%`}</td>
                  <td><button className="btn sm" onClick={() => nav('/backtesting', { state: { open: x.id } })}>Open</button></td>
                </tr>
              ))}
              {!q.data?.length && <tr><td colSpan={10} className="empty">{q.isLoading ? 'Loading…' : 'No saved backtests yet. Run one on the Backtesting page.'}</td></tr>}
            </tbody>
          </table>
        </div>
        {q.error && <div className="alert" style={{ marginTop: 8 }}>Could not load history: {(q.error as Error).message}</div>}
      </div>
      {err && <div className="alert">{err}</div>}
      {cmp && (
        <div className="card">
          <h3>Comparison</h3>
          <p className="sub">Metrics recomputed from each run&apos;s stored trades with the same definitions. No run is ranked: read profit together with drawdown, win rate and trade count.{!cmp.same_range && <b className="warn"> Date ranges differ, so totals are not directly comparable.</b>}</p>
          <div className="scroll">
            <table>
              <thead><tr><th className="l">Metric</th>{R.map((r, i) => <th key={r.id}><span style={{ color: COLS[i] }}>■</span> {r.strategy.name}<div className="sub">{r.id}</div></th>)}</tr></thead>
              <tbody>
                {M('Net P&L', (m) => rs(m.total))}{M('Gross P&L', (m) => rs(m.gross))}{M('Charges', (m) => `₹${n0(m.charges)}`)}{M('Max drawdown', (m) => rs(m.max_drawdown))}
                {M('Trades', (m) => m.trades)}{M('Win rate', (m) => `${m.win_rate}%`)}{M('Avg trade', (m) => rs(m.avg_trade))}{M('Profit factor', (m) => m.profit_factor ?? 'n/a')}
                {M('Largest win / loss', (m) => <>{rs(m.best)} / {rs(m.worst)}</>)}
                <tr><td className="l">Skipped expiries</td>{R.map((r) => <td key={r.id}>{r.skipped}</td>)}</tr>
              </tbody>
            </table>
          </div>
          <h3 style={{ marginTop: 14 }}>Configuration differences</h3>
          {cmp.differences.length ? (
            <table>
              <thead><tr><th className="l">Setting</th>{R.map((r) => <th key={r.id} className="l">{r.id}</th>)}</tr></thead>
              <tbody>{cmp.differences.map((k) => <tr key={k}><td className="l">{k}</td>{R.map((r) => <td key={r.id} className="l wrapcell">{k === 'legs' ? legsText(r.strategy.legs) : k === 'charges' ? Object.entries(r.strategy.charges ?? {}).map(([a, b]) => `${a}=${b}`).join(', ') : String(r.strategy[k] ?? '–')}</td>)}</tr>)}</tbody>
            </table>
          ) : <p className="sub">Identical configuration.</p>}
          <h3 style={{ marginTop: 14 }}>Cumulative net P&amp;L</h3>
          <CompareChart runs={R} />
        </div>
      )}
    </div>
  )
}
