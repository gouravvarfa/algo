import { createChart, type IChartApi, type UTCTimestamp } from 'lightweight-charts'
import { useEffect, useMemo, useRef, useState } from 'react'
import { PAGE_FRIENDLY_CHART } from '../../lib/chartScroll'
import { cssVar, useTheme } from '../../lib/theme'
import { n0, num, sign } from '../../lib/format'

export interface BtLeg { n: number; side: string; opt: string; strike: number; lots: number; lot_size: number; qty: number; entry: number; exit: number; exit_day: string; reason: string; pnl: number; entry_day?: string; expiry?: string }
export interface BtTrade { expiry: string; entry_day: string; exit_day: string; und_entry: number; und_exit: number; vix_entry?: number | null; legs: BtLeg[]; gross: number; charges: number; net: number; flags: string[] }
export interface BtMetrics {
  trades: number; total: number; gross: number; charges: number; wins?: number; losses?: number; win_rate: number; avg_trade?: number; avg_win: number; avg_loss: number; best: number; worst: number
  profit_factor?: number | null; max_drawdown: number; expectancy?: number; expectancy_ratio?: number | null; max_consecutive_wins?: number; max_consecutive_losses?: number
  per_trade?: { expiry: string; exit_day: string; net: number }[]; monthly: Record<string, number>; weekday: Record<string, { trades: number; pnl: number }>
  yearly?: { period: string; trades: number; wins: number; gross: number; charges: number; net: number }[]; daily?: { period: string; trades: number; wins: number; net: number }[]
  equity: { date: string; equity: number; drawdown: number }[]; exit_reasons?: Record<string, number>
}
export interface BtResult { id: string; strategy: { name: string; symbol: string; start: string; end: string }; metrics: BtMetrics; trades: BtTrade[]; skipped: { expiry: string; reason: string }[]; note: string }

const rs = (v: number | null | undefined) => (v == null ? '–' : <span className={v >= 0 ? 'pos' : 'neg'}>₹{n0(Math.round(v) || 0)}</span>)

function EquityChart({ metrics }: { metrics: BtMetrics }) {
  const host = useRef<HTMLDivElement>(null)
  const { dark } = useTheme()
  useEffect(() => {
    if (!host.current) return
    const ch: IChartApi = createChart(host.current, { ...PAGE_FRIENDLY_CHART, autoSize: true, height: 280, layout: { background: { color: 'transparent' }, textColor: cssVar('--text-secondary') }, grid: { vertLines: { visible: false }, horzLines: { color: 'rgba(128,128,128,.15)' } } })
    const by: Record<string, BtMetrics['equity'][number]> = {}
    metrics.equity.forEach((p) => { by[p.date] = p })
    const pts = Object.values(by)
    ch.addAreaSeries({ lineColor: cssVar('--accent'), topColor: 'rgba(37,99,235,.25)', bottomColor: 'rgba(37,99,235,0)' }).setData(pts.map((p) => ({ time: p.date as unknown as UTCTimestamp, value: p.equity })))
    ch.addHistogramSeries({ color: 'rgba(220,38,38,.45)', priceScaleId: 'dd' }).setData(pts.map((p) => ({ time: p.date as unknown as UTCTimestamp, value: p.drawdown })))
    ch.priceScale('dd').applyOptions({ scaleMargins: { top: 0.7, bottom: 0 } })
    ch.timeScale().fitContent()
    return () => ch.remove()
  }, [metrics, dark])
  return <div ref={host} style={{ height: 280 }} />
}

