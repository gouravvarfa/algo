import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { ApiError, del, get, post, put } from '../../lib/api'
import { useToast } from '../../lib/toast'
import './admin.css'

interface Entry { date: string; exchange: string; kind: string; start_time: string | null; end_time: string | null; note: string | null; created_by: string | null }
interface Data { entries: Entry[]; today: { text: string }[]; sync?: { at: number | null; added: number; error: string | null } }

const day = (d: string) => new Date(d + 'T00:00:00').toLocaleDateString('en-IN', { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' })

/** Exchange holidays and special sessions. A holiday closes the market for the strategy and real orders; a special session (Muhurat, Budget
 * Sunday) keeps them off too. Either way everybody gets a banner on the day and up to three days before ("Kal market band rahega"). */
export default function CalendarPage() {
  const qc = useQueryClient()
  const toast = useToast()
  const q = useQuery({ queryKey: ['calendar'], queryFn: () => get<Data>('/api/calendar') })
  const [f, setF] = useState({ date: '', exchange: 'NSE', kind: 'holiday', start: '', end: '', note: '' })
  const done = (d: Data) => { qc.setQueryData(['calendar'], d); void qc.invalidateQueries({ queryKey: ['announcement'] }) }
  const save = useMutation({
    mutationFn: () => put<Data>('/api/admin/calendar', { ...f, start: f.kind === 'special' ? f.start : null, end: f.kind === 'special' ? f.end : null }),
    onSuccess: (d) => { toast('Saved. The banner and the market hours follow it.'); setF({ ...f, date: '', note: '' }); done(d) },
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Could not save'),
  })
  const remove = useMutation({
    mutationFn: (e: Entry) => del<Data>(`/api/admin/calendar/${e.date}/${e.exchange}`),
    onSuccess: (d) => { toast('Removed'); done(d) },
  })
  const sync = useMutation({
    mutationFn: () => post<Data>('/api/admin/calendar/sync'),
    onSuccess: (d) => { toast(d.sync?.error ? `NSE did not answer: ${d.sync.error}` : 'NSE holiday list is up to date'); done(d) },
  })
  const ok = !!f.date && (f.kind === 'holiday' || (!!f.start && !!f.end && f.start < f.end))
  const rows = q.data?.entries ?? []
  return (
    <div className="page">
      <h1>Market calendar</h1>
      <p className="sub">Holidays and special sessions. On these days Top-Bottom does not trade (paper or real), and everybody sees a notice on the day and up to 3 days before.</p>
      <div className="card" style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <div style={{ flex: 1, minWidth: 220 }}><b>NSE holidays are added automatically</b>
          <div className="sub">From NSE's official F&amp;O holiday list, at every start and every 12 hours. Days you add by hand are never overwritten.
            {q.data?.sync?.at ? ` Last check ${new Date(q.data.sync.at * 1000).toLocaleString('en-IN')}${q.data.sync.error ? ` failed: ${q.data.sync.error}` : ''}.` : ''}</div></div>
        <button className="btn" disabled={sync.isPending} onClick={() => sync.mutate()}>{sync.isPending ? 'Checking NSE…' : 'Check NSE now'}</button>
      </div>
      {(q.data?.today.length ?? 0) > 0 && (
        <div className="card"><div className="card-head"><h2>Banner showing now</h2></div>{q.data!.today.map((t) => <div key={t.text} className="banner info"><span>📅 {t.text}</span></div>)}</div>
      )}
      <form className="card" onSubmit={(e) => { e.preventDefault(); if (ok) save.mutate() }}>
        <div className="card-head"><h2>Add a day</h2></div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: 12 }}>
          <label className="field">Date<input type="date" value={f.date} onChange={(e) => setF({ ...f, date: e.target.value })} required /></label>
          <label className="field">Exchange<select value={f.exchange} onChange={(e) => setF({ ...f, exchange: e.target.value })}><option value="NSE">NSE (index & stock futures)</option><option value="MCX">MCX (gold / silver)</option></select></label>
          <label className="field">Type<select value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}><option value="holiday">Holiday (closed)</option><option value="special">Special session (Muhurat…)</option></select></label>
          {f.kind === 'special' && <>
            <label className="field">Starts<input type="time" value={f.start} onChange={(e) => setF({ ...f, start: e.target.value })} /></label>
            <label className="field">Ends<input type="time" value={f.end} onChange={(e) => setF({ ...f, end: e.target.value })} /></label>
          </>}
          <label className="field" style={{ gridColumn: '1 / -1' }}>Name<input value={f.note} maxLength={200} onChange={(e) => setF({ ...f, note: e.target.value })} placeholder="e.g. Diwali Balipratipada / Muhurat Trading" /></label>
        </div>
        <div style={{ marginTop: 10 }}><button className="btn primary" disabled={!ok || save.isPending}>{save.isPending ? 'Saving…' : 'Save'}</button></div>
      </form>
      <div className="card">
        <div className="card-head"><h2>Upcoming</h2><span className="spacer" /><span className="sub">from a week ago</span></div>
        {rows.length === 0 ? <p className="empty">Nothing yet. The NSE list arrives automatically within a minute of the server starting.</p> : (
          <div style={{ overflowX: 'auto' }}>
            <table className="tbl"><thead><tr><th>Day</th><th>Exchange</th><th>Type</th><th>Name</th><th /></tr></thead>
              <tbody>{rows.map((e) => (
                <tr key={e.date + e.exchange}>
                  <td>{day(e.date)}</td><td>{e.exchange}</td>
                  <td>{e.kind === 'holiday' ? <span className="chip warn">closed</span> : <span className="chip">special {e.start_time}–{e.end_time}</span>}</td>
                  <td>{e.note || '–'}<div className="sub">{e.created_by === 'NSE (auto)' ? 'from NSE, automatic' : `by ${e.created_by ?? '–'}`}</div></td>
                  <td><button className="btn sm" onClick={() => { if (window.confirm(`Remove ${e.date} ${e.exchange}?`)) remove.mutate(e) }}>Remove</button></td>
                </tr>
              ))}</tbody></table>
          </div>
        )}
      </div>
    </div>
  )
}
