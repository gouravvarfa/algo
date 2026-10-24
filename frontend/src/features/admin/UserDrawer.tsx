import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState, type FormEvent } from 'react'
import { ApiError, api, del, get, post, put } from '../../lib/api'
import { dt } from '../../lib/format'
import { useToast } from '../../lib/toast'
import './admin.css'

interface Limits { max_lots: number | null; allowed_symbols: string[] | null }
interface Note { id: number; author_name: string; note: string; created_at: number }
const SYMBOLS = ['NIFTY', 'BANKNIFTY']

/** One account's limits (enforced on the server when the simulator saves positions) and the admins' private notes about it. */
export function UserDrawer({ user, onClose, canManage, isSelf = false }: { user: { id: string; name: string; username: string }; onClose: () => void; canManage: boolean; isSelf?: boolean }) {
  const canAssign = canManage || isSelf
  const qc = useQueryClient()
  const toast = useToast()
  const lim = useQuery({ queryKey: ['admin', 'limits', user.id], queryFn: () => get<Limits>(`/api/admin/users/${user.id}/limits`) })
  const notes = useQuery({ queryKey: ['admin', 'notes', user.id], queryFn: () => get<{ notes: Note[] }>(`/api/admin/users/${user.id}/notes`) })
  const strat = useQuery({ queryKey: ['admin', 'strategies', user.id], queryFn: () => get<{ strategies: { id: string; symbol: string; trading_symbol: string; lots: number | null; expiry: string | null; fault: string | null }[] }>(`/api/admin/users/${user.id}/strategies`) })
  const fo = useQuery({ queryKey: ['admin', 'fo-stocks'], queryFn: () => get<{ symbols: string[] }>('/api/admin/fo-stocks'), staleTime: 3_600_000 })
  const [sym, setSym] = useState('')
  const [lots, setLots] = useState('1')
  const assign = useMutation({
    mutationFn: () => post(`/api/admin/users/${user.id}/strategies`, { symbol: sym.trim().toUpperCase(), lots: Number(lots) || 1 }),
    onSuccess: () => { toast('Strategy assigned'); setSym(''); void qc.invalidateQueries({ queryKey: ['admin'] }) },
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Could not assign'),
  })
  const unassign = useMutation({
    mutationFn: (id: string) => del(`/api/admin/users/${user.id}/instruments/${id}`),
    onSuccess: () => { toast('Strategy removed'); void qc.invalidateQueries({ queryKey: ['admin'] }) },
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Could not remove'),
  })
  const [maxLots, setMaxLots] = useState('')
  const [syms, setSyms] = useState<string[]>(SYMBOLS)
  const [note, setNote] = useState('')
  useEffect(() => { if (lim.data) { setMaxLots(lim.data.max_lots == null ? '' : String(lim.data.max_lots)); setSyms(lim.data.allowed_symbols ?? SYMBOLS) } }, [lim.data])

  const save = useMutation({
    mutationFn: () => put<Limits>(`/api/admin/users/${user.id}/limits`, { max_lots: maxLots.trim() === '' ? null : Number(maxLots), allowed_symbols: syms.length === SYMBOLS.length ? null : syms }),
    onSuccess: () => { toast('Limits saved'); void qc.invalidateQueries({ queryKey: ['admin', 'limits', user.id] }) },
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Could not save'),
  })
  const add = useMutation({
    mutationFn: () => post(`/api/admin/users/${user.id}/notes`, { note }),
    onSuccess: () => { setNote(''); void qc.invalidateQueries({ queryKey: ['admin', 'notes', user.id] }) },
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Could not add the note'),
  })
  const [prof, setProf] = useState({ name: user.name, username: user.username, email: '' })
  const profile = useMutation({
    mutationFn: () => api('/api/admin/users/' + user.id + '/profile', { method: 'PATCH', json: prof }),
    onSuccess: () => { toast('Profile saved'); void qc.invalidateQueries({ queryKey: ['admin'] }) },
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Could not save'),
  })
  const clearPos = useMutation({
    mutationFn: async () => { for (const sym of SYMBOLS) await put(`/api/admin/users/${user.id}/sim-positions?symbol=${sym}`, { positions: [] }) },
    onSuccess: () => toast('Simulator positions cleared'),
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Could not clear'),
  })
  const toggle = (s: string) => setSyms((cur) => (cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s]))

  return (
    <div className="drawer-wrap" onClick={onClose}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={`Details for ${user.name}`}>
        <div className="card-head"><h2>{user.name} <span className="sub mono">{user.username}</span></h2><span className="spacer" /><button className="btn sm" onClick={onClose}>Close</button></div>

        <h3>Strategies <span className="sub">(Top-Bottom on stock futures, assigned by you)</span></h3>
        <ul className="notes">
          {(strat.data?.strategies ?? []).map((x) => (
            <li key={x.id}><span><b>{x.symbol}</b> <span className="sub">{x.trading_symbol} · {x.lots ?? 1} lot · exp {x.expiry}</span>{x.fault ? <span className="chip bad" title={x.fault}> FAULT</span> : null}</span>
              {canAssign && x.symbol !== 'BANKNIFTY' && <button className="btn sm danger" onClick={() => { if (confirm(`Remove ${x.symbol} from ${user.name}? Its trades stay in their history.`)) unassign.mutate(x.id) }}>Remove</button>}</li>
          ))}
          {!strat.data?.strategies.length && <li className="sub">No strategy yet. BankNifty is created when they first open Top Bottom.</li>}
        </ul>
        {canAssign && (
          <form className="linkrow" onSubmit={(e: FormEvent) => { e.preventDefault(); if (sym.trim()) assign.mutate() }}>
            <input list="fo-stocks" value={sym} onChange={(e) => setSym(e.target.value.toUpperCase())} placeholder="Type to pick an F&O stock, e.g. RELIANCE" maxLength={20} />
            <datalist id="fo-stocks">{(fo.data?.symbols ?? []).map((x) => <option key={x} value={x} />)}</datalist>
            <input type="number" min={1} max={50} value={lots} onChange={(e) => setLots(e.target.value)} style={{ width: 72 }} aria-label="Lots" title="Lots" />
            <button className="btn primary" type="submit" disabled={assign.isPending || !(fo.data?.symbols ?? []).includes(sym.trim().toUpperCase())}>{assign.isPending ? 'Adding…' : 'Assign'}</button>
          </form>
        )}

        <h3>Profile</h3>
        <form className="stack" onSubmit={(e: FormEvent) => { e.preventDefault(); profile.mutate() }}>
          <label className="field">Full name<input value={prof.name} onChange={(e) => setProf({ ...prof, name: e.target.value })} disabled={!canManage} /></label>
          <label className="field">Username<input value={prof.username} onChange={(e) => setProf({ ...prof, username: e.target.value.toLowerCase() })} disabled={!canManage} /></label>
          <label className="field">Email (optional)<input type="email" value={prof.email} onChange={(e) => setProf({ ...prof, email: e.target.value })} disabled={!canManage} placeholder="unchanged" /></label>
          {canManage && <div className="btnrow" style={{ justifyContent: 'flex-start' }}><button className="btn primary" type="submit" disabled={profile.isPending}>Save profile</button>
            <button className="btn danger" type="button" onClick={() => { if (confirm(`Clear every open Options Simulator position of ${user.name}? The person is told under Account activity.`)) clearPos.mutate() }}>Clear simulator positions</button></div>}
        </form>

        <h3>Limits</h3>
        <p className="sub">Checked by the server every time the Options Simulator saves positions. Paper trading only.</p>
        <form className="stack" onSubmit={(e: FormEvent) => { e.preventDefault(); save.mutate() }}>
          <label className="field">Max lots held in total<input type="number" min={0} value={maxLots} onChange={(e) => setMaxLots(e.target.value)} placeholder="no limit" disabled={!canManage} /></label>
          <div className="field">Allowed indexes
            <div className="btnrow" style={{ justifyContent: 'flex-start' }}>
              {SYMBOLS.map((s) => <label key={s} className="inline"><input type="checkbox" checked={syms.includes(s)} disabled={!canManage} onChange={() => toggle(s)} /> {s}</label>)}
            </div>
          </div>
          {canManage && <button className="btn primary" type="submit" disabled={save.isPending}>Save limits</button>}
        </form>

        <h3>Notes <span className="sub">(only admins see these)</span></h3>
        {canManage && (
          <form className="linkrow" onSubmit={(e: FormEvent) => { e.preventDefault(); if (note.trim()) add.mutate() }}>
            <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="e.g. asked for a higher limit on 24 Oct" maxLength={1000} />
            <button className="btn" type="submit" disabled={add.isPending || !note.trim()}>Add</button>
          </form>
        )}
        <ul className="notes">
          {(notes.data?.notes ?? []).map((n) => <li key={n.id}><span>{n.note}</span><small>{n.author_name} · {dt(new Date(n.created_at * 1000).toISOString())}</small></li>)}
          {!notes.data?.notes.length && <li className="sub">No notes yet.</li>}
        </ul>
      </aside>
    </div>
  )
}