function Bars({ vals, tips, signVals, neutral }: { vals: number[]; tips: string[]; signVals?: number[]; neutral?: boolean }) {
  const ref = useRef<HTMLDivElement>(null)
  const [W, setW] = useState(500)
  useEffect(() => { if (ref.current) setW(ref.current.clientWidth || 500) }, [])
  const H = 200, L = 52, B = 18, T = 8
  const mx = Math.max(0, ...vals), mn = Math.min(0, ...vals), rg = mx - mn || 1
  const Y = (v: number) => T + ((mx - v) / rg) * (H - T - B)
  const bw = (W - L - 6) / vals.length
  return (
    <div ref={ref}>
      <svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`}>
        {[mx, 0, mn].map((v, i) => <g key={i}><line x1={L} x2={W} y1={Y(v)} y2={Y(v)} className="gridline" /><text x={L - 5} y={Y(v) + 3} textAnchor="end" className="ct">{v === 0 ? '0' : neutral ? n0(v) : '₹' + n0(v)}</text></g>)}
        {vals.map((v, i) => {
          const sv = signVals ? signVals[i] : v
          return <rect key={i} x={L + i * bw + bw * 0.12} width={Math.max(1, bw * 0.76)} y={Math.min(Y(v), Y(0))} height={Math.max(1, Math.abs(Y(v) - Y(0)))} style={{ fill: sv >= 0 ? 'var(--success)' : 'var(--danger)', fillOpacity: 0.8 }}><title>{tips[i]}{neutral ? '' : `: ₹${n0(v)}`}</title></rect>
        })}
      </svg>
    </div>
  )
}

const METRIC_DEFS = (
  <>
    <b>Expectancy</b> = win% × avg win + loss% × avg loss = average net per trade. <b>Expectancy ratio</b> = expectancy ÷ |avg loss|. <b>Consecutive wins / losses</b> = longest run in exit order. <b>Daily / yearly</b> tables group trades by exit date. <b>Trade</b> = one expiry cycle (all legs). <b>Net</b> = gross − charges. <b>Win</b> = net &gt; 0; a trade with net ≤ 0 counts as a loss. <b>Win rate</b> = wins ÷ trades. <b>Avg win / loss</b> = mean net of winning / losing trades. <b>Avg trade</b> = net P&amp;L ÷ trades. <b>Profit factor</b> = sum of wins ÷ |sum of losses| (n/a when there are no losing trades). <b>Max drawdown</b> = largest fall of cumulative net P&amp;L from its previous peak, in exit order. No return-on-capital is shown: margin/capital is not modelled, so there is no honest denominator.
  </>
)

export function downloadTradesCsv(r: BtResult) {
  const rows: (string | number | null)[][] = [['expiry', 'entry_day', 'exit_day', 'leg', 'side', 'opt', 'strike', 'lots', 'lot_size', 'qty', 'entry', 'exit', 'leg_exit_day', 'reason', 'leg_pnl', 'trade_gross', 'trade_charges', 'trade_net', 'flags']]
  r.trades.forEach((t) => t.legs.forEach((l) => rows.push([t.expiry, t.entry_day, t.exit_day, l.n, l.side, l.opt, l.strike, l.lots, l.lot_size, l.qty, l.entry, l.exit, l.exit_day, l.reason, l.pnl, t.gross, t.charges, t.net, t.flags.join(' | ')])))
  const blob = new Blob([rows.map((x) => x.map((v) => `"${String(v ?? '').replace(/"/g, '""')}"`).join(',')).join('\n')], { type: 'text/csv' })
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = `backtest_${r.id}.csv`; a.click()
}

