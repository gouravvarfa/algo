import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Fragment, useState } from 'react'
import { ApiError, del, get, post, put } from '../../lib/api'
import { useToast } from '../../lib/toast'
import './admin.css'

interface Acct {
  user_id: string; username: string | null; name: string | null; client_code: string; status: string; status_message: string | null; last_checked_at: number | null
  mode: string; effective_mode: string; real_allowed: boolean; real_blockers: string[]; max_lots: number; max_daily_loss: number | null; allowed_symbols: string[]
  mode_changed_at: number | null; mode_changed_by: string | null
}
interface AdminOrder { id: string; username: string | null; name: string | null; side: string; lots: number; symbol: string; strike: number; opt: string; expiry: string; price: number; intent: string; status: string; broker_order_id: string | null; message: string | null; created_at: number }
interface TbRow { user_id: string; username: string | null; name: string | null; symbol: string; position: { side: string; qty: number; contract: string; entry_price: number | null } | null; pending_entry: boolean; halted: string | null; released: boolean; stop_target: number | null }
interface TbOrder { id: string; username: string | null; name: string | null; symbol: string; contract: string; role: string; side: string; qty: number; order_type: string; price: number | null; trigger_price: number | null; status: string; message: string | null; created_at: number }
interface Overview { master_on: boolean; can_turn_on: boolean; accounts: Acct[]; counts: { connected: number; allowed: number; real_now: number; total: number } }

