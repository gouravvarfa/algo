import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { del, get, post, put } from '../../lib/api'

interface Status { configured: boolean; client_code?: string; mode: string; status: string; status_message: string | null; last_checked_at?: number | null }
interface Summary {
  funds: { available_cash: number | null; net: number | null; span_margin: number | null; exposure_margin: number | null; m2m_realised: number | null; m2m_unrealised: number | null }
  positions: { symbol: string; exchange: string; product: string; net_qty: number | null; avg_price: number | null; ltp: number | null; pnl: number | null }[]
  open_positions: { symbol: string }[]
  orders: { order_id: string; symbol: string; side: string; qty: number | null; filled: number | null; status: string | null; time: string | null }[]
  note: string
}

const inr = (v: number | null | undefined) => (v == null ? '–' : '₹' + v.toLocaleString('en-IN', { maximumFractionDigits: 2 }))
const LABEL: Record<string, string> = { not_set: 'Not connected', untested: 'Saved, not checked yet', connected: 'Connected', error: 'Login failed' }
const msg = (e: unknown) => (e instanceof Error ? e.message : 'Something went wrong')

/** The user's own Angel One account: they enter their details once; the server logs in for them every time. Read-only: no order can be placed from here. */
export function BrokerCard() {
  const qc = useQueryClient()
  const st = useQuery({ queryKey: ['broker'], queryFn: () => get<Status>('/api/me/broker') })
  const [open, setOpen] = useState(false)
  const [f, setF] = useState({ client_code: '', api_key: '', pin: '', totp_secret: '' })
  const [err, setErr] = useState('')
  const refresh = () => qc.invalidateQueries({ queryKey: ['broker'] })
  const save = useMutation({
    mutationFn: () => put<Status>('/api/me/broker', f),
    onSuccess: () => { setF({ client_code: '', api_key: '', pin: '', totp_secret: '' }); setOpen(false); setErr(''); refresh() },
    onError: (e) => setErr(msg(e)),
  })
  const test = useMutation({ mutationFn: () => post<Status>('/api/me/broker/test'), onSuccess: () => { setErr(''); refresh() }, onError: (e) => { setErr(msg(e)); refresh() } })
  const remove = useMutation({ mutationFn: () => del('/api/me/broker'), onSuccess: () => { setErr(''); qc.removeQueries({ queryKey: ['broker-summary'] }); refresh() } })
  const sum = useMutation({ mutationFn: () => get<Summary>('/api/me/broker/summary'), onError: (e) => setErr(msg(e)) })
  const s = st.data
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value })
  const ready = f.client_code && f.api_key && f.pin && f.totp_secret
  return (
    <div className="card">
      <div className="card-head"><h2>Angel One account</h2><span className="spacer" />
        {s && <span className={'chip ' + (s.status === 'connected' ? 'ok' : s.status === 'error' ? 'bad' : 'warn')}>{LABEL[s.status] ?? s.status}</span>}</div>
      <p className="sub">Connect your own Angel One account so this app can read it for you (funds, positions, orders). This screen is <b>read-only</b>: it cannot place, change or cancel an order, and
        your account stays in <b>Paper</b> mode.</p>
      {s?.configured && !open && (
        <dl className="kv">
          <dt>Client code</dt><dd>{s.client_code}</dd>
          <dt>Mode</dt><dd>Paper (real orders are not switched on)</dd>
          {s.status_message && <><dt>Last problem</dt><dd className="err">{s.status_message}</dd></>}
        </dl>
      )}
      {(open || !s?.configured) && (
        <form className="col" style={{ gap: 8, maxWidth: 420 }} autoComplete="off" onSubmit={(e) => { e.preventDefault(); if (ready) save.mutate() }}>
          <label className="inline">Client code<input value={f.client_code} onChange={set('client_code')} autoComplete="off" placeholder="e.g. A123456" /></label>
          <label className="inline">SmartAPI key<input type="password" value={f.api_key} onChange={set('api_key')} autoComplete="new-password" /></label>
          <label className="inline">Trading PIN<input type="password" inputMode="numeric" value={f.pin} onChange={set('pin')} autoComplete="new-password" /></label>
          <label className="inline">TOTP secret<input type="password" value={f.totp_secret} onChange={set('totp_secret')} autoComplete="new-password" placeholder="the letters shown next to the QR code" /></label>
          <p className="note">Saved encrypted on the server and never shown again, not even to an admin. To change anything, enter all four again.</p>
          <div className="row"><button className="btn primary" disabled={!ready || save.isPending}>{save.isPending ? 'Saving…' : 'Save'}</button>
            {s?.configured && <button type="button" className="btn" onClick={() => { setOpen(false); setErr('') }}>Cancel</button>}</div>
        </form>
      )}
      {s?.configured && !open && (
        <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
          <button className="btn" disabled={test.isPending} onClick={() => test.mutate()}>{test.isPending ? 'Checking…' : 'Test connection'}</button>
          <button className="btn" disabled={sum.isPending} onClick={() => sum.mutate()}>{sum.isPending ? 'Reading…' : 'View my account'}</button>
          <button className="btn" onClick={() => setOpen(true)}>Change details</button>
          <button className="btn danger" onClick={() => { if (window.confirm('Remove your Angel One details from this app?')) remove.mutate() }}>Remove</button>
        </div>
      )}
      {err && <p className="err" role="alert">{err}</p>}
      {sum.data && (
        <div style={{ marginTop: 12 }}>
          <dl className="kv">
            <dt>Available cash</dt><dd>{inr(sum.data.funds.available_cash)}</dd>
            <dt>Margin used</dt><dd>SPAN {inr(sum.data.funds.span_margin)} · Exposure {inr(sum.data.funds.exposure_margin)}</dd>
            <dt>Today's P&amp;L</dt><dd>Realised {inr(sum.data.funds.m2m_realised)} · Unrealised {inr(sum.data.funds.m2m_unrealised)}</dd>
            <dt>Open positions</dt><dd>{sum.data.open_positions.length}</dd>
          </dl>
          {sum.data.positions.length > 0 && (
            <table className="tbl"><thead><tr><th>Symbol</th><th>Qty</th><th>Avg</th><th>LTP</th><th>P&amp;L</th></tr></thead>
              <tbody>{sum.data.positions.map((p, i) => (<tr key={i}><td>{p.symbol}</td><td>{p.net_qty ?? '–'}</td><td>{inr(p.avg_price)}</td><td>{inr(p.ltp)}</td><td>{inr(p.pnl)}</td></tr>))}</tbody></table>
          )}
          {sum.data.orders.length > 0 && (
            <details style={{ marginTop: 8 }}><summary>{sum.data.orders.length} orders today</summary>
              <table className="tbl"><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Status</th></tr></thead>
                <tbody>{sum.data.orders.map((o) => (<tr key={o.order_id}><td>{o.time ?? ''}</td><td>{o.symbol}</td><td>{o.side}</td><td>{o.filled ?? 0}/{o.qty ?? '–'}</td><td>{o.status}</td></tr>))}</tbody></table></details>
          )}
          <p className="note">{sum.data.note}</p>
        </div>
      )}
    </div>
  )
}