export function ResultView({ r }: { r: BtResult }) {
  const m = r.metrics, s = r.strategy
  const [filter, setFilter] = useState('')
  const trades = useMemo(() => r.trades.filter((t) => !filter || (filter === 'win' && t.net > 0) || (filter === 'loss' && t.net <= 0) || (filter === 'sl' && t.legs.some((l) => l.reason.startsWith('SL') || l.reason.startsWith('OVERALL SL'))) || (filter === 'flag' && t.flags.length > 0)), [r.trades, filter])
  if (!m.trades) return <div className="card">No trades. Skipped: {r.skipped.map((x) => `${x.expiry} – ${x.reason}`).join('; ') || 'none'}</div>
  const per = m.per_trade ?? [...r.trades].sort((a, b) => (a.exit_day + a.expiry).localeCompare(b.exit_day + b.expiry)).map((t) => ({ expiry: t.expiry, exit_day: t.exit_day, net: t.net }))
  const nW = m.wins ?? per.filter((t) => t.net > 0).length, nL = m.losses ?? per.length - nW
  const cards: [string, React.ReactNode][] = [
    ['Net P&L', rs(m.total)], ['Gross P&L', rs(m.gross)], ['Charges', `₹${n0(m.charges)}`], ['Trades', m.trades], ['Wins / losses', `${nW} / ${nL}`], ['Win rate', `${m.win_rate}%`],
    ['Avg trade', rs(m.avg_trade ?? m.total / m.trades)], ['Avg win', rs(m.avg_win)], ['Avg loss', rs(m.avg_loss)], ['Largest win', rs(m.best)], ['Largest loss', rs(m.worst)],
    ['Profit factor', m.profit_factor ?? 'n/a (no losing trades)'], ['Max drawdown', rs(m.max_drawdown)], ['Expectancy / trade', rs(m.expectancy ?? m.total / m.trades)],
    ['Expectancy ratio', m.expectancy_ratio ?? 'n/a'], ['Max consecutive wins', m.max_consecutive_wins ?? '–'], ['Max consecutive losses', m.max_consecutive_losses ?? '–'],
  ]
  const nets = per.map((t) => t.net), lo = Math.min(...nets), hi = Math.max(...nets), nb = Math.min(12, Math.max(4, Math.ceil(Math.sqrt(nets.length))))
  const w = (hi - lo) / nb || 1
  const cnt = Array<number>(nb).fill(0)
  nets.forEach((v) => { cnt[Math.min(nb - 1, Math.floor((v - lo) / w))]++ })
  const reasons = m.exit_reasons ?? (() => { const o: Record<string, number> = {}; r.trades.forEach((t) => t.legs.forEach((l) => { o[l.reason] = (o[l.reason] || 0) + 1 })); return o })()
  return (
    <>
      <div className="card">
        <h2>{s.name} · {s.symbol} · {s.start} → {s.end}</h2>
        <div className="mcards">{cards.map(([a, b]) => <div key={a}><span className="k">{a}</span><b>{b}</b></div>)}</div>
        <p className="sub">{r.note}</p>
        <div className="row noprint"><button className="btn sm" onClick={() => downloadTradesCsv(r)}>Export trades CSV</button><button className="btn sm" onClick={() => window.print()}>Print / save PDF</button></div>
      </div>
      <div className="card"><h3>Cumulative net P&amp;L (line) &amp; drawdown (bars), by exit date</h3><EquityChart metrics={m} /></div>
      <div className="grid2">
        <div className="card"><h3>Trade-wise net P&amp;L (in exit order)</h3><Bars vals={per.map((t) => t.net)} tips={per.map((t) => `${t.expiry} (exit ${t.exit_day})`)} /></div>
        <div className="card"><h3>Net P&amp;L distribution (trades per bucket)</h3><Bars vals={cnt} tips={cnt.map((c, i) => `₹${n0(lo + i * w)} to ₹${n0(lo + (i + 1) * w)}: ${c} trades`)} signVals={cnt.map((_, i) => lo + (i + 0.5) * w)} neutral /></div>
      </div>
      <div className="grid2">
        <div className="card"><h3>Leg exit reasons</h3><table><thead><tr><th className="l">Reason</th><th>Legs</th></tr></thead><tbody>{Object.entries(reasons).map(([k, v]) => <tr key={k}><td className="l">{k}</td><td>{v}</td></tr>)}</tbody></table></div>
        <div className="card"><h3>How the metrics are calculated</h3><div className="sub" style={{ lineHeight: 1.6 }}>{METRIC_DEFS}</div></div>
      </div>
      <div className="grid2">
        <div className="card"><h3>Monthly P&amp;L</h3><table><thead><tr><th className="l">Month</th><th>P&amp;L</th></tr></thead><tbody>{Object.entries(m.monthly).map(([k, v]) => <tr key={k}><td className="l">{k}</td><td>{rs(v)}</td></tr>)}</tbody></table></div>
        <div className="card"><h3>Entry weekday</h3><table><thead><tr><th className="l">Day</th><th>Trades</th><th>P&amp;L</th></tr></thead><tbody>{Object.entries(m.weekday).map(([k, v]) => <tr key={k}><td className="l">{k}</td><td>{v.trades}</td><td>{rs(v.pnl)}</td></tr>)}</tbody></table></div>
      </div>
      {m.yearly && m.daily && (
        <div className="grid2">
          <div className="card"><h3>Yearly performance (by exit date)</h3><table><thead><tr><th className="l">Year</th><th>Trades</th><th>Wins</th><th>Gross</th><th>Charges</th><th>Net</th></tr></thead><tbody>{m.yearly.map((y) => <tr key={y.period}><td className="l">{y.period}</td><td>{y.trades}</td><td>{y.wins}</td><td>{rs(y.gross)}</td><td>₹{n0(y.charges)}</td><td>{rs(y.net)}</td></tr>)}</tbody></table></div>
          <div className="card"><h3>Daily performance (by exit date)</h3><div style={{ maxHeight: 300, overflow: 'auto' }}><table><thead><tr><th className="l">Date</th><th>Trades</th><th>Wins</th><th>Net</th></tr></thead><tbody>{m.daily.map((y) => <tr key={y.period}><td className="l">{y.period}</td><td>{y.trades}</td><td>{y.wins}</td><td>{rs(y.net)}</td></tr>)}</tbody></table></div></div>
        </div>
      )}
      <div className="card">
        <div className="card-head"><h3>Expiry-wise trade log</h3><span className="spacer" /><label className="inline">Filter <select value={filter} onChange={(e) => setFilter(e.target.value)}><option value="">all</option><option value="win">winners</option><option value="loss">losers</option><option value="sl">had SL</option><option value="flag">flagged</option></select></label></div>
        <div className="scroll">
          <table>
            <thead><tr><th className="l">Expiry</th><th className="l">Entry</th><th className="l">Exit</th><th>Spot in → out</th><th className="l">Legs (entry → exit · reason)</th><th>Gross</th><th>Charges</th><th>Net</th></tr></thead>
            <tbody>
              {trades.map((t) => (
                <tr key={t.expiry + t.entry_day}>
                  <td className="l">{t.expiry}</td><td className="l">{t.entry_day}</td><td className="l">{t.exit_day}</td>
                  <td>{n0(t.und_entry)} → {n0(t.und_exit)}{t.vix_entry != null && <div className="sub">VIX {num(t.vix_entry, 1)}</div>}</td>
                  <td className="l legcell">
                    {t.legs.map((l) => (
                      <div key={l.n}><span className={`badge ${l.side}`}>{l.side[0]}</span> {l.lots}×{n0(l.strike)}{l.opt} {num(l.entry)} → {num(l.exit)}{' '}
                        <span className="sub">{l.reason}{l.reason !== 'EXPIRY SETTLEMENT' ? ' ' + l.exit_day : ''} · lot {l.lot_size}{l.entry_day && l.entry_day !== t.entry_day ? ` · entered ${l.entry_day}` : ''}{l.expiry && l.expiry !== t.expiry ? ` · exp ${l.expiry}` : ''}</span> {rs(l.pnl)}</div>
                    ))}
                    {t.flags.length > 0 && <div className="sub warn">⚠ {t.flags.join(' · ⚠ ')}</div>}
                  </td>
                  <td>{rs(t.gross)}</td><td>₹{n0(t.charges)}</td><td><b className={sign(t.net)}>{rs(t.net)}</b></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      {r.skipped.length > 0 && <div className="card"><h3>Skipped expiries ({r.skipped.length})</h3><table><tbody>{r.skipped.map((x) => <tr key={x.expiry}><td className="l">{x.expiry}</td><td className="l">{x.reason}</td></tr>)}</tbody></table></div>}
    </>
  )
}
