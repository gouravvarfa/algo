import { useQuery } from '@tanstack/react-query'
import { get } from '../../lib/api'
import { ago } from '../../lib/format'
import { statusClass } from './HealthPage'
import './developer.css'

interface Fn {
  name: string; status: string; calls_total: number; errors_total: number; calls_hour: number; errors_hour: number; error_pct: number
  avg_ms: number | null; p95_ms: number | null; last_success: number | null; last_error: number | null; last_error_msg: string | null
}
interface Payload { functions: Fn[]; requests: { requests: number; errors_5xx: number; error_pct: number; p50_ms: number | null; p95_ms: number | null; p99_ms: number | null; routes: { route: string; count: number; avg_ms: number; errors: number }[] } }

export default function FunctionsPage() {
  const q = useQuery({ queryKey: ['dev', 'functions'], queryFn: () => get<Payload>('/api/dev/functions'), refetchInterval: 5000 })
  const fns = q.data?.functions ?? []
  const r = q.data?.requests
  return (
    <div className="page">
      <h1>Functions</h1>
      <p className="sub">Every important function records its calls, errors and time. Status: OK under 2% errors in the last hour · DEGRADED 2–20% · FAULTY above 20% or the last 3 calls failed.</p>
      {q.error && <div className="alert">{(q.error as Error).message}</div>}
      <div className="card">
        <div className="scroll">
          <table>
            <thead><tr><th className="l">Function</th><th>Status</th><th>Calls / hour</th><th>Avg ms</th><th>p95 ms</th><th>Error %</th><th className="l">Last success</th><th className="l">Last error</th></tr></thead>
            <tbody>
              {fns.map((f) => (
                <tr key={f.name}>
                  <td className="l mono">{f.name}</td>
                  <td><span className={`chip ${statusClass(f.status)}`}>{f.status}</span></td>
                  <td className="num">{f.calls_hour.toLocaleString('en-IN')}</td>
                  <td className="num">{f.avg_ms ?? '–'}</td>
                  <td className="num">{f.p95_ms ?? '–'}</td>
                  <td className={`num ${f.error_pct > 0 ? 'neg' : ''}`}>{f.error_pct}%</td>
                  <td className="l">{ago(f.last_success)}</td>
                  <td className="l" title={f.last_error_msg ?? ''}>{f.last_error ? `${ago(f.last_error)} · ${f.last_error_msg?.slice(0, 50) ?? ''}` : '–'}</td>
                </tr>
              ))}
              {!fns.length && <tr><td colSpan={8} className="empty">No functions recorded yet.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
      {r && (
        <div className="card">
          <div className="card-head"><h2>HTTP requests (last 5 minutes)</h2><span className="chip muted">{r.requests} requests</span><span className="chip muted">p50 {r.p50_ms ?? '–'} ms</span><span className="chip muted">p95 {r.p95_ms ?? '–'} ms</span><span className="chip muted">p99 {r.p99_ms ?? '–'} ms</span><span className={`chip ${r.errors_5xx ? 'bad' : 'ok'}`}>{r.errors_5xx} server errors</span></div>
          <table>
            <thead><tr><th className="l">Route</th><th>Count</th><th>Avg ms</th><th>5xx</th></tr></thead>
            <tbody>{r.routes.map((x) => <tr key={x.route}><td className="l mono">{x.route}</td><td className="num">{x.count}</td><td className="num">{x.avg_ms}</td><td className={`num ${x.errors ? 'neg' : ''}`}>{x.errors}</td></tr>)}</tbody>
          </table>
        </div>
      )}
    </div>
  )
}
