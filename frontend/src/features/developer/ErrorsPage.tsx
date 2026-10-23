import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { get, post } from '../../lib/api'
import { ago } from '../../lib/format'
import './developer.css'

interface Group { component: string; message: string; count: number; first_seen: number; last_seen: number; users: string[]; last_action: string | null }
interface Incident { id: number | null; ts: number; component: string; user_id: string | null; severity: string; message: string; auto_action: string | null; resolved_at: number | null }

export default function ErrorsPage() {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['dev', 'errors'], queryFn: () => get<{ groups: Group[]; recent: Incident[] }>('/api/dev/errors?limit=200'), refetchInterval: 10_000 })
  const resolve = useMutation({ mutationFn: (id: number) => post(`/api/dev/incidents/${id}/resolve`), onSuccess: () => { void qc.invalidateQueries({ queryKey: ['dev', 'errors'] }) } })
  return (
    <div className="page">
      <h1>Errors and incidents</h1>
      <p className="sub">Every isolated failure with what the system did about it. Grouped by cause first, then the latest entries.</p>
      <div className="card">
        <div className="card-head"><h2>Grouped by cause</h2></div>
        <div className="scroll">
          <table>
            <thead><tr><th className="l">Component</th><th className="l">Message</th><th>Count</th><th className="l">First seen</th><th className="l">Last seen</th><th>Users</th><th className="l">System action</th></tr></thead>
            <tbody>
              {(q.data?.groups ?? []).map((g, i) => (
                <tr key={i}>
                  <td className="l mono">{g.component}</td><td className="l" title={g.message}>{g.message}</td><td className="num">{g.count}</td>
                  <td className="l">{ago(g.first_seen)}</td><td className="l">{ago(g.last_seen)}</td><td className="num">{g.users.length || '–'}</td><td className="l dim">{g.last_action ?? '–'}</td>
                </tr>
              ))}
              {!q.data?.groups.length && <tr><td colSpan={7} className="empty">No incidents recorded. Everything the system noticed is listed here.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
      <div className="card">
        <div className="card-head"><h2>Latest incidents</h2></div>
        <div className="scroll">
          <table>
            <thead><tr><th className="l">When</th><th className="l">Severity</th><th className="l">Component</th><th className="l">Message</th><th className="l">System action</th><th /></tr></thead>
            <tbody>
              {(q.data?.recent ?? []).map((e, i) => (
                <tr key={e.id ?? `m${i}`} className={e.resolved_at ? 'resolved' : ''}>
                  <td className="l">{ago(e.ts)}</td>
                  <td className="l"><span className={`chip ${e.severity === 'error' ? 'bad' : e.severity === 'warning' ? 'warn' : 'muted'}`}>{e.severity}</span></td>
                  <td className="l mono">{e.component}</td><td className="l wrap">{e.message}</td><td className="l dim">{e.auto_action ?? '–'}</td>
                  <td>{e.id != null && !e.resolved_at && <button className="btn sm" onClick={() => resolve.mutate(e.id!)}>Resolve</button>}{e.resolved_at && <span className="sub">resolved</span>}</td>
                </tr>
              ))}
              {!q.data?.recent.length && <tr><td colSpan={6} className="empty">Nothing yet.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