const SYMS = ['NIFTY', 'BANKNIFTY']
const STATUS: Record<string, string> = { connected: 'Connected', error: 'Login failed', untested: 'Not checked' }
const when = (t: number | null | undefined) => (t ? new Date(t * 1000).toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' }) : '–')
const errMsg = (e: unknown) => (e instanceof ApiError ? e.message : 'Something went wrong')

function Editor({ a, onDone }: { a: Acct; onDone: () => void }) {
  const qc = useQueryClient()
  const toast = useToast()
  const [allowed, setAllowed] = useState(a.real_allowed)
  const lots = String(a.max_lots)                                            // kept for the API; the person picks lots per instrument
  const [loss, setLoss] = useState(a.max_daily_loss == null ? '' : String(a.max_daily_loss))
  const [syms, setSyms] = useState<string[]>(a.allowed_symbols)
  const save = useMutation({
    mutationFn: () => put(`/api/admin/users/${a.user_id}/broker/real`, { allowed, max_lots: Number(lots), max_daily_loss: loss.trim() === '' ? null : Number(loss), allowed_symbols: syms }),
    onSuccess: () => { toast(allowed ? 'Real access saved' : 'Real access withdrawn, account is on paper'); void qc.invalidateQueries({ queryKey: ['admin', 'broker'] }); onDone() },
    onError: (e) => toast(errMsg(e)),
  })
  const bad = !allowed ? false : !(Number(lots) >= 1 && Number(lots) <= 50) || syms.length === 0 || (loss.trim() !== '' && !(Number(loss) > 0))
  return (
    <form className="stack" style={{ padding: 12, background: 'var(--bg-hover)', borderRadius: 10, gap: 10 }} onSubmit={(e) => { e.preventDefault(); if (!bad) save.mutate() }}>
      <label className="inline"><input type="checkbox" checked={allowed} onChange={(e) => setAllowed(e.target.checked)} /> <b>Allow real orders</b> for {a.name ?? a.username}</label>
      <div className="row" style={{ gap: 14, flexWrap: 'wrap', opacity: allowed ? 1 : 0.5 }}>
        <label className="field" style={{ minWidth: 170 }}>Daily loss limit (₹)<input type="number" min={1} value={loss} placeholder="no cap" onChange={(e) => setLoss(e.target.value)} disabled={!allowed} /></label>
        <div className="field">Symbols<div className="row" style={{ gap: 12, minHeight: 34, alignItems: 'center' }}>
          {SYMS.map((s) => (<label key={s} className="inline"><input type="checkbox" disabled={!allowed} checked={syms.includes(s)} onChange={(e) => setSyms(e.target.checked ? [...syms, s] : syms.filter((x) => x !== s))} /> {s}</label>))}</div></div>
      </div>
      <p className="note">Even when allowed, this person must connect and verify Angel One, and confirm with their own password, before real mode turns on. Withdrawing puts the account on paper at once.</p>
      <div className="row" style={{ gap: 8 }}><button className="btn primary" disabled={bad || save.isPending}>{save.isPending ? 'Saving…' : 'Save'}</button><button type="button" className="btn" onClick={onDone}>Cancel</button></div>
    </form>
  )
}

/** Admin: every connected Angel One account at a glance (status, PAPER or REAL, limits), the platform's master switch, and the per-account real-order permission. No secret is ever shown. */
export default function BrokerAdminPage() {
  const qc = useQueryClient()
  const toast = useToast()
  const q = useQuery({ queryKey: ['admin', 'broker'], queryFn: () => get<Overview>('/api/admin/broker/overview'), refetchInterval: 30_000 })
  const [edit, setEdit] = useState<string | null>(null)
  const tb = useQuery({ queryKey: ['admin', 'broker-tb'], queryFn: () => get<{ instruments: TbRow[]; orders: TbOrder[] }>('/api/admin/broker/tb?limit=100'), refetchInterval: 15_000 })
  const ord = useQuery({ queryKey: ['admin', 'broker-orders'], queryFn: () => get<{ orders: AdminOrder[] }>('/api/admin/broker/orders?limit=100'), refetchInterval: 20_000 })
  const refresh = () => { void qc.invalidateQueries({ queryKey: ['admin', 'broker'] }) }
  const master = useMutation({
    mutationFn: (enabled: boolean) => post<{ accounts_back_to_paper: number }>('/api/admin/broker/master', { enabled }),
    onSuccess: (r, enabled) => { toast(enabled ? 'Real orders are ON for the platform' : `Kill switch pulled: ${r.accounts_back_to_paper} account(s) back to paper`); refresh() },
    onError: (e) => toast(errMsg(e)),
  })
  const remove = useMutation({ mutationFn: (id: string) => del(`/api/admin/users/${id}/broker`), onSuccess: () => { toast('Broker details removed'); refresh() }, onError: (e) => toast(errMsg(e)) })
  const d = q.data
  return (
    <div className="page">
      <h1>Broker &amp; Real</h1>
      <p className="sub">Angel One accounts that people connected themselves. You see status and limits only, never a password, PIN, key or TOTP secret.</p>
      {q.isLoading && <div className="skeleton" style={{ height: 120 }} />}
      {q.error && <p className="err">{errMsg(q.error)}</p>}
      {d && (
        <>
          <div className="card" style={{ borderColor: d.master_on ? 'var(--danger)' : undefined }}>
            <div className="card-head"><h2>Master switch</h2><span className="spacer" />
              <span className={'chip ' + (d.master_on ? 'bad' : 'ok')}>{d.master_on ? 'REAL ORDERS ON' : 'OFF · everyone is on paper'}</span></div>
            <p className="sub">{d.master_on
              ? 'Accounts you have allowed can switch to real mode. Pulling the switch puts every account back to paper at once.'
              : 'Nobody can use real mode while this is off, whatever else is set. Only the owner can switch it on; any admin can switch it off.'}</p>
            <div className="row" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
              {d.master_on ? (
                <button className="btn danger" disabled={master.isPending} onClick={() => { if (confirm('Switch real orders OFF for everyone? Every account goes back to paper now.')) master.mutate(false) }}>Kill switch: all to paper</button>
              ) : (
                <button className="btn primary" disabled={!d.can_turn_on || master.isPending} title={d.can_turn_on ? '' : 'Only the owner can do this'}
                  onClick={() => { if (confirm('Allow real orders on the platform? Only accounts you have separately allowed will be able to use them.')) master.mutate(true) }}>Switch real orders ON</button>
              )}
              {!d.master_on && !d.can_turn_on && <span className="sub">Only the owner can switch it on.</span>}
            </div>
          </div>

          <div className="row" style={{ gap: 8, flexWrap: 'wrap', margin: '4px 0 10px' }}>
            <span className="chip muted">{d.counts.total} connected accounts</span>
            <span className="chip ok">{d.counts.connected} login verified</span>
            <span className="chip warn">{d.counts.allowed} allowed for real</span>
            <span className={'chip ' + (d.counts.real_now ? 'bad' : 'muted')}>{d.counts.real_now} on REAL now</span>
          </div>

          <div className="card">
            <div className="card-head"><h2>Accounts</h2></div>
            {d.accounts.length === 0 ? <p className="empty">Nobody has connected an Angel One account yet. People do it themselves under Settings → Angel One account.</p> : (
              <div style={{ overflowX: 'auto' }}>
                <table className="tbl">
                  <thead><tr><th>User</th><th>Angel One</th><th>Login</th><th>Mode now</th><th>Real access</th><th>Limits</th><th /></tr></thead>
                  <tbody>
                    {d.accounts.map((a) => (
                      <Fragment key={a.user_id}>
                        <tr>
                          <td><b>{a.name ?? a.username ?? a.user_id}</b><div className="sub">{a.username}</div></td>
                          <td>{a.client_code}</td>
                          <td><span className={'chip ' + (a.status === 'connected' ? 'ok' : a.status === 'error' ? 'bad' : 'warn')} title={a.status_message ?? ''}>{STATUS[a.status] ?? a.status}</span>
                            <div className="sub">{when(a.last_checked_at)}</div></td>
                          <td><span className={'chip ' + (a.effective_mode === 'real' ? 'bad' : 'muted')}>{a.effective_mode === 'real' ? 'REAL' : 'PAPER'}</span>
                            {a.mode === 'real' && a.effective_mode !== 'real' && <div className="sub" title={a.real_blockers.join(' ')}>chose real, blocked</div>}
                            {a.mode_changed_at ? <div className="sub">{when(a.mode_changed_at)}</div> : null}</td>
                          <td>{a.real_allowed ? <span className="chip warn">Allowed</span> : <span className="chip muted">Not allowed</span>}</td>
                          <td className="sub">{a.real_allowed ? <>lots: the person chooses · {a.max_daily_loss == null ? 'no loss cap' : `₹${a.max_daily_loss.toLocaleString('en-IN')} / day`}<br />{a.allowed_symbols.join(', ')}</> : '–'}</td>
                          <td style={{ whiteSpace: 'nowrap' }}>
                            <button className="btn sm" onClick={() => setEdit(edit === a.user_id ? null : a.user_id)}>Real access…</button>{' '}
                            <button className="btn sm danger" onClick={() => { if (confirm(`Remove ${a.name ?? a.username}'s Angel One details? They must enter them again.`)) remove.mutate(a.user_id) }}>Remove</button>
                          </td>
                        </tr>
                        {edit === a.user_id && <tr><td colSpan={7}><Editor a={a} onDone={() => setEdit(null)} /></td></tr>}
                      </Fragment>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
          <div className="card" style={{ borderColor: tb.data?.instruments.some((x) => x.halted) ? 'var(--danger)' : undefined }}>
            <div className="card-head"><h2>Top-Bottom real orders</h2><span className="spacer" /><span className="sub">what the executor holds right now</span></div>
            {!tb.data || tb.data.instruments.length === 0 ? <p className="empty">No instrument has real orders switched on yet.</p> : (
              <div style={{ overflowX: 'auto' }}>
                <table className="tbl"><thead><tr><th>User</th><th>Instrument</th><th>Real position</th><th>State</th></tr></thead>
                  <tbody>{tb.data.instruments.map((x) => (
                    <tr key={x.user_id + x.symbol}><td>{x.name ?? x.username}</td><td>{x.symbol}</td>
                      <td>{x.position ? <><b>{x.position.side} {x.position.qty}</b> {x.position.contract}<div className="sub">at {x.position.entry_price ?? '–'}{x.stop_target != null ? ` · stop ${x.stop_target}` : ''}</div></> : x.pending_entry ? 'entry being placed' : '–'}</td>
                      <td>{x.halted ? <span className="chip bad" title={x.halted}>HALTED</span> : x.released ? <span className="chip warn">switch off, position open</span> : <span className="chip ok">running</span>}{x.halted && <div className="sub err">{x.halted}</div>}</td></tr>
                  ))}</tbody></table>
              </div>
            )}
            {tb.data && tb.data.orders.length > 0 && (
              <details style={{ marginTop: 8 }}><summary>{tb.data.orders.length} recent Top-Bottom orders</summary>
                <div style={{ overflowX: 'auto' }}><table className="tbl"><thead><tr><th>When</th><th>User</th><th>Order</th><th>Price</th><th>Status</th></tr></thead>
                  <tbody>{tb.data.orders.map((o) => (
                    <tr key={o.id}><td>{when(o.created_at)}</td><td>{o.name ?? o.username}</td><td>{o.role} <b>{o.side}</b> {o.qty} {o.contract}<div className="sub">{o.order_type}</div></td>
                      <td>{o.price != null ? `₹${o.price}` : '–'}{o.trigger_price != null ? ` / trg ${o.trigger_price}` : ''}</td>
                      <td><span className={'chip ' + (o.status === 'filled' ? 'ok' : o.status === 'rejected' || o.status === 'unknown' ? 'bad' : 'muted')} title={o.message ?? ''}>{o.status}</span></td></tr>
                  ))}</tbody></table></div></details>
            )}
          </div>

          <div className="card">
            <div className="card-head"><h2>Real orders</h2><span className="spacer" /><span className="sub">newest first · every order is also in the Audit log</span></div>
            {!ord.data || ord.data.orders.length === 0 ? <p className="empty">No real order has been prepared yet.</p> : (
              <div style={{ overflowX: 'auto' }}>
                <table className="tbl">
                  <thead><tr><th>When</th><th>User</th><th>Order</th><th>Price</th><th>Status</th><th>Angel One id</th></tr></thead>
                  <tbody>{ord.data.orders.map((o) => (
                    <tr key={o.id}>
                      <td>{when(o.created_at)}</td>
                      <td>{o.name ?? o.username}</td>
                      <td><b>{o.side}</b> {o.lots} × {o.symbol} {o.strike} {o.opt}<div className="sub">{o.expiry} · {o.intent}</div></td>
                      <td>₹{o.price.toLocaleString('en-IN')}</td>
                      <td><span className={'chip ' + (o.status === 'placed' ? 'ok' : o.status === 'rejected' || o.status === 'unknown' ? 'bad' : 'muted')} title={o.message ?? ''}>{o.status}</span>
                        {o.status === 'unknown' && <div className="sub err">outcome not known: the user must check Angel One</div>}</td>
                      <td className="sub">{o.broker_order_id ?? '–'}</td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            )}
          </div>
          <p className="note">"REAL" means the account is armed and within its limits. Orders go through a preview with every safety check, and each can be sent only once. Every change here is written to the Audit log.</p>
        </>
      )}
    </div>
  )
}
