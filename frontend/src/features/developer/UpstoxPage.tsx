import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, get, ApiError } from '../../lib/api'
import { useToast } from '../../lib/toast'
import './developer.css'

type State = 'none' | 'app_only' | 'connected' | 'expired'
interface Upstox {
  state: State; api_key: string | null; redirect_uri: string | null; user_id: string | null; user_name: string | null
  connected_at: string | null; token_expires_at: string | null; updated_by: string | null
}
interface Trade { trade_id?: string; trading_symbol?: string; transaction_type?: string; quantity?: number; average_price?: number; price?: number; order_timestamp?: string; exchange_timestamp?: string; trade_time?: string; exchange?: string; product?: string }

/** The address Upstox sends the person back to: this very page. It must be registered, exactly like this, in the Upstox app. */
const HERE = `${window.location.origin}/app/dev/upstox`

function when(iso: string | null): string {
  if (!iso) return '-'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', dateStyle: 'medium', timeStyle: 'short' }) + ' IST'
}
const say = (e: unknown) => (e instanceof ApiError ? e.message : 'Something went wrong. Try again.')

function Chip({ s }: { s: State }) {
  if (s === 'connected') return <span className="chip ok big">Connected</span>
  if (s === 'expired') return <span className="chip warn big">Login ended: connect again</span>
  if (s === 'app_only') return <span className="chip warn big">App saved, not logged in</span>
  return <span className="chip muted big">Not set up</span>
}

