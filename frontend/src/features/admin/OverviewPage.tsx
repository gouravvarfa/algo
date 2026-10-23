import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { get } from '../../lib/api'
import { ago, money, sign } from '../../lib/format'
import { useSession } from '../../lib/session'
import './admin.css'

interface Row { id: string; username: string; name: string; role: string; status: string; online: boolean; open_positions: number; unrealized: number; state: string; fault: string | null; paused: boolean }
interface Entry { id: number; ts: string; actor_role: string | null; action: string; target_type: string | null }
interface Summary { overall: string; feed: { status: string; reason: string } | null; engine: { status: string; reason: string } | null; users: { status: string; reason: string } | null; incidents: { status: string; reason: string } | null }

const tone = (s: string | undefined) => (s === 'OK' ? 'ok' : s === 'DEGRADED' ? 'warn' : s === 'DOWN' ? 'bad' : 'muted')

/** The admin's landing page: who is here, who is waiting to set up, anything that needs attention, and the latest things that happened. */
export default function AdminOverview() {
  const { can } = useSession()
  const users = useQuery({ queryKey: ['admin', 'live-users'], queryFn: () => get<{ users: Row[] }>('/api/admin/live-users'), refetchInterval: 5000 })
  const audit = useQuery({ queryKey: ['admin', 'audit-head'], queryFn: () => get<{ entries: Entry[] }>('/api/admin/audit?limit=8'), refetchInterval: 15_000, enabled: can('audit.read') })
  const sys = useQuery({ queryKey: ['console', 'summary'], queryFn: () => get<Summary>('/api/admin/system-summary'), refetchInterval: 15_000 })
  const rows = users.data?.users ?? []
  const n = (f: (r: Row) => boolean) => rows.filter(f).length
  const attention = rows.filter((r) => r.fault || r.paused || r.status === 'INVITED')
  const tiles: [string, number | string, string][] = [
    ['Online now', n((r) => r.online), 'ok'], ['Active users', n((r) => r.status === 'ACTIVE'), ''], ['Waiting for setup', n((r) => r.status === 'INVITED'), n((r) => r.status === 'INVITED') ? 'warn' : ''],
    ['Disabled', n((r) => r.status === 'DISABLED'), ''], ['Open positions', rows.reduce((a, r) => a + r.open_positions, 0), ''], ['Unrealized (all users)', money(rows.reduce((a, r) => a + r.unrealized, 0)), ''],
  ]
  return (
    <div className="page">
      <div className="row" style={{ alignItems: 'center' }}><h1>Admin overview</h1><span className="spacer" /><Link className="btn primary" to="/admin/users">Manage users</Link></div>
      <div className="kpis">{tiles.map(([k, v, t]) => <div key={k} className={`kpi ${t}`}><span className="k">{k}</span><b className="num">{v}</b></div>)}</div>

      <div className="grid2">
        <div className="card">
          <div className="card-head"><h2>Needs attention</h2><span className="chip muted">{attention.length}</span></div>
          {attention.length ? (
            <table><tbody>
              {attention.map((r) => (
                <tr key={r.id}>
                  <td className="l"><b>{r.name}</b> <span className="sub mono">{r.username}</span></td>
                  <td className="l">{r.fault ? <span className="chip bad" title={r.fault}>strategy FAULT</span> : r.paused ? <span className="chip warn">engine paused</span> : <span className="chip warn">setup link not used yet</span>}</td>
                  <td><Link className="btn sm" to="/admin/users">Open</Link></td>
                </tr>
              ))}
            </tbody></table>
          ) : <p className="empty">Nothing needs attention.</p>}
        </div>
        <div className="card">
          <div className="card-head"><h2>System</h2>{sys.data && <span className={`chip ${tone(sys.data.overall)}`}>{sys.data.overall}</span>}{can('system.read') && <><span className="spacer" /><Link className="btn sm" to="/dev">Developer console →</Link></>}</div>
          <dl className="kv2">
            {(['feed', 'engine', 'users', 'incidents'] as const).map((k) => (
              <div key={k} className="kvrow"><dt style={{ textTransform: 'capitalize' }}>{k === 'feed' ? 'Market feed' : k}</dt><dd><span className={`chip ${tone(sys.data?.[k]?.status)}`}>{sys.data?.[k]?.status ?? '…'}</span> <span className="sub">{sys.data?.[k]?.reason}</span></dd></div>
            ))}
          </dl>
        </div>
      </div>

      <div className="card">
        <div className="card-head"><h2>People</h2><span className="spacer" /><Link className="btn sm" to="/admin/users">All users</Link></div>
        <div className="scroll"><table>
          <thead><tr><th className="l">Name</th><th className="l">Role</th><th className="l">Status</th><th className="l">Online</th><th className="l">Strategy</th><th>Unrealized</th><th /></tr></thead>
          <tbody>{rows.map((r) => (
            <tr key={r.id}><td className="l"><b>{r.name}</b> <span className="sub mono">{r.username}</span></td><td className="l">{r.role}</td>
              <td className="l"><span className={`chip ${r.status === 'ACTIVE' ? 'ok' : r.status === 'INVITED' ? 'warn' : 'bad'}`}>{r.status}</span></td>
              <td className="l">{r.online ? <span className="online"><i />online</span> : <span className="sub">offline</span>}</td>
              <td className="l">{r.state === 'BUY' || r.state === 'SELL' ? <span className={`badge ${r.state}`}>{r.state}</span> : <span className="sub">{r.state}</span>}</td>
              <td className={`num ${sign(r.unrealized)}`}>{money(r.unrealized)}</td>
              <td><Link className="btn sm" to={`/admin/users/${r.id}/view/top-bottom`}>View as user</Link></td></tr>
          ))}</tbody>
        </table></div>
      </div>

      {can('audit.read') && (
        <div className="card">
          <div className="card-head"><h2>Latest activity</h2><span className="spacer" /><Link className="btn sm" to="/admin/audit">Audit log</Link></div>
          <table><tbody>{(audit.data?.entries ?? []).map((e) => (
            <tr key={e.id}><td className="l mono dim">{e.ts.replace('T', ' ').slice(0, 19)}</td><td className="l"><span className="chip muted">{e.action}</span></td><td className="l dim">{e.actor_role ?? '–'}</td><td className="l dim">{ago(Date.parse(e.ts) / 1000)}</td></tr>
          ))}</tbody></table>
        </div>
      )}
    </div>
  )
}
