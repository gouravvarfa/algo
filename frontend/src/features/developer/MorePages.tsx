import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { post, get } from '../../lib/api'
import { dt } from '../../lib/format'
import { useToast } from '../../lib/toast'
import './developer.css'

// ---------------------------------------------------------------- Sessions
interface Sess { user_id: string; username: string; role: string; device: string; started_at: string; last_seen_at: string; online: boolean; idle_s: number }

export function SessionsPage() {
  const qc = useQueryClient()
  const toast = useToast()
  const q = useQuery({ queryKey: ['dev', 'sessions'], queryFn: () => get<{ sessions: Sess[] }>('/api/dev/sessions'), refetchInterval: 5000 })
  const out = useMutation({ mutationFn: (id: string) => post(`/api/admin/users/${id}/force-logout`), onSuccess: () => { toast('Signed out'); void qc.invalidateQueries({ queryKey: ['dev', 'sessions'] }) }, onError: (e) => toast((e as Error).message) })
  const rows = q.data?.sessions ?? []
  return (
    <div className="page">
      <h1>Sessions</h1>
      <p className="sub">Every account that is signed in right now. A page that stops pinging for 2 minutes counts as gone (offline). Every session ends at 4:00 pm IST; there is no idle timeout.</p>
      <div className="card"><div className="scroll"><table>
        <thead><tr><th className="l">User</th><th className="l">Role</th><th className="l">Device</th><th className="l">Signed in</th><th className="l">Last ping</th><th>Idle</th><th className="l">State</th><th /></tr></thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.user_id}>
              <td className="l"><b>{s.username}</b></td><td className="l">{s.role}</td><td className="l">{s.device}</td><td className="l">{dt(s.started_at)}</td><td className="l">{dt(s.last_seen_at)}</td>
              <td className="num">{Math.round(s.idle_s / 60)} min</td>
              <td className="l">{s.online ? <span className="chip ok">online</span> : <span className="chip muted">gone quiet</span>}</td>
              <td><button className="btn sm danger" onClick={() => { if (confirm(`Sign ${s.username} out of this device now?`)) out.mutate(s.user_id) }}>Sign out</button></td>
            </tr>
          ))}
          {!rows.length && <tr><td colSpan={8} className="empty">{q.isLoading ? 'Loading…' : 'Nobody is signed in.'}</td></tr>}
        </tbody>
      </table></div></div>
    </div>
  )
}

// ---------------------------------------------------------------- Login attempts
interface Att { id: number; ts: string; action: string; actor_id: string | null; detail: string | null }
const ATT_TONE: Record<string, string> = { 'login.success': 'ok', 'user.setup_complete': 'ok', 'login.failed': 'bad', 'login.locked': 'bad', 'login.refused': 'warn', 'login.takeover': 'warn', 'password.change_failed': 'bad' }

export function LoginAttemptsPage() {
  const [failed, setFailed] = useState(false)
  const q = useQuery({ queryKey: ['dev', 'attempts', failed], queryFn: () => get<{ failed_last_hour: number; entries: Att[] }>(`/api/dev/login-attempts?only_failed=${failed}`), refetchInterval: 10_000 })
  const detail = (d: string | null) => { try { const o = JSON.parse(d ?? '{}') as Record<string, string>; return [o.username && `user ${o.username}`, o.reason, o.ip && `from ${o.ip}`, o.device].filter(Boolean).join(' · ') } catch { return d ?? '' } }
  return (
    <div className="page">
      <div className="row" style={{ alignItems: 'center' }}>
        <h1>Login attempts</h1>{q.data && <span className={`chip ${q.data.failed_last_hour >= 10 ? 'bad' : q.data.failed_last_hour ? 'warn' : 'ok'}`}>{q.data.failed_last_hour} failed in the last hour</span>}
        <span className="spacer" /><label className="inline"><input type="checkbox" checked={failed} onChange={(e) => setFailed(e.target.checked)} /> failed only</label>
      </div>
      <p className="sub">Five wrong attempts lock a username (and an address) for 15 minutes. The message shown to the person never says which part was wrong.</p>
      <div className="card"><div className="scroll"><table>
        <thead><tr><th className="l">When (UTC)</th><th className="l">What happened</th><th className="l">Details</th></tr></thead>
        <tbody>
          {(q.data?.entries ?? []).map((e) => <tr key={e.id}><td className="l mono">{e.ts.replace('T', ' ').slice(0, 19)}</td><td className="l"><span className={`chip ${ATT_TONE[e.action] ?? 'muted'}`}>{e.action}</span></td><td className="l dim wrap">{detail(e.detail)}</td></tr>)}
          {!q.data?.entries.length && <tr><td colSpan={3} className="empty">{q.isLoading ? 'Loading…' : 'Nothing recorded.'}</td></tr>}
        </tbody>
      </table></div></div>
    </div>
  )
}

