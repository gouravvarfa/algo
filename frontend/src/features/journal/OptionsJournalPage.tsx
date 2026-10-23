import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { ApiError, get, put } from '../../lib/api'
import { money, num, sign } from '../../lib/format'
import { useToast } from '../../lib/toast'
import './journal.css'

interface Trade {
  id: number; symbol: string; side: string; opt: string; strike: number; expiry: string | null; lots: number; lot_size: number
  entry: number; entry_date: string | null; entry_time: string | null; exit: number | null; exit_date: string | null; exit_time: string | null; exit_reason: string | null
  status: 'OPEN' | 'CLOSED' | 'REMOVED'; realized: number | null; note: string | null; tags: string[]; rating: number | null
}
interface Stats {
  trades: number; open: number; closed: number; removed: number; net: number; win_rate: number | null; avg_win: number | null; avg_loss: number | null
  profit_factor: number | null; best: number | null; worst: number | null; curve: { date: string; day: number; cum: number }[]
}

function Curve({ points }: { points: Stats['curve'] }) {
  if (points.length < 2) return <p className="empty">The equity curve appears after trades have been closed on two different days.</p>
  const w = 760, h = 150, pad = 6
  const ys = points.map((p) => p.cum), lo = Math.min(0, ...ys), hi = Math.max(0, ...ys), span = hi - lo || 1
  const x = (i: number) => pad + (i / (points.length - 1)) * (w - 2 * pad)
  const y = (v: number) => h - pad - ((v - lo) / span) * (h - 2 * pad)
  const d = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.cum).toFixed(1)}`).join(' ')
  const up = ys[ys.length - 1] >= 0
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="curve" role="img" aria-label="Cumulative realised P&L by exit day" preserveAspectRatio="none">
      <line x1={pad} x2={w - pad} y1={y(0)} y2={y(0)} className="zero" />
      <path d={`${d} L${x(points.length - 1)},${y(0)} L${x(0)},${y(0)} Z`} className={up ? 'area up' : 'area down'} />
      <path d={d} className={up ? 'line up' : 'line down'} />
    </svg>
  )
}

/** The options journal: every leg ever put on in the Options Simulator (kept even after the screen is cleared), with statistics and the person's own notes.
 * With `userId` an admin sees another person's journal, read-only. */
export default function OptionsJournalPage({ userId }: { userId?: string }) {
  const qc = useQueryClient()
  const toast = useToast()
  const readOnly = !!userId
  const url = userId ? `/api/admin/users/${userId}/option-journal` : '/api/me/option-journal'
  const q = useQuery({ queryKey: ['optj', userId ?? 'me'], queryFn: () => get<{ trades: Trade[]; stats: Stats }>(url), refetchInterval: 20_000 })
  const [status, setStatus] = useState('')
  const [index, setIndex] = useState('')
  const [edit, setEdit] = useState<number | null>(null)
  const [draft, setDraft] = useState({ note: '', tags: '', rating: 0 })
  const save = useMutation({
    mutationFn: (id: number) => put(`/api/me/option-journal/${id}`, { note: draft.note, tags: draft.tags.split(',').map((t) => t.trim()).filter(Boolean), rating: draft.rating || null }),
    onSuccess: () => { toast('Saved'); setEdit(null); void qc.invalidateQueries({ queryKey: ['optj'] }) },
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Could not save'),
  })
  const rows = useMemo(() => (q.data?.trades ?? []).filter((t) => (!status || t.status === status) && (!index || t.symbol === index)), [q.data, status, index])
  const s = q.data?.stats
  const open = (t: Trade) => { setEdit(t.id); setDraft({ note: t.note ?? '', tags: t.tags.join(', '), rating: t.rating ?? 0 }) }
  const kpi: [string, string, string][] = s ? [
    ['Net realised P&L', money(s.net), sign(s.net)], ['Closed trades', String(s.closed), ''], ['Win rate', s.win_rate == null ? '–' : `${s.win_rate}%`, ''],
    ['Avg win / avg loss', `${s.avg_win == null ? '–' : money(s.avg_win)} / ${s.avg_loss == null ? '–' : money(s.avg_loss)}`, ''], ['Profit factor', s.profit_factor == null ? '–' : String(s.profit_factor), ''],
    ['Best / worst', `${s.best == null ? '–' : money(s.best)} / ${s.worst == null ? '–' : money(s.worst)}`, ''], ['Open now', String(s.open), ''],
  ] : []

  return (
    <div className="page">
      <div className="row" style={{ alignItems: 'center' }}>
        <h1>Options journal</h1>{readOnly && <span className="chip warn">read-only</span>}
        <span className="spacer" />
        {!readOnly && <a className="btn sm" href="/api/me/option-journal.csv" download>Export CSV</a>}
      </div>
      <p className="sub">Every simulated leg is recorded here for good, even after you clear the simulator. Open a row to add your reasoning, tags and a rating. Paper trading only.</p>
      {s && <div className="kpis">{kpi.map(([k, v, t]) => <div key={k} className="kpi"><span className="k">{k}</span><b className={`num ${t}`}>{v}</b></div>)}</div>}
      {s && <div className="card"><div className="card-head"><h2>Cumulative realised P&amp;L</h2><span className="chip muted">by exit day</span></div><Curve points={s.curve} /></div>}

      <div className="card">
        <div className="card-head">
          <h2>Trades</h2><span className="chip muted">{rows.length}</span><span className="spacer" />
          <select value={index} onChange={(e) => setIndex(e.target.value)}><option value="">all indexes</option><option>NIFTY</option><option>BANKNIFTY</option></select>
          <select value={status} onChange={(e) => setStatus(e.target.value)}><option value="">all</option><option value="OPEN">open</option><option value="CLOSED">closed</option><option value="REMOVED">removed</option></select>
        </div>
        <div className="scroll"><table>
          <thead><tr><th className="l">Entered</th><th className="l">Leg</th><th>Lots</th><th>Entry</th><th className="l">Exited</th><th>Exit</th><th>P&amp;L</th><th className="l">Status</th><th className="l">Journal</th></tr></thead>
          <tbody>
            {rows.flatMap((t) => [
              <tr key={t.id}>
                <td className="l mono">{t.entry_date}{t.entry_time ? ` ${t.entry_time}` : ''}</td>
                <td className="l"><span className={`badge ${t.side}`}>{t.side}</span> <b>{t.symbol}</b> {num(t.strike)} {t.opt} <span className="sub">{t.expiry}</span></td>
                <td className="num">{t.lots}×{t.lot_size}</td><td className="num">{num(t.entry)}</td>
                <td className="l mono">{t.exit_date ? `${t.exit_date}${t.exit_time ? ` ${t.exit_time}` : ''}` : '–'}</td><td className="num">{t.exit == null ? '–' : num(t.exit)}</td>
                <td className={`num ${sign(t.realized)}`}>{t.realized == null ? '–' : money(t.realized)}</td>
                <td className="l"><span className={`chip ${t.status === 'CLOSED' ? 'ok' : t.status === 'OPEN' ? 'warn' : 'muted'}`}>{t.status.toLowerCase()}</span>{t.exit_reason ? <span className="sub"> {t.exit_reason}</span> : null}</td>
                <td className="l wrap">
                  {t.rating ? <span className="stars">{'★'.repeat(t.rating)}<i>{'★'.repeat(5 - t.rating)}</i></span> : null} {t.tags.map((g) => <span key={g} className="chip muted">{g}</span>)}
                  {t.note ? <div className="sub">{t.note}</div> : null}
                  {!readOnly && <button className="btn sm" onClick={() => (edit === t.id ? setEdit(null) : open(t))}>{edit === t.id ? 'Close' : t.note || t.rating ? 'Edit' : 'Add note'}</button>}
                </td>
              </tr>,
              edit === t.id && (
                <tr key={`${t.id}-e`}><td colSpan={9} className="l">
                  <div className="editor">
                    <label className="field">Why did you take this trade, and what did you learn?<textarea rows={3} maxLength={2000} value={draft.note} onChange={(e) => setDraft({ ...draft, note: e.target.value })} /></label>
                    <label className="field">Tags (comma separated)<input value={draft.tags} onChange={(e) => setDraft({ ...draft, tags: e.target.value })} placeholder="breakout, expiry day, hedge" /></label>
                    <div className="field">Rating of the decision<span className="rate">{[1, 2, 3, 4, 5].map((n) => <button key={n} type="button" className={n <= draft.rating ? 'on' : ''} onClick={() => setDraft({ ...draft, rating: n === draft.rating ? 0 : n })} aria-label={`${n} star`}>★</button>)}</span></div>
                    <div><button className="btn primary" onClick={() => save.mutate(t.id)} disabled={save.isPending}>Save</button></div>
                  </div>
                </td></tr>
              ),
            ])}
            {!rows.length && <tr><td colSpan={9} className="empty">{q.isLoading ? 'Loading…' : 'No trades yet. Put legs on in the Options Simulator and they appear here.'}</td></tr>}
          </tbody>
        </table></div>
      </div>
    </div>
  )
}
