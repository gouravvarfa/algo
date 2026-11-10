import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { get } from '../../lib/api'

export interface RealTrade {
  instrument_id: string; symbol: string; direction: string; qty: number; contract: string; entry_price: number | null; entry_at: number | null
  exit_price: number | null; exit_at: number | null; exit_by: string | null; pnl_points: number | null; pnl_amount: number | null; status: string
}
interface Inst { instrument_id: string; symbol: string; real_on: boolean; position: { side: string; qty: number; contract: string; entry_price: number } | null; stop_target: number | null; halted: string | null }
export interface RealData { trades: RealTrade[]; instruments: Inst[]; realised: number; closed: number; wins: number; open: number }

const rs = (v: number | null | undefined, sign = false) => (v == null ? '–' : (sign && v > 0 ? '+' : v < 0 ? '−' : '') + '₹' + Math.abs(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }))
const col = (v: number | null | undefined) => (v == null || v === 0 ? undefined : v > 0 ? 'var(--success)' : 'var(--danger)')
const when = (t: number | null) => (t ? new Date(t * 1000).toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '–')

/** REAL money only: every Top-Bottom trade Angel One filled for this person, all instruments together, so it is never mixed up with paper trades. */
export default function RealTradesPage() {
  const q = useQuery({ queryKey: ['broker-tb-real-trades'], queryFn: () => get<RealData>('/api/me/broker/tb-real-trades'), refetchInterval: 10_000, retry: false })
  const d = q.data
  if (q.isError) return <div className="page"><div className="card"><h2>Real Trades</h2><p className="sub">Could not load your real trades. Is your Angel One account connected in <Link to="/settings">Settings</Link>?</p></div></div>
  if (!d) return <div className="page"><div className="skeleton" style={{ height: 140 }} /></div>
  const on = d.instruments.filter((i) => i.real_on)
  return (
    <div className="page" style={{ maxWidth: 1000 }}>
      <div className="card" style={{ borderLeft: '4px solid var(--danger)' }}>
        <div className="row" style={{ alignItems: 'center', gap: 24, flexWrap: 'wrap' }}>
          <div>
            <div className="sub">REAL money · realised P&amp;L (before brokerage and taxes)</div>
            <div className="num" style={{ fontSize: 30, fontWeight: 700, color: col(d.realised) }}>{rs(d.realised, true)}</div>
          </div>
          <div><div className="sub">Closed trades</div><div className="num" style={{ fontSize: 20 }}>{d.closed}{d.closed ? ` · ${d.wins} won` : ''}</div></div>
          <div><div className="sub">Open now</div><div className="num" style={{ fontSize: 20 }}>{d.open}</div></div>
          <div><div className="sub">Real switch ON</div><div style={{ fontSize: 15, fontWeight: 600 }}>{on.length ? on.map((i) => i.symbol).join(', ') : 'none'}</div></div>
        </div>
        <p className="sub" style={{ marginTop: 8 }}>Only orders Angel One actually filled. Paper trades are on the <Link to="/top-bottom">Top Bottom</Link> page and never appear here.</p>
      </div>

      {d.instruments.some((i) => i.position || i.halted) && (
        <div className="card">
          <h3>Open real positions</h3>
          <div style={{ overflowX: 'auto' }}><table className="tbl"><thead><tr><th>Instrument</th><th>Side</th><th>Qty</th><th>Contract</th><th>Entry</th><th>Stop</th><th>State</th></tr></thead>
            <tbody>{d.instruments.filter((i) => i.position || i.halted).map((i) => (
              <tr key={i.instrument_id}>
                <td>{i.symbol}</td><td style={{ color: i.position?.side === 'BUY' ? 'var(--success)' : 'var(--danger)', fontWeight: 600 }}>{i.position?.side ?? '–'}</td>
                <td className="num">{i.position?.qty ?? '–'}</td><td>{i.position?.contract ?? '–'}</td><td className="num">{i.position ? rs(i.position.entry_price) : '–'}</td>
                <td className="num">{i.stop_target != null ? rs(i.stop_target) : '–'}</td><td>{i.halted ? <b style={{ color: 'var(--danger)' }}>HALTED</b> : i.real_on ? 'Real ON' : 'Real OFF (position kept)'}</td>
              </tr>))}</tbody></table></div>
        </div>
      )}

      <div className="card">
        <h3>All real trades</h3>
        {d.trades.length === 0 ? <p className="sub">No real trade yet. After you turn real ON for an instrument, the first real order goes out at its next new System Point.</p> : (
          <div style={{ overflowX: 'auto' }}><table className="tbl"><thead><tr><th>Instrument</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Exit by</th><th>Points</th><th>P&amp;L</th></tr></thead>
            <tbody>{d.trades.map((t, n) => (
              <tr key={`${t.instrument_id}-${t.entry_at}-${n}`}>
                <td>{t.symbol}<div className="sub">{t.contract}</div></td>
                <td style={{ color: t.direction === 'BUY' ? 'var(--success)' : 'var(--danger)', fontWeight: 600 }}>{t.direction}</td>
                <td className="num">{t.qty}</td>
                <td className="num">{rs(t.entry_price)}<div className="sub">{when(t.entry_at)}</div></td>
                <td className="num">{t.status === 'OPEN' ? <b>OPEN</b> : rs(t.exit_price)}<div className="sub">{when(t.exit_at)}</div></td>
                <td>{t.exit_by ?? '–'}</td>
                <td className="num" style={{ color: col(t.pnl_points) }}>{t.pnl_points == null ? '–' : t.pnl_points.toFixed(2)}</td>
                <td className="num" style={{ color: col(t.pnl_amount), fontWeight: 600 }}>{rs(t.pnl_amount, true)}</td>
              </tr>))}</tbody></table></div>
        )}
      </div>
    </div>
  )
}
