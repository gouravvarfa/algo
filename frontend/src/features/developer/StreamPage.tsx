import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { get } from '../../lib/api'
import { stream } from '../../lib/stream'
import '../journal/journal.css'
import './developer.css'

interface Stat {
  enabled: boolean; available: boolean; clients?: number; tokens_watched?: number; connects?: number; disconnects?: number; errors?: number
  msgs_per_s?: number; bytes_per_s?: number; lag_ms_avg?: number | null; lag_ms_p95?: number | null; lag_samples?: number; feed_connected?: boolean
  engine?: { sweeps: number; last_ms?: number; avg_ms?: number; p95_ms?: number; cycle_s?: number | null } | null
  list?: { id: number; user: string; tokens: number; age_s: number; msgs: number; bytes: number; ua: string }[]
}
const n = (x: number | null | undefined, d = 0) => (x == null ? '–' : x.toLocaleString('en-IN', { maximumFractionDigits: d }))

/** Live tick push to browsers (SSE): who is connected, how many messages, and how old a price is when it leaves the server. */
export default function StreamPage() {
  const q = useQuery({ queryKey: ['dev', 'stream'], queryFn: () => get<Stat>('/api/dev/stream'), refetchInterval: 2000 })
  const s = q.data
  const mine = stream.status
  return (
    <div className="page">
      <div className="row" style={{ alignItems: 'center' }}>
        <h1>Live stream</h1>
        <span className={'chip ' + (s?.enabled ? 'ok' : 'warn')}>{s ? (s.enabled ? 'ON' : 'OFF (screens poll)') : '…'}</span>
        <span className="chip muted">this tab: {mine}</span>
        <span className="spacer" />
        <Link className="btn sm" to="/dev/flags">Switch it on / off (feature.stream)</Link>
      </div>
      <p className="sub">The server pushes each price to the browser the moment it arrives (Server-Sent Events). It only listens to the tick feed: the paper engine, the strategies and the dry run are not touched. If it is off or fails, every screen goes back to its normal polling.</p>
      {q.error && <div className="alert">{(q.error as Error).message}</div>}
      {s && !s.available && <div className="alert warn">The stream is not running in this process.</div>}
      <div className="kpis">
        <div className="kpi"><span className="k">Open streams</span><b className="num">{n(s?.clients)}</b></div>
        <div className="kpi"><span className="k">Tokens watched</span><b className="num">{n(s?.tokens_watched)}</b></div>
        <div className="kpi"><span className="k">Messages / s</span><b className="num">{n(s?.msgs_per_s, 2)}</b></div>
        <div className="kpi"><span className="k">Bytes / s</span><b className="num">{n(s?.bytes_per_s)}</b></div>
        <div className="kpi"><span className="k">Server delay, average</span><b className="num">{s?.lag_ms_avg == null ? '–' : `${n(s.lag_ms_avg, 1)} ms`}</b></div>
        <div className="kpi"><span className="k">Server delay, p95</span><b className="num">{s?.lag_ms_p95 == null ? '–' : `${n(s.lag_ms_p95, 1)} ms`}</b></div>
        <div className="kpi"><span className="k">Connects / drops</span><b className="num">{n(s?.connects)} / {n(s?.disconnects)}</b></div>
        <div className="kpi"><span className="k">Engine: one pass, avg</span><b className="num">{s?.engine?.avg_ms == null ? '–' : `${n(s.engine.avg_ms, 0)} ms`}</b></div>
        <div className="kpi"><span className="k">Engine: pass every</span><b className="num">{s?.engine?.cycle_s == null ? '–' : `${n(s.engine.cycle_s, 2)} s`}</b></div>
        <div className="kpi"><span className="k">Errors</span><b className={'num ' + ((s?.errors ?? 0) > 0 ? 'neg' : '')}>{n(s?.errors)}</b></div>
      </div>
      <p className="sub">Server delay = from the moment a tick reached the server to the moment its message was written (the last 60 s). The network time to the browser is on top of it. The tick feed from Angel One is {s?.feed_connected ? 'connected' : 'NOT connected'}.</p>
      <div className="card">
        <div className="card-head"><h2>Connected browsers</h2><span className="chip muted">{s?.list?.length ?? 0}</span></div>
        <div className="scroll"><table>
          <thead><tr><th className="l">#</th><th className="l">User</th><th>Tokens</th><th>Open for</th><th>Messages</th><th>Bytes</th><th className="l">Browser</th></tr></thead>
          <tbody>
            {(s?.list ?? []).map((c) => (
              <tr key={c.id}><td className="l">{c.id}</td><td className="l mono">{c.user}</td><td className="num">{c.tokens}</td><td className="num">{c.age_s} s</td><td className="num">{n(c.msgs)}</td><td className="num">{n(c.bytes)}</td><td className="l" title={c.ua}>{c.ua.slice(0, 48)}</td></tr>
            ))}
            {!(s?.list ?? []).length && <tr><td colSpan={7} className="empty">No screen is receiving pushed prices right now (they connect only while the market is open).</td></tr>}
          </tbody>
        </table></div>
      </div>
    </div>
  )
}
