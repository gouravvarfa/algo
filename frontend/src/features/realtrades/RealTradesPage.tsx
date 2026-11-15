import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { get } from '../../lib/api'
import TopBottomPage from '../topbottom/TopBottomPage'

export interface RealTrade {
  instrument_id: string; symbol: string; direction: string; qty: number; contract: string; entry_price: number | null; entry_at: number | null
  exit_price: number | null; exit_at: number | null; exit_by: string | null; pnl_points: number | null; pnl_amount: number | null; status: string
}
interface Inst { instrument_id: string; symbol: string; real_on: boolean; position: { side: string; qty: number; contract: string; entry_price: number } | null; stop_target: number | null; halted: string | null }
export interface RealData { trades: RealTrade[]; instruments: Inst[]; realised: number; closed: number; wins: number; open: number }

const rs = (v: number | null | undefined, sign = false) => (v == null ? '–' : (sign && v > 0 ? '+' : v < 0 ? '−' : '') + '₹' + Math.abs(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }))
const col = (v: number | null | undefined) => (v == null || v === 0 ? undefined : v > 0 ? 'var(--success)' : 'var(--danger)')
const when = (t: number | null) => (t ? new Date(t * 1000).toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '–')

/** The instruments the Real Trades dashboard shows: armed now, holding a real position, halted, or with any real trade. Never an instrument with paper only. */
export function realInstrumentIds(d: RealData): string[] {
  const traded = new Set(d.trades.map((t) => t.instrument_id))
  return d.instruments.filter((i) => i.real_on || i.position || i.halted || traded.has(i.instrument_id)).map((i) => i.instrument_id)
}

/** REAL money only: the same dashboard as Top Bottom (chart, position, stop, stats, trades), built only from what Angel One filled for this person,
 * for each instrument with real money on it, plus every real trade of all instruments together. Never mixed with paper trades. */
export default function RealTradesPage() {
  const q = useQuery({ queryKey: ['broker-tb-real-trades'], queryFn: () => get<RealData>('/api/me/broker/tb-real-trades'), refetchInterval: 10_000, retry: false })
  const d = q.data
  if (q.isError) return <div className="page"><div className="card"><h2>Real Trades</h2><p className="sub">Could not load your real trades. Is your Angel One account connected in <Link to="/settings">Settings</Link>?</p></div></div>
  if (!d) return <div className="page"><div className="skeleton" style={{ height: 140 }} /></div>
  const on = d.instruments.filter((i) => i.real_on)
  const ids = realInstrumentIds(d)

  const top = (
    <>
      <div className="card" style={{ borderLeft: '4px solid var(--danger)' }}>
        <div className="row" style={{ alignItems: 'center', gap: 24, flexWrap: 'wrap' }}>
          <div>
            <div className="sub">REAL money · all instruments · realised P&amp;L (before brokerage and taxes)</div>
            <div className="num" style={{ fontSize: 30, fontWeight: 700, color: col(d.realised) }}>{rs(d.realised, true)}</div>
          </div>
          <div><div className="sub">Closed trades</div><div className="num" style={{ fontSize: 20 }}>{d.closed}{d.closed ? ` · ${d.wins} won` : ''}</div></div>
          <div><div className="sub">Open now</div><div className="num" style={{ fontSize: 20 }}>{d.open}</div></div>
          <div><div className="sub">Real armed</div><div style={{ fontSize: 15, fontWeight: 600 }}>{on.length ? on.map((i) => i.symbol).join(', ') : 'none'}</div></div>
        </div>
        <p className="sub" style={{ marginTop: 8 }}>Only orders Angel One actually filled, from the moment you turned REAL on. Paper trades stay on the <Link to="/top-bottom">Top Bottom</Link> page and never appear here.</p>
      </div>
      {!ids.length && <div className="card"><p className="sub">No instrument has real money on it yet. Turn REAL on for an instrument on the <Link to="/top-bottom">Top Bottom</Link> page: its dashboard appears here and fills from the next new System Point.</p></div>}
    </>
  )

  const bottom = (
    <div className="card">
      <div className="card-head"><h2>All real trades</h2><span className="chip bad">REAL</span></div>
      {d.trades.length === 0 ? <p className="sub">No real trade yet. After you turn REAL on for an instrument, the first real order goes out at its next new System Point.</p> : (
        <div className="scroll"><table><thead><tr><th className="l">Instrument</th><th className="l">Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th className="l">Exit by</th><th>Points</th><th>P&amp;L</th></tr></thead>
          <tbody>{d.trades.map((t, n) => (
            <tr key={`${t.instrument_id}-${t.entry_at}-${n}`}>
              <td className="l">{t.symbol}<div className="sub">{t.contract}</div></td>
              <td className="l" style={{ color: t.direction === 'BUY' ? 'var(--success)' : 'var(--danger)', fontWeight: 600 }}>{t.direction}</td>
              <td className="num">{t.qty}</td>
              <td className="num">{rs(t.entry_price)}<div className="sub">{when(t.entry_at)}</div></td>
              <td className="num">{t.status === 'OPEN' ? <b>OPEN</b> : rs(t.exit_price)}<div className="sub">{when(t.exit_at)}</div></td>
              <td className="l">{t.exit_by ?? '–'}</td>
              <td className="num" style={{ color: col(t.pnl_points) }}>{t.pnl_points == null ? '–' : t.pnl_points.toFixed(2)}</td>
              <td className="num" style={{ color: col(t.pnl_amount), fontWeight: 600 }}>{rs(t.pnl_amount, true)}</td>
            </tr>))}</tbody></table></div>
      )}
    </div>
  )

  if (!ids.length) return <div className="page">{top}{bottom}</div>
  return <TopBottomPage realOnly only={ids} top={top} bottom={bottom} />
}
