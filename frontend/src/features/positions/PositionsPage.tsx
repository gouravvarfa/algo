import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ApiError, get, post } from '../../lib/api'
import { useToast } from '../../lib/toast'

interface Pos { symbol: string; exchange: string; product: string; net_qty: number | null; avg_price: number | null; ltp: number | null; pnl: number | null; realised: number | null; unrealised: number | null; app: boolean; instrument_id: string | null; stop: number | null }
interface Data { connected: boolean; error?: string; positions?: Pos[]; open?: number; total_pnl?: number; at?: number }

const rs = (v: number | null | undefined, sign = false) => (v == null ? '–' : (sign && v > 0 ? '+' : v < 0 ? '−' : '') + '₹' + Math.abs(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }))
const col = (v: number | null | undefined) => (v == null || v === 0 ? undefined : v > 0 ? 'var(--success)' : 'var(--danger)')

/** The connected Angel One account's positions today, like Angel One's own Positions screen: total P&L, then one row per contract.
 * Rows the app manages (Top-Bottom real orders) can be exited from here; anything else is shown only and must be managed in Angel One. */
export default function PositionsPage() {
  const qc = useQueryClient()
  const toast = useToast()
  const q = useQuery({ queryKey: ['broker-positions'], queryFn: () => get<Data>('/api/me/broker/positions'), refetchInterval: 10_000, retry: false })
  const exit = useMutation({
    mutationFn: (id: string) => post<{ message: string }>(`/api/me/broker/tb/${id}/exit`),
    onSuccess: (r) => { toast(r.message); for (const k of ['broker-positions', 'broker-tb', 'broker-instruments', 'broker-tb-trades']) void qc.invalidateQueries({ queryKey: [k] }) },
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Could not exit'),
  })
  const d = q.data
  if (!d) return <div className="page"><div className="skeleton" style={{ height: 140 }} /></div>
  if (!d.connected) return <div className="page"><div className="card"><h2>Positions</h2><p className="sub">Connect your Angel One account in <Link to="/settings">Settings</Link> to see your positions here.</p></div></div>
  const ps = d.positions ?? []
  const mine = ps.filter((p) => p.app && p.net_qty && p.instrument_id)
  const exitOne = (p: Pos) => { if (window.confirm(`EXIT ${p.symbol} now?\n\nReal goes OFF for this instrument and the position is closed at Angel One with a LIMIT order at the live price.`)) exit.mutate(p.instrument_id!) }
  return (
    <div className="page" style={{ maxWidth: 900 }}>
      <div className="card">
        <div className="row" style={{ alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <div>
            <div className="sub">Total P&amp;L · today</div>
            <div className="num" style={{ fontSize: 30, fontWeight: 700, color: col(d.total_pnl) }}>{rs(d.total_pnl, true)}</div>
            <div className="sub">{d.open ?? 0} open position{d.open === 1 ? '' : 's'} · from Angel One{d.at ? ` · ${new Date(d.at * 1000).toLocaleTimeString('en-IN')}` : ''}</div>
          </div>
          <span className="spacer" />
          {mine.length > 0 && (
            <button className="btn danger" disabled={exit.isPending}
              onClick={() => { if (window.confirm(`EXIT ALL ${mine.length} position(s) this app manages?\n\nPositions you opened yourself in Angel One are not touched.`)) mine.forEach((p) => exit.mutate(p.instrument_id!)) }}>Exit all (app)</button>
          )}
        </div>
        {d.error && <p className="err" role="alert" style={{ color: 'var(--danger)' }}>{d.error}</p>}
      </div>

      <div className="card" style={{ padding: 0 }}>
        {ps.length === 0 && <p className="empty" style={{ padding: 20 }}>No positions today.</p>}
        {ps.map((p) => (
          <div key={p.symbol + p.product} style={{ padding: '14px 18px', borderBottom: '1px solid var(--border)', opacity: p.net_qty ? 1 : 0.6 }}>
            <div className="row" style={{ gap: 8, alignItems: 'center' }}>
              <span className="sub">{p.exchange} · {p.product}</span>
              <span className="spacer" />
              {p.app ? <span className="chip bad" title="Opened by this app's Top-Bottom real orders">APP · REAL</span> : <span className="chip muted" title="Not opened by this app: manage it in Angel One">manual</span>}
              {p.stop != null && <span className="chip muted">SL {p.stop.toLocaleString('en-IN')}</span>}
              {!p.net_qty && <span className="chip muted">closed</span>}
            </div>
            <div className="row" style={{ alignItems: 'baseline', marginTop: 4 }}>
              <b style={{ fontSize: 17 }}>{p.symbol}</b>
              <span className="spacer" />
              <b className="num" style={{ fontSize: 17, color: col(p.pnl) }}>{rs(p.pnl, true)}</b>
            </div>
            <div className="row sub" style={{ marginTop: 2, alignItems: 'center', gap: 8 }}>
              <span>Qty: <b>{p.net_qty != null && p.net_qty > 0 ? '+' : ''}{p.net_qty ?? 0}</b> · Avg: <b>{p.avg_price?.toLocaleString('en-IN') ?? '–'}</b></span>
              <span className="spacer" />
              <span>LTP: <b>{p.ltp?.toLocaleString('en-IN') ?? '–'}</b></span>
              {p.app && !!p.net_qty && p.instrument_id && <button className="btn sm danger" disabled={exit.isPending} onClick={() => exitOne(p)}>Exit</button>}
            </div>
          </div>
        ))}
      </div>
      <p className="note">Numbers are Angel One&apos;s own, refreshed every 10 seconds. Only positions this app opened can be exited here; the rest stay untouched and are managed in Angel One.</p>
    </div>
  )
}
