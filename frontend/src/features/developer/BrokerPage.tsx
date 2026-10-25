import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { api, get, ApiError } from '../../lib/api'
import { useToast } from '../../lib/toast'
import './developer.css'

interface Feed { connected: boolean; last_tick_age_s: number | null; reconnects: number; subscribed: number; error: string | null }
interface Broker {
  configured: boolean; source: 'saved' | 'env' | 'none'; client_code: string | null; connected_at: string | null
  updated_by: string | null; encrypted: boolean; feed: Feed | null
}
const EMPTY = { api_key: '', client_code: '', pin: '', totp_secret: '' }

function when(iso: string | null): string {
  if (!iso) return '-'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', dateStyle: 'medium', timeStyle: 'short' }) + ' IST'
}

function Status({ b }: { b: Broker }) {
  if (!b.configured) return <span className="chip muted big">Not connected</span>
  if (!b.feed) return <span className="chip ok big">Details saved</span>
  if (b.feed.connected) return <span className="chip ok big">Live data connected</span>
  return <span className="chip warn big">Details saved, live feed not connected</span>
}

export default function BrokerPage() {
  const qc = useQueryClient()
  const toast = useToast()
  const [form, setForm] = useState(EMPTY)
  const [error, setError] = useState<string | null>(null)
  const q = useQuery({ queryKey: ['dev', 'broker'], queryFn: () => get<Broker>('/api/dev/broker'), refetchInterval: 5000 })
  const done = (msg: string) => (b: Broker) => { qc.setQueryData(['dev', 'broker'], b); setError(null); toast(msg) }
  const fail = (e: unknown) => setError(e instanceof ApiError ? e.message : 'Something went wrong. Try again.')
  const connect = useMutation({
    mutationFn: () => api<Broker>('/api/dev/broker', { method: 'PUT', json: form }),
    onSuccess: (b) => { setForm(EMPTY); done('Connected: the live feed is reconnecting with these details')(b) },
    onError: fail,
  })
  const disconnect = useMutation({
    mutationFn: () => api<Broker>('/api/dev/broker', { method: 'DELETE' }),
    onSuccess: done('Saved details removed'),
    onError: fail,
  })
  const b = q.data
  const filled = Object.values(form).every((v) => v.trim().length > 0)
  const set = (k: keyof typeof EMPTY) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value })
  const submit = (e: FormEvent) => { e.preventDefault(); if (filled && !connect.isPending) connect.mutate() }
  const age = b?.feed?.last_tick_age_s

  return (
    <div className="page">
      <h1>Angel One connection</h1>
      <p className="sub">Enter the broker details here instead of editing files on the server. They are tested with a real login first and saved only if Angel One accepts them; the live feed then reconnects by itself. Changes are written to the audit log.</p>

      <div className="card">
        <div className="broker-status">
          {b ? <Status b={b} /> : <span className="chip muted big">Checking…</span>}
          {b?.configured && (
            <dl className="broker-facts">
              <div><dt>Client code</dt><dd className="mono">{b.client_code}</dd></div>
              <div><dt>Details from</dt><dd>{b.source === 'saved' ? `saved here${b.updated_by ? ` by ${b.updated_by}` : ''}` : 'the server settings (.env)'}</dd></div>
              <div><dt>Last connected here</dt><dd>{when(b.connected_at)}</dd></div>
              {b.feed && <div><dt>Last price</dt><dd>{age == null ? 'none yet' : `${age.toFixed(0)} s ago`}</dd></div>}
              {b.feed && <div><dt>Prices followed</dt><dd>{b.feed.subscribed}</dd></div>}
              <div><dt>Stored</dt><dd>{b.source === 'saved' ? (b.encrypted ? 'encrypted' : 'plain text (older file: save again to encrypt)') : 'in the server environment'}</dd></div>
            </dl>
          )}
        </div>
        {b?.feed?.error && !b.feed.connected && <div className="alert warn" style={{ marginTop: 10 }}>Feed message: {b.feed.error}</div>}
        {b?.configured && b.source === 'saved' && (
          <div style={{ marginTop: 12 }}>
            <button type="button" className="btn danger sm" disabled={disconnect.isPending}
              onClick={() => { if (window.confirm('Remove the saved Angel One details? Live data stops unless the server settings (.env) have them.')) disconnect.mutate() }}>
              Remove saved details
            </button>
          </div>
        )}
      </div>

      <form className="card" onSubmit={submit} autoComplete="off">
        <h2>{b?.configured ? 'Change the details' : 'Connect Angel One'}</h2>
        <p className="sub" style={{ margin: '4px 0 12px' }}>From your Angel One SmartAPI account. After saving, only the masked client code is ever shown again; the key, PIN and secret stay encrypted on the server.</p>
        <div className="form-grid">
          <label className="field">API key
            <input type="password" value={form.api_key} onChange={set('api_key')} autoComplete="new-password" spellCheck={false} placeholder="from the SmartAPI app page" />
          </label>
          <label className="field">Client code
            <input value={form.client_code} onChange={set('client_code')} autoComplete="off" spellCheck={false} placeholder="your Angel One ID, e.g. A123456" />
          </label>
          <label className="field">PIN
            <input type="password" inputMode="numeric" value={form.pin} onChange={set('pin')} autoComplete="new-password" placeholder="your trading PIN (MPIN)" />
          </label>
          <label className="field">TOTP secret
            <input type="password" value={form.totp_secret} onChange={set('totp_secret')} autoComplete="new-password" spellCheck={false} placeholder="the long key beside the QR code" />
          </label>
        </div>
        <p className="sub" style={{ margin: '10px 0 0' }}>The TOTP secret is the long key (letters and digits) Angel One shows when you enable TOTP, not the 6-digit code.</p>
        {error && <div className="alert" role="alert" style={{ marginTop: 12 }}>{error}</div>}
        <div style={{ marginTop: 14, display: 'flex', gap: 10, alignItems: 'center' }}>
          <button className="btn primary" type="submit" disabled={!filled || connect.isPending}>{connect.isPending ? 'Testing the login…' : 'Test and connect'}</button>
          {connect.isPending && <span className="dim">Logging in to Angel One, this takes a few seconds.</span>}
        </div>
      </form>
    </div>
  )
}
