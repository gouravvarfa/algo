import { useQuery } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { ApiError, get, post } from '../../lib/api'
import { useSession } from '../../lib/session'
import { dt } from '../../lib/format'

export function AccountPage() {
  const { me, mode, logout } = useSession()
  const [cur, setCur] = useState('')
  const [n1, setN1] = useState('')
  const [n2, setN2] = useState('')
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const activity = useQuery({ queryKey: ['me', 'activity'], queryFn: () => get<{ views: { at: string; by: string }[] }>('/api/me/activity'), enabled: mode === 'platform' })

  async function change(ev: FormEvent) {
    ev.preventDefault()
    setMsg(null)
    if (n1 !== n2) { setMsg({ ok: false, text: 'The two new passwords are not the same.' }); return }
    setBusy(true)
    try {
      await post('/api/auth/change-password', { current_password: cur, new_password: n1 })
      setMsg({ ok: true, text: 'Password changed.' })
      setCur(''); setN1(''); setN2('')
    } catch (e) {
      setMsg({ ok: false, text: e instanceof ApiError ? e.message : 'Could not change the password.' })
    } finally {
      setBusy(false)
    }
  }

  if (mode !== 'platform' || !me) return <div className="page"><div className="card"><h1>Account</h1><p className="sub" style={{ marginTop: 8 }}>No login is configured on this server, so there is no account to manage.</p></div></div>

  return (
    <div className="page" style={{ maxWidth: 760 }}>
      <h1>Your account</h1>
      <div className="card">
        <dl className="kv">
          <dt>Name</dt><dd>{me.user.name}</dd>
          <dt>Username</dt><dd>{me.user.username}</dd>
          <dt>Role</dt><dd>{me.user.role}{me.user.owner ? ' (owner)' : ''}</dd>
          <dt>This device</dt><dd>{me.session.device}</dd>
          <dt>Signed in</dt><dd>{dt(me.session.started_at)}</dd>
        </dl>
        <div style={{ marginTop: 12 }}><button className="btn danger" onClick={() => void logout()}>Sign out</button></div>
      </div>
      <form className="card stack" onSubmit={change}>
        <h2>Change password</h2>
        <label className="field">Current password<input type="password" autoComplete="current-password" required value={cur} onChange={(e) => setCur(e.target.value)} /></label>
        <label className="field">New password (at least 12 characters)<input type="password" autoComplete="new-password" minLength={12} required value={n1} onChange={(e) => setN1(e.target.value)} /></label>
        <label className="field">Repeat new password<input type="password" autoComplete="new-password" minLength={12} required value={n2} onChange={(e) => setN2(e.target.value)} /></label>
        {msg && <div className={'alert' + (msg.ok ? ' ok' : '')} role="status">{msg.text}</div>}
        <div><button className="btn primary" type="submit" disabled={busy}>Change password</button></div>
      </form>
      <div className="card">
        <h2>Account activity</h2>
        <p className="sub" style={{ margin: '4px 0 10px' }}>Whenever an admin opens your dashboard (read-only), it is listed here.</p>
        {activity.data?.views.length ? (
          <table><thead><tr><th className="l">When</th><th className="l">Opened by</th></tr></thead>
            <tbody>{activity.data.views.map((v, i) => <tr key={i}><td className="l">{dt(v.at)}</td><td className="l">{v.by}</td></tr>)}</tbody></table>
        ) : <p className="empty">Nobody has opened your dashboard.</p>}
      </div>
    </div>
  )
}