export default function UpstoxPage() {
  const qc = useQueryClient()
  const toast = useToast()
  const nav = useNavigate()
  const [params] = useSearchParams()
  const [form, setForm] = useState({ api_key: '', api_secret: '', redirect_uri: HERE })
  const [error, setError] = useState<string | null>(null)
  const q = useQuery({ queryKey: ['dev', 'upstox'], queryFn: () => get<Upstox>('/api/dev/upstox'), refetchInterval: 30000 })
  const b = q.data
  const apply = (msg: string) => (s: Upstox) => { qc.setQueryData(['dev', 'upstox'], s); setError(null); toast(msg) }

  const saveApp = useMutation({
    mutationFn: () => api<Upstox>('/api/dev/upstox/app', { method: 'PUT', json: form }),
    onSuccess: (s) => { setForm({ api_key: '', api_secret: '', redirect_uri: form.redirect_uri }); apply('Upstox app saved')(s) },
    onError: (e) => setError(say(e)),
  })
  const login = useMutation({
    mutationFn: () => api<{ url: string }>('/api/dev/upstox/login', { method: 'POST', json: {} }),
    onSuccess: (r) => { window.location.assign(r.url) },
    onError: (e) => setError(say(e)),
  })
  const callback = useMutation({
    mutationFn: (v: { code: string; state: string }) => api<Upstox>('/api/dev/upstox/callback', { method: 'POST', json: v }),
    onSuccess: apply('Connected to Upstox'),
    onError: (e) => setError(say(e)),
  })
  const disconnect = useMutation({
    mutationFn: () => api<Upstox>('/api/dev/upstox', { method: 'DELETE' }),
    onSuccess: apply('Upstox details removed'),
    onError: (e) => setError(say(e)),
  })
  const trades = useMutation({ mutationFn: () => get<{ count: number; trades: Trade[] }>('/api/dev/upstox/trades'), onError: (e) => setError(say(e)) })

  // Upstox sends the person back here with ?code=...&state=...: hand them to the server once, then clean the address bar.
  const handled = useRef(false)
  useEffect(() => {
    const code = params.get('code'), state = params.get('state'), denied = params.get('error')
    if (handled.current || (!code && !denied)) return
    handled.current = true
    nav('/dev/upstox', { replace: true })
    if (denied) setError(`Upstox did not log you in (${params.get('error_description') ?? denied}). Press Connect with Upstox to try again.`)
    else if (code && state) callback.mutate({ code, state })
    else setError('The way back from Upstox was incomplete. Press Connect with Upstox to try again.')
  }, [params, nav, callback])

  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value })
  const filled = form.api_key.trim() && form.api_secret.trim() && form.redirect_uri.trim()
  const submit = (e: FormEvent) => { e.preventDefault(); if (filled && !saveApp.isPending) saveApp.mutate() }
  const hasApp = b != null && b.state !== 'none'
  const rows = trades.data?.trades ?? []

  return (
    <div className="page">
      <h1>Upstox connection</h1>
      <p className="sub">Upstox logs in on its own page, so there is no PIN to type here. Save the app details once, then press Connect with Upstox. The login ends every day at about 03:30 IST: press Connect again each trading day. This page only reads; it never places an order. Changes are written to the audit log.</p>

      <div className="card">
        <div className="broker-status">
          {b ? <Chip s={b.state} /> : <span className="chip muted big">Checking…</span>}
          {hasApp && (
            <dl className="broker-facts">
              <div><dt>App key</dt><dd className="mono">{b.api_key}</dd></div>
              <div><dt>Redirect address</dt><dd className="mono">{b.redirect_uri}</dd></div>
              {b.user_id && <div><dt>Logged in as</dt><dd>{b.user_name ? `${b.user_name} (${b.user_id})` : b.user_id}</dd></div>}
              {b.connected_at && <div><dt>Connected</dt><dd>{when(b.connected_at)}</dd></div>}
              {b.token_expires_at && b.state !== 'app_only' && <div><dt>Login lasts until</dt><dd>{when(b.token_expires_at)}</dd></div>}
              {b.updated_by && <div><dt>Changed by</dt><dd>{b.updated_by}</dd></div>}
            </dl>
          )}
        </div>
        {callback.isPending && <p className="dim" style={{ marginTop: 10 }}>Finishing the login with Upstox…</p>}
        {error && <div className="alert" role="alert" style={{ marginTop: 12 }}>{error}</div>}
        {hasApp && (
          <div style={{ marginTop: 14, display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            <button type="button" className="btn primary" disabled={login.isPending || callback.isPending} onClick={() => login.mutate()}>
              {b.state === 'connected' ? 'Log in again' : 'Connect with Upstox'}
            </button>
            {b.state === 'connected' && <button type="button" className="btn" disabled={trades.isPending} onClick={() => trades.mutate()}>{trades.isPending ? 'Asking Upstox…' : "Show today's trades"}</button>}
            <button type="button" className="btn danger" disabled={disconnect.isPending}
              onClick={() => { if (window.confirm('Remove the saved Upstox details and the login?')) disconnect.mutate() }}>Remove details</button>
          </div>
        )}
      </div>

      {trades.data && (
        <div className="card">
          <h2>Today's trades ({trades.data.count})</h2>
          {rows.length === 0 ? <p className="sub">No trades today. The connection works.</p> : (
            <div className="scroll">
              <table>
                <thead><tr><th className="l">Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th className="l">Time</th></tr></thead>
                <tbody>
                  {rows.map((t, i) => (
                    <tr key={t.trade_id ?? i}>
                      <td className="l mono">{t.trading_symbol ?? '-'}</td><td>{t.transaction_type ?? '-'}</td><td>{t.quantity ?? '-'}</td>
                      <td>{t.average_price ?? t.price ?? '-'}</td><td className="l dim">{t.exchange_timestamp ?? t.order_timestamp ?? t.trade_time ?? '-'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      <form className="card" onSubmit={submit} autoComplete="off">
        <h2>{hasApp ? 'Change the app details' : 'Save the Upstox app details'}</h2>
        <p className="sub" style={{ margin: '4px 0 12px' }}>From the developer page of your Upstox account (My Apps). After saving, only a masked key is shown; the secret stays encrypted on the server and never comes back.</p>
        <div className="form-grid">
          <label className="field">API key
            <input type="password" value={form.api_key} onChange={set('api_key')} autoComplete="new-password" spellCheck={false} placeholder="the app's API key" />
          </label>
          <label className="field">API secret
            <input type="password" value={form.api_secret} onChange={set('api_secret')} autoComplete="new-password" spellCheck={false} placeholder="the app's API secret" />
          </label>
          <label className="field">Redirect address
            <input value={form.redirect_uri} onChange={set('redirect_uri')} autoComplete="off" spellCheck={false} />
          </label>
        </div>
        <p className="sub" style={{ margin: '10px 0 0' }}>Register this exact redirect address in your Upstox app: <span className="mono">{HERE}</span>. On the server it must be your https address, for example https://your-domain/app/dev/upstox.</p>
        <div style={{ marginTop: 14 }}>
          <button className="btn primary" type="submit" disabled={!filled || saveApp.isPending}>{saveApp.isPending ? 'Saving…' : 'Save app details'}</button>
        </div>
      </form>
    </div>
  )
}
