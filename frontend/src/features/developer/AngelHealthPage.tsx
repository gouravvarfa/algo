import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { ApiError, get, post } from '../../lib/api'
import { useToast } from '../../lib/toast'

interface Ep { endpoint: string; calls: number; errors: number; rate_limited: number; p50_ms: number | null; p95_ms: number | null; max_ms: number | null; last_at: number | null; last_error: string | null; last_error_at: number | null }
interface Data {
  status: string; calls: number; errors: number; rate_limited: number; window_s: number; endpoints: Ep[]
  outbound_ip: { ip: string | null; error: string | null; at: number }
  ip_rejects_7d: { created_at: number; symbol: string; role: string; message: string }[]
  accounts: { username: string | null; status: string; status_message: string | null; last_checked_at: number | null }[]
}

const ago = (t: number | null) => { if (!t) return '–'; const s = Math.round(Date.now() / 1000 - t); return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago` }
const ms = (v: number | null) => (v == null ? '–' : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`)
const NAMES: Record<string, string> = { loginByPassword: 'Login', getLtpData: 'Live price (LTP)', batch: 'Margin calculator', getRMS: 'Funds', getPosition: 'Positions', getOrderBook: 'Order book',
  placeOrder: 'Place order', modifyOrder: 'Modify order', cancelOrder: 'Cancel order', getCandleData: 'Candles (history)', logout: 'Logout' }

/** How Angel One is answering, measured on the app's own calls: speed per endpoint, errors, rate limits, and the IP our orders leave from. */
export default function AngelHealthPage() {
  const toast = useToast()
  const [win, setWin] = useState(3600)
  const q = useQuery({ queryKey: ['dev', 'angel', win], queryFn: () => get<Data>(`/api/dev/angel?window=${win}`), refetchInterval: 15_000 })
  const probe = useMutation({
    mutationFn: () => post<{ ok: boolean; session_ms?: number; ltp_ms?: number; nifty?: number; error?: string }>('/api/dev/angel/probe'),
    onSuccess: (r) => { toast(r.ok ? `Angel One answered: NIFTY ${r.nifty} · session ${r.session_ms} ms · price ${r.ltp_ms} ms` : `Probe failed: ${r.error}`); void q.refetch() },
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Probe failed'),
  })
  const d = q.data
  const tone = d?.status === 'ok' ? 'ok' : d?.status === 'idle' ? 'muted' : d?.status === 'degraded' ? 'warn' : 'bad'
  return (
    <div className="page">
      <h1>Angel One API</h1>
      <p className="sub">Measured on the calls the app already makes (nothing extra is sent). Refreshes every 15 seconds.</p>
      <div className="card">
        <div className="card-head" style={{ gap: 10, flexWrap: 'wrap' }}>
          <h2>Overall</h2><span className={'chip ' + tone}>{(d?.status ?? '…').toUpperCase()}</span><span className="spacer" />
          <select value={win} onChange={(e) => setWin(Number(e.target.value))}><option value={900}>last 15 min</option><option value={3600}>last hour</option><option value={86400}>last 24 h</option></select>
          <button className="btn sm" disabled={probe.isPending} onClick={() => probe.mutate()} title="One live NIFTY price through the app's data session. Read-only.">{probe.isPending ? 'Probing…' : 'Probe now'}</button>
        </div>
        <div className="tbkpis" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 10 }}>
          <div className="stat"><div className="k">Calls</div><div className="v">{d?.calls ?? '–'}</div></div>
          <div className="stat"><div className="k">Errors</div><div className={'v ' + ((d?.errors ?? 0) > 0 ? 'neg' : '')}>{d?.errors ?? '–'}</div></div>
          <div className="stat"><div className="k">Rate limited</div><div className={'v ' + ((d?.rate_limited ?? 0) > 0 ? 'neg' : '')}>{d?.rate_limited ?? '–'}</div><div className="s">403 / 429 answers</div></div>
          <div className="stat"><div className="k">Orders leave from</div><div className="v" style={{ fontSize: 18 }}>{d?.outbound_ip.ip ?? (d?.outbound_ip.error ? 'unknown' : '…')}</div><div className="s">must be the static IP registered in SmartAPI</div></div>
        </div>
      </div>

      <div className="card">
        <div className="card-head"><h2>By endpoint</h2></div>
        {!d || d.endpoints.length === 0 ? <p className="empty">No Angel One call in this window yet.</p> : (
          <div style={{ overflowX: 'auto' }}>
            <table className="tbl"><thead><tr><th>Call</th><th>Count</th><th>Median</th><th>95%</th><th>Slowest</th><th>Errors</th><th>Rate limited</th><th>Last</th><th>Last error</th></tr></thead>
              <tbody>{d.endpoints.map((e) => (
                <tr key={e.endpoint}>
                  <td><b>{NAMES[e.endpoint] ?? e.endpoint}</b><div className="sub mono">{e.endpoint}</div></td>
                  <td>{e.calls}</td><td>{ms(e.p50_ms)}</td><td className={(e.p95_ms ?? 0) > 3000 ? 'neg' : ''}>{ms(e.p95_ms)}</td><td>{ms(e.max_ms)}</td>
                  <td className={e.errors ? 'neg' : ''}>{e.errors}</td><td className={e.rate_limited ? 'neg' : ''}>{e.rate_limited}</td><td>{ago(e.last_at)}</td>
                  <td className="sub">{e.last_error ? `${e.last_error} · ${ago(e.last_error_at)}` : '–'}</td>
                </tr>
              ))}</tbody></table>
          </div>
        )}
      </div>

      <div className="card">
        <div className="card-head"><h2>Accounts</h2><span className="spacer" /><span className="sub">login state of every connected Angel One account</span></div>
        {!d || d.accounts.length === 0 ? <p className="empty">No account connected.</p> : (
          <table className="tbl"><thead><tr><th>User</th><th>Login</th><th>Checked</th><th>Last problem</th></tr></thead>
            <tbody>{d.accounts.map((a, i) => (
              <tr key={i}><td>{a.username ?? '–'}</td><td><span className={'chip ' + (a.status === 'connected' ? 'ok' : a.status === 'error' ? 'bad' : 'warn')}>{a.status}</span></td><td>{ago(a.last_checked_at)}</td><td className="sub">{a.status_message ?? '–'}</td></tr>
            ))}</tbody></table>
        )}
      </div>

      <div className="card">
        <div className="card-head"><h2>Orders refused for the IP (7 days)</h2></div>
        {!d || d.ip_rejects_7d.length === 0 ? <p className="empty">None. Angel One has not refused a real order because of the static IP.</p> : (
          <ul>{d.ip_rejects_7d.map((r, i) => <li key={i}>{new Date(r.created_at * 1000).toLocaleString('en-IN')} · {r.symbol} {r.role}: <span className="neg">{r.message}</span></li>)}</ul>
        )}
      </div>
    </div>
  )
}