// ---------------------------------------------------------------- Roles and permissions
export function RolesPage() {
  const q = useQuery({ queryKey: ['dev', 'roles'], queryFn: () => get<{ roles: { role: string; rank: number; permissions: string[] }[]; rules: string[] }>('/api/dev/roles') })
  const all = [...new Set((q.data?.roles ?? []).flatMap((r) => r.permissions))].sort()
  return (
    <div className="page">
      <h1>Roles &amp; permissions</h1>
      <div className="card"><div className="scroll"><table>
        <thead><tr><th className="l">Permission</th>{(q.data?.roles ?? []).map((r) => <th key={r.role}>{r.role}</th>)}</tr></thead>
        <tbody>{all.map((p) => <tr key={p}><td className="l mono">{p}</td>{q.data!.roles.map((r) => <td key={r.role}>{r.permissions.includes(p) ? <span className="pos">✔</span> : <span className="dim">–</span>}</td>)}</tr>)}</tbody>
      </table></div></div>
      <div className="card"><h2>Rules</h2><ul className="rules">{(q.data?.rules ?? []).map((r) => <li key={r}>{r}</li>)}</ul></div>
    </div>
  )
}

// ---------------------------------------------------------------- System info
interface Info { python: string; platform: string; fastapi: string; uptime_s: number; data_dir: string; databases: { name: string; mb: number }[]; counts: Record<string, number>; checks: { name: string; ok: boolean; note: string }[] }
const upt = (s: number) => (s < 3600 ? `${Math.round(s / 60)} min` : s < 172800 ? `${(s / 3600).toFixed(1)} h` : `${Math.round(s / 86400)} days`)

export function SystemInfoPage() {
  const q = useQuery({ queryKey: ['dev', 'info'], queryFn: () => get<Info>('/api/dev/system-info'), refetchInterval: 30_000 })
  const d = q.data
  return (
    <div className="page">
      <h1>System info</h1>
      {d && (
        <>
          <div className="kpis">
            <div className="kpi"><span className="k">Uptime</span><b>{upt(d.uptime_s)}</b></div><div className="kpi"><span className="k">Python</span><b>{d.python}</b></div><div className="kpi"><span className="k">FastAPI</span><b>{d.fastapi}</b></div>
            {Object.entries(d.counts).map(([k, v]) => <div className="kpi" key={k}><span className="k">{k.replace(/_/g, ' ')}</span><b>{v}</b></div>)}
          </div>
          <div className="card"><h2>Before putting this on a server</h2>
            <table><tbody>{d.checks.map((c) => <tr key={c.name}><td className="l"><span className={`chip ${c.ok ? 'ok' : 'warn'}`}>{c.ok ? 'set' : 'check'}</span></td><td className="l"><b>{c.name}</b></td><td className="l dim wrap">{c.note}</td></tr>)}</tbody></table></div>
          <div className="card"><h2>Databases</h2><p className="sub">{d.data_dir} · back up this whole folder.</p>
            <table><thead><tr><th className="l">File</th><th>Size (MB)</th></tr></thead><tbody>{d.databases.map((f) => <tr key={f.name}><td className="l mono">{f.name}</td><td className="num">{f.mb}</td></tr>)}</tbody></table></div>
          <p className="sub">{d.platform}</p>
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- Live logs
export function LiveLogsPage() {
  const [level, setLevel] = useState('')
  const [follow, setFollow] = useState(true)
  const q = useQuery({ queryKey: ['dev', 'logs', level], queryFn: () => get<{ available: boolean; note?: string; size_kb?: number; lines: string[] }>(`/api/dev/live-logs?lines=400&level=${level}`), refetchInterval: follow ? 3000 : false })
  const box = useRef<HTMLPreElement>(null)
  useEffect(() => { if (follow && box.current) box.current.scrollTop = box.current.scrollHeight }, [q.data, follow])
  return (
    <div className="page">
      <div className="row" style={{ alignItems: 'center' }}>
        <h1>Live logs</h1><span className="spacer" />
        <select value={level} onChange={(e) => setLevel(e.target.value)}><option value="">all lines</option><option value="WARNING">warnings</option><option value="ERROR">errors</option><option value="INFO">info</option></select>
        <label className="inline"><input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> follow (3 s)</label>
        {q.data?.size_kb != null && <span className="sub">{q.data.size_kb} KB</span>}
      </div>
      <p className="sub">Passwords, tokens, cookies and authenticator secrets are masked before they leave the server.</p>
      <div className="card" style={{ padding: 0 }}>
        {q.data && !q.data.available ? <p className="empty">{q.data.note}</p> : <pre className="logbox" ref={box}>{(q.data?.lines ?? []).join('\n') || 'No lines.'}</pre>}
      </div>
    </div>
  )
}
