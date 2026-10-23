import { useState, type FormEvent } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { ApiError, post } from '../../lib/api'
import { useSession } from '../../lib/session'
import './auth.css'

interface Refusal { message: string; device?: string; last_active?: string; canTakeOver: boolean }

const REASONS: Record<string, string> = {
  idle: 'You were signed out because the page was idle for 30 minutes.',
  expired: 'Your session reached its 12-hour limit. Sign in again.',
  forced: 'An admin signed you out.',
  replaced: 'You were signed out because this account was signed in on another device.',
  disabled: 'This account was disabled.',
}

export function LoginPage() {
  const { mode, me, loading, refresh } = useSession()
  const nav = useNavigate()
  const loc = useLocation()
  const from = (loc.state as { from?: string } | null)?.from ?? '/top-bottom'
  const [f, setF] = useState({ username: '', password: '', totp: '' })
  const [show, setShow] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [refusal, setRefusal] = useState<Refusal | null>(null)
  const reason = new URLSearchParams(loc.search).get('why')

  if (loading) return <div className="auth-page"><div className="auth-card skeleton" style={{ minHeight: 360 }} /></div>
  if (me || mode !== 'platform') return <Navigate to={from} replace />

  async function attempt(replace: boolean) {
    setError('')
    setRefusal(null)
    if (!f.username.trim() || !f.password || !f.totp.trim()) { setError('Enter your username, password and authenticator code.'); return }
    setBusy(true)
    try {
      await post('/api/auth/login', { username: f.username, password: f.password, totp: f.totp, replace_existing: replace })
      await refresh()
      nav(from, { replace: true })
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setRefusal({ message: e.message, device: String(e.body.device ?? ''), last_active: String(e.body.last_active ?? ''), canTakeOver: !!e.body.can_take_over })
      } else if (e instanceof ApiError && e.status === 401) {
        setError('Invalid username, password or code.')
        setF((x) => ({ ...x, totp: '' }))
      } else {
        setError(e instanceof Error ? e.message : 'Sign-in failed. Try again.')
      }
    } finally {
      setBusy(false)
    }
  }
  const submit = (ev: FormEvent) => { ev.preventDefault(); void attempt(false) }
  const set = (k: keyof typeof f) => (ev: { target: { value: string } }) => { setF((x) => ({ ...x, [k]: ev.target.value })); setRefusal(null) }

  return (
    <div className="auth-page">
      <main className="auth-card login">
        <section className="auth-brand">
          <div className="auth-logo"><span className="bars"><b /><b /><b /></span>Algo Trading</div>
          <h1>Smarter trading<br />with <span className="grad">data &amp; strategy</span></h1>
          <p>Analyse. Backtest. Paper trade.<br />Research with real market data, no broker orders.</p>
          <ul>
            <li><b>Paper trading</b><span>Practice without risk</span></li>
            <li><b>Research</b><span>Data-driven insights</span></li>
            <li><b>One device</b><span>Each account signs in on one device</span></li>
          </ul>
        </section>
        <section className="auth-panel">
          <p className="auth-note">Need access? Ask your admin for an invite.</p>
          <form onSubmit={submit} noValidate>
            <h2>Welcome back</h2>
            <p className="sub">Sign in to continue.</p>
            {reason && REASONS[reason] && <div className="alert warn">{REASONS[reason]}</div>}
            <label className="field">Username
              <input autoFocus autoComplete="username" value={f.username} onChange={set('username')} placeholder="Enter your username" />
            </label>
            <label className="field">Password
              <span className="pw">
                <input type={show ? 'text' : 'password'} autoComplete="current-password" value={f.password} onChange={set('password')} placeholder="Enter your password" />
                <button type="button" className="eye" onClick={() => setShow((s) => !s)} aria-label={show ? 'Hide password' : 'Show password'}>{show ? 'Hide' : 'Show'}</button>
              </span>
            </label>
            <label className="field">Authenticator code
              <input inputMode="numeric" autoComplete="one-time-code" maxLength={20} value={f.totp} onChange={set('totp')} placeholder="6-digit code or a backup code" />
            </label>
            <p className="sub">Forgot your password or lost your phone? Ask your admin to reset it; you will get a new setup link.</p>
            {error && <div className="alert" role="alert">{error}</div>}
            {refusal && (
              <div className="alert warn" role="alert">
                {refusal.message}
                {refusal.device && <> ({refusal.device}, last active {refusal.last_active?.replace('T', ' ').slice(0, 16)})</>}
                {refusal.canTakeOver && <button type="button" className="btn" style={{ marginTop: 8, width: '100%' }} disabled={busy} onClick={() => void attempt(true)}>Sign out the other device and sign in here</button>}
              </div>
            )}
            <button className="btn primary big" type="submit" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
          </form>
          <p className="auth-foot">Paper trading and research only. No broker orders.</p>
        </section>
      </main>
    </div>
  )
}
