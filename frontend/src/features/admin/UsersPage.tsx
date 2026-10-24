import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, api, get, post } from '../../lib/api'
import { money, sign } from '../../lib/format'
import { useSession } from '../../lib/session'
import { useToast } from '../../lib/toast'
import { UserDrawer } from './UserDrawer'
import './admin.css'

interface Row {
  id: string; username: string; name: string; email: string | null; role: string; status: string; online: boolean; device: string | null; last_active: string | null
  can_view: boolean; instruments: number; open_positions: number; state: string; unrealized: number; live_realized: number; fault: string | null; paused: boolean; strategy_on?: boolean; strategy_locked?: boolean
}
interface Created { setup_url: string; setup_path: string; expires_in_hours: number; user: { username: string; name: string } }

const statusChip = (s: string) => (s === 'ACTIVE' ? 'ok' : s === 'INVITED' ? 'warn' : 'bad')

export default function UsersPage() {
  const qc = useQueryClient()
  const toast = useToast()
  const { me } = useSession()
  const q = useQuery({ queryKey: ['admin', 'live-users'], queryFn: () => get<{ users: Row[] }>('/api/admin/live-users'), refetchInterval: 5000 })
  const [form, setForm] = useState({ name: '', username: '', role: 'user' })
  const [created, setCreated] = useState<Created | null>(null)
  const [err, setErr] = useState('')
  const [picked, setPicked] = useState<string[]>([])
  const [open, setOpen] = useState<Row | null>(null)
  const refresh = () => { void qc.invalidateQueries({ queryKey: ['admin'] }) }

  const create = useMutation({
    mutationFn: () => post<Created>('/api/admin/users', { name: form.name.trim(), username: form.username.trim().toLowerCase(), role: form.role }),
    onSuccess: (d) => { setCreated(d); setForm({ name: '', username: '', role: 'user' }); setErr(''); refresh() },
    onError: (e) => setErr(e instanceof ApiError ? e.message : 'Could not create the user.'),
  })
  const act = useMutation({
    mutationFn: async (v: { id: string; kind: 'disable' | 'enable' | 'remove' | 'logout' | 'reset' | 'strat_on' | 'strat_off' }) => {
      if (v.kind === 'strat_on' || v.kind === 'strat_off') return post(v.id === me?.user.id ? '/api/me/strategy' : `/api/admin/users/${v.id}/strategy`, { enabled: v.kind === 'strat_on' })
      if (v.kind === 'disable') return api(`/api/admin/users/${v.id}`, { method: 'PATCH', json: { status: 'DISABLED' } })
      if (v.kind === 'enable') return api(`/api/admin/users/${v.id}`, { method: 'PATCH', json: { status: 'ACTIVE' } })
      if (v.kind === 'remove') return api(`/api/admin/users/${v.id}`, { method: 'DELETE' })
      if (v.kind === 'logout') return post(`/api/admin/users/${v.id}/force-logout`)
      return post<Created>(`/api/admin/users/${v.id}/reset-totp`)
    },
    onSuccess: (d, v) => {
      toast({ disable: 'User disabled and signed out', enable: 'User enabled', remove: 'User removed', logout: 'User signed out', reset: 'Authenticator reset: share the new setup link', strat_on: 'Strategy switched ON', strat_off: 'Strategy switched OFF' }[v.kind])
      if (v.kind === 'reset') setCreated(d as Created)
      refresh()
    },
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Action failed'),
  })
  const bulk = useMutation({
    mutationFn: (action: 'disable' | 'enable' | 'logout') => post<{ done: string[]; failed: { id: string; reason: string }[] }>('/api/admin/users/bulk', { ids: picked, action }),
    onSuccess: (d) => { toast(`${d.done.length} done${d.failed.length ? `, ${d.failed.length} refused` : ''}`); setPicked([]); refresh() },
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Bulk action failed'),
  })
  const tick = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]))
  const sure = (text: string, fn: () => void) => { if (confirm(text)) fn() }
  const submit = (ev: FormEvent) => {
    ev.preventDefault()
    // our own checks, shown inside the page: no browser pop-up bubble
    if (!form.name.trim()) { setErr('Enter the full name.'); return }
    if (!/^[a-z0-9][a-z0-9._-]{2,31}$/.test(form.username)) { setErr('Username must be 3-32 characters: lower-case letters, digits, dot, dash or underscore.'); return }
    setErr(''); create.mutate()
  }
  const rows = q.data?.users ?? []

  return (
    <div className="page">
      <div className="row" style={{ alignItems: 'center' }}>
        <h1>Users</h1>
        <span className="chip muted">{rows.filter((r) => r.online).length} online · {rows.length} total</span>
        <span className="spacer" />
        <a className="btn sm" href="/api/admin/users.csv" download>Export CSV</a>
        <span className="sub">Nobody can sign up on their own. You add and remove people here.</span>
      </div>

      <form className="card row" onSubmit={submit} noValidate autoComplete="off">
        <label className="field">Full name<input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Rahul Sharma" /></label>
        <label className="field">Username<input required pattern="[a-z0-9][a-z0-9._\-]{2,31}" title="3-32 characters: lower-case letters, digits, . _ -" value={form.username} autoCapitalize="none" autoCorrect="off" autoComplete="off" data-lpignore="true" data-form-type="other" spellCheck={false} maxLength={32} onChange={(e) => setForm({ ...form, username: e.target.value.toLowerCase().replace(/[^a-z0-9._-]/g, '') })} placeholder="rahul" /><small className="sub">3–32 letters, digits, . _ - (no spaces, no capitals)</small></label>
        <label className="field">Role
          <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
            <option value="user">user</option>
            {me?.user.role === 'developer' && <option value="admin">admin</option>}
            {me?.user.owner && <option value="developer">developer</option>}
          </select>
        </label>
        <button className="btn primary" type="submit" disabled={create.isPending}>Add user</button>
        {err && <div className="alert" style={{ flexBasis: '100%' }}>{err}</div>}
      </form>

      {created && (
        <div className="alert ok linkbox">
          <b>One-time setup link for {created.user.name} ({created.user.username})</b>, valid {created.expires_in_hours} hours and usable once. Send it to them privately.
          <div className="linkrow"><input readOnly value={created.setup_url} onFocus={(e) => e.currentTarget.select()} />
            <button className="btn sm" type="button" onClick={() => void navigator.clipboard.writeText(created.setup_url).then(() => toast('Link copied'))}>Copy</button>
            <button className="btn sm" type="button" onClick={() => setCreated(null)}>Hide</button></div>
        </div>
      )}

      {picked.length > 0 && (
        <div className="bulkbar"><b>{picked.length} selected</b><span className="spacer" />
          <button className="btn sm" onClick={() => sure(`Sign ${picked.length} people out?`, () => bulk.mutate('logout'))}>Sign out</button>
          <button className="btn sm" onClick={() => bulk.mutate('enable')}>Enable</button>
          <button className="btn sm danger" onClick={() => sure(`Disable ${picked.length} accounts? They are signed out at once.`, () => bulk.mutate('disable'))}>Disable</button>
          <button className="btn sm" onClick={() => setPicked([])}>Clear</button></div>
      )}
      <div className="card">
        <div className="scroll">
          <table>
            <thead><tr><th /><th className="l">Name</th><th className="l">Role</th><th className="l">Status</th><th className="l">Online</th><th className="l">Device</th><th className="l">Strategy</th><th>Open</th><th>Unrealized</th><th>Realized</th><th>Actions</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td>{r.can_view && r.id !== me?.user.id && r.status !== 'DELETED' && <input type="checkbox" checked={picked.includes(r.id)} onChange={() => tick(r.id)} aria-label={`Select ${r.name}`} />}</td>
                  <td className="l"><b>{r.name}</b> <span className="sub mono">{r.username}</span></td>
                  <td className="l">{r.role}</td>
                  <td className="l"><span className={`chip ${statusChip(r.status)}`}>{r.status}</span></td>
                  <td className="l">{r.online ? <span className="online"><i />online</span> : <span className="sub">offline</span>}</td>
                  <td className="l dim">{r.device ?? '–'}</td>
                  <td className="l">
                    {r.strategy_on === false ? <span className="chip warn">switched off{r.strategy_locked ? ' (admin)' : ''}</span> : r.fault ? <span className="chip bad" title={r.fault}>FAULT</span> : r.paused ? <span className="chip warn">paused</span> : r.state === 'BUY' || r.state === 'SELL' ? <span className={`badge ${r.state}`}>{r.state}</span> : <span className="sub">{r.state}</span>}
                  </td>
                  <td className="num">{r.open_positions}</td>
                  <td className={`num ${sign(r.unrealized)}`}>{money(r.unrealized)}</td>
                  <td className={`num ${sign(r.live_realized)}`}>{money(r.live_realized)}</td>
                  <td>
                    <span className="btnrow acts">
                      {r.id === me?.user.id && (r.strategy_on === false
                        ? <button className="btn sm" disabled={!!r.strategy_locked} title={r.strategy_locked ? 'Switched off by an admin' : undefined} onClick={() => act.mutate({ id: r.id, kind: 'strat_on' })}>Strategy ON</button>
                        : <button className="btn sm" onClick={() => sure('Switch your own strategy OFF? Nothing new is evaluated for you until you switch it back on.', () => act.mutate({ id: r.id, kind: 'strat_off' }))}>Strategy OFF</button>)}
                      {r.can_view && <button className="btn sm" onClick={() => setOpen(r)}>Limits &amp; notes</button>}
                      {r.can_view && <Link className="btn sm primary" to={`/admin/users/${r.id}/view/top-bottom`}>View as user</Link>}
                      {r.can_view && r.status !== 'DELETED' && r.id !== me?.user.id && <>
                        {r.strategy_on === false
                          ? <button className="btn sm" onClick={() => act.mutate({ id: r.id, kind: 'strat_on' })}>Strategy ON</button>
                          : <button className="btn sm" onClick={() => sure(`Switch the strategy of ${r.name} OFF? Nothing new is evaluated for them until you switch it back on.`, () => act.mutate({ id: r.id, kind: 'strat_off' }))}>Strategy OFF</button>}
                        <button className="btn sm" disabled={!r.online} onClick={() => sure(`Sign ${r.name} out now?`, () => act.mutate({ id: r.id, kind: 'logout' }))}>Sign out</button>
                        <button className="btn sm" onClick={() => sure(`Reset the authenticator of ${r.name}? They get a new setup link and are signed out.`, () => act.mutate({ id: r.id, kind: 'reset' }))}>Reset TOTP</button>
                        {r.status === 'DISABLED'
                          ? <button className="btn sm" onClick={() => act.mutate({ id: r.id, kind: 'enable' })}>Enable</button>
                          : <button className="btn sm" onClick={() => sure(`Disable ${r.name}? They are signed out at once; their data is kept.`, () => act.mutate({ id: r.id, kind: 'disable' }))}>Disable</button>}
                        <button className="btn sm danger" onClick={() => sure(`Remove ${r.name}? They can no longer sign in (the data is kept for the audit period).`, () => act.mutate({ id: r.id, kind: 'remove' }))}>Remove</button>
                      </>}
                    </span>
                  </td>
                </tr>
              ))}
              {!rows.length && <tr><td colSpan={11} className="empty">{q.isLoading ? 'Loading…' : 'No users yet.'}</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
      {open && <UserDrawer user={open} canManage={open.id !== me?.user.id} isSelf={open.id === me?.user.id} onClose={() => setOpen(null)} />}
    </div>
  )
}
