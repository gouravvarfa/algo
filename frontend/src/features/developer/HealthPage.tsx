import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { get, post } from '../../lib/api'
import { ago } from '../../lib/format'
import './developer.css'

export interface Tile { key: string; title: string; status: 'OK' | 'DEGRADED' | 'DOWN' | 'N/A'; reason: string; metrics: Record<string, unknown>; checked_at: number }
interface Health { overall: 'OK' | 'DEGRADED' | 'DOWN'; checked_at: number; tiles: Tile[] }
interface SelfTest { at: number | null; passed: number; failed: number; results: { name: string; ok: boolean; expected: string; actual: string; error: string | null; ms: number }[] }

export const statusClass = (s: string) => (s === 'OK' ? 'ok' : s === 'DEGRADED' || s === 'IDLE' ? 'warn' : s === 'N/A' ? 'muted' : 'bad')

/** Landing page of the developer panel: one traffic-light tile per component, then the known-answer self-tests. */
export default function HealthPage() {
  const qc = useQueryClient()
  const health = useQuery({ queryKey: ['dev', 'health'], queryFn: () => get<Health>('/api/dev/health'), refetchInterval: 5000 })
  const tests = useQuery({ queryKey: ['dev', 'selftest'], queryFn: () => get<SelfTest>('/api/dev/selftest'), refetchInterval: 30_000 })
  const run = useMutation({ mutationFn: () => post<SelfTest>('/api/dev/selftest/run'), onSuccess: (d) => { qc.setQueryData(['dev', 'selftest'], d); void qc.invalidateQueries({ queryKey: ['dev', 'health'] }) } })
  const h = health.data
  return (
    <div className="page">
      <div className="row" style={{ alignItems: 'center' }}>
        <h1>System health</h1>
        {h && <span className={`chip ${statusClass(h.overall)} big`}>{h.overall}</span>}
        <span className="spacer" />
        <span className="sub">{h ? `checked ${ago(h.checked_at)} · refreshes every 5 s` : ''}</span>
      </div>
      {health.error && <div className="alert">Could not load health: {(health.error as Error).message}</div>}
      <div className="tiles">
        {(h?.tiles ?? Array.from({ length: 8 })).map((t, i) =>
          t ? (
            <div key={(t as Tile).key} className={`tile ${statusClass((t as Tile).status)}`}>
              <div className="tile-head"><b>{(t as Tile).title}</b><span className={`chip ${statusClass((t as Tile).status)}`}>{(t as Tile).status}</span></div>
              <p>{(t as Tile).reason}</p>
            </div>
          ) : <div key={i} className="tile skeleton" style={{ minHeight: 84 }} />,
        )}
      </div>
      <div className="card">
        <div className="card-head">
          <h2>Known-answer self-tests</h2>
          {tests.data && tests.data.at && <span className={`chip ${tests.data.failed ? 'bad' : 'ok'}`}>{tests.data.passed} passed · {tests.data.failed} failed</span>}
          <span className="spacer" />
          <span className="sub">{tests.data?.at ? `last run ${ago(tests.data.at)}` : 'not run yet'}</span>
          <button className="btn primary sm" onClick={() => run.mutate()} disabled={run.isPending}>{run.isPending ? 'Running…' : 'Run now'}</button>
        </div>
        <p className="sub" style={{ marginBottom: 8 }}>Each check feeds a small fixed input to a real function and compares with an answer worked out independently. A failure names the function and shows the difference.</p>
        <div className="scroll">
          <table>
            <thead><tr><th className="l">Check</th><th>Result</th><th className="l">Expected</th><th className="l">Actual</th><th>ms</th></tr></thead>
            <tbody>
              {(tests.data?.results ?? []).map((r) => (
                <tr key={r.name}>
                  <td className="l mono">{r.name}</td>
                  <td><span className={`chip ${r.ok ? 'ok' : 'bad'}`}>{r.ok ? 'PASS' : 'FAIL'}</span></td>
                  <td className="l mono dim">{r.ok ? '' : r.expected}</td>
                  <td className="l mono dim">{r.ok ? '' : r.error ?? r.actual}</td>
                  <td className="num">{r.ms}</td>
                </tr>
              ))}
              {!tests.data?.results.length && <tr><td colSpan={5} className="empty">No results yet. Press “Run now”.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
