import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ApiError, get, post, put } from '../../lib/api'
import { useDataSource } from '../../lib/datasource'
import { useToast } from '../../lib/toast'

interface Item { instrument_id: string; symbol: string; real_on: boolean; real_effective: boolean }
interface TbState {
  instrument_id: string; position: { side: string; qty: number; contract: string; entry_price: number | null } | null; pending_entry: boolean; halted: string | null; stop_target: number | null
  released: boolean; exiting?: boolean; missed: { at: string; reason: string }[]; events: { at: string; text: string }[]; orders: { id: string; role: string; side: string; qty: number; type: string; price: number | null; trigger: number | null; state: string }[]
}
interface Data { account_mode: string; real_allowed: boolean; blockers: string[]; instruments: Item[] }

/** The person's own Real/Paper switch for ONE Top-Bottom instrument. Shown only to accounts an admin allowed for real orders, never in an admin's "view as" screen.
 * Off by default; it can be turned on only while the whole account is in REAL mode, and it turns itself off whenever the account leaves REAL. */
export function RealSwitch({ instrumentId, symbol }: { instrumentId: string; symbol: string }) {
  const ds = useDataSource()
  const qc = useQueryClient()
  const toast = useToast()
  const q = useQuery({ queryKey: ['broker-instruments'], queryFn: () => get<Data>('/api/me/broker/instruments'), enabled: !ds.readOnly, refetchInterval: 30_000, retry: false })
  const tb = useQuery({ queryKey: ['broker-tb'], queryFn: () => get<{ instruments: TbState[] }>('/api/me/broker/tb'), enabled: !ds.readOnly, refetchInterval: 10_000, retry: false })
  const clear = useMutation({
    mutationFn: () => post(`/api/me/broker/tb/${instrumentId}/clear-halt`),
    onSuccess: () => { toast('Halt cleared. Orders can go out again.'); void qc.invalidateQueries({ queryKey: ['broker-tb'] }) },
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Could not clear the halt'),
  })
  const exit = useMutation({
    mutationFn: () => post<{ result: string; message: string }>(`/api/me/broker/tb/${instrumentId}/exit`),
    onSuccess: (r) => { toast(r.message); for (const k of ['broker-tb', 'broker-instruments', 'broker-tb-trades']) void qc.invalidateQueries({ queryKey: [k] }) },
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Could not exit'),
  })
  const set = useMutation({
    mutationFn: (real: boolean) => put<Data>(`/api/me/broker/instruments/${instrumentId}`, { real }),
    onSuccess: (d, real) => { qc.setQueryData(['broker-instruments'], d); void qc.invalidateQueries({ queryKey: ['broker-tb-trades'] }); void qc.invalidateQueries({ queryKey: ['broker-tb'] }); toast(real ? `Real orders armed for ${symbol}` : `${symbol} is on paper`) },
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Could not change the switch'),
  })
  const d = q.data
  const it = d?.instruments.find((i) => i.instrument_id === instrumentId)
  const mg = useQuery({ queryKey: ['broker-margin', instrumentId], queryFn: () => get<Margin>(`/api/me/broker/tb/${instrumentId}/margin`), enabled: !ds.readOnly && !!d?.real_allowed, refetchInterval: 120_000, retry: false })
  if (ds.readOnly || !d || !it) return null
  const marginLine = mg.data?.connected ? <MarginLine m={mg.data} /> : null
  if (!d.real_allowed) return (
    <div className="row" style={{ gap: 10, alignItems: 'center', flexWrap: 'wrap', padding: '6px 0' }}>
      <span className="chip muted">PAPER</span>
      <span className="sub">Real orders for <b>{symbol}</b></span>
      <button className="btn sm" disabled title={d.blockers.join(' ')}>Turn real ON</button>
      {marginLine}
      <span className="sub" style={{ flexBasis: '100%' }}>Not available yet: {d.blockers.length ? d.blockers.join(' · ') : 'an admin has not allowed real orders for your account'}. <Link to="/settings">Settings</Link></span>
    </div>
  )
  const st = tb.data?.instruments.find((x) => x.instrument_id === instrumentId)
  const accountReal = d.account_mode === 'real'
  const on = it.real_on && accountReal
  return (
    <div className="row" style={{ gap: 10, alignItems: 'center', flexWrap: 'wrap', padding: '6px 0' }}>
      <span className={'chip ' + (on ? 'bad' : 'muted')}>{on ? 'REAL' : 'PAPER'}</span>
      <span className="sub">Real orders for <b>{symbol}</b></span>
      <button className={'btn sm' + (on ? '' : ' danger')} disabled={set.isPending || (!on && !accountReal)}
        title={!accountReal ? d.blockers.join(' ') || 'Switch your account to REAL in Settings first.' : ''}
        onClick={() => {
          if (on) set.mutate(false)
          else if (window.confirm(`Turn REAL orders ON for ${symbol}?\n\nThis instrument will trade real money in your Angel One account, within the limits an admin set for you. You can turn it off here at any time.`)) set.mutate(true)
        }}>{on ? 'Switch to paper' : 'Turn real ON'}</button>
      {(on || st?.position || st?.pending_entry) && (
        <button className="btn sm danger" disabled={exit.isPending || st?.exiting}
          title="Turns real OFF for this instrument and closes the real position at Angel One now with a LIMIT order"
          onClick={() => { if (window.confirm(`EXIT NOW for ${symbol}?\n\nReal orders go OFF for this instrument, the stop is cancelled and the open real position is closed at Angel One with a LIMIT order at the live price.`)) exit.mutate() }}>
          {st?.exiting ? 'Exiting…' : exit.isPending ? 'Sending…' : 'Exit now'}
        </button>
      )}
      {marginLine}
      {!accountReal && <span className="sub">Your account is on paper. <Link to="/settings">Switch to REAL in Settings</Link>.</span>}
      {st && <TbStatus st={st} onClear={() => { if (window.confirm('Have you looked at your Angel One orders and positions? Clearing the halt lets the app send orders again.')) clear.mutate() }} clearing={clear.isPending} />}
    </div>
  )
}

interface Margin { connected: boolean; error?: string; contract?: string; lots?: number; qty?: number; need_buy?: number | null; need_sell?: number | null; available?: number | null; enough?: boolean | null; short_by?: number | null }
const rs = (v: number | null | undefined) => (v == null ? '–' : '₹' + Math.round(v).toLocaleString('en-IN'))

/** Angel One's own margin calculator for one real order of this instrument, next to the cash in the account. */
function MarginLine({ m }: { m: Margin }) {
  if (m.error) return <span className="sub" style={{ flexBasis: '100%' }}>Margin: {m.error}</span>
  return (
    <span className="sub" style={{ flexBasis: '100%' }}>
      Margin for {m.lots} lot (qty {m.qty}) {m.contract}: <b>BUY {rs(m.need_buy)}</b> · <b>SELL {rs(m.need_sell)}</b> · available <b>{rs(m.available)}</b>{' '}
      {m.enough == null ? '' : m.enough ? <span className="chip ok">enough</span> : <span className="chip bad">short by {rs(m.short_by)}: entries will be skipped</span>}
    </span>
  )
}

function TbStatus({ st, onClear, clearing }: { st: TbState; onClear: () => void; clearing: boolean }) {
  const p = st.position
  return (
    <div style={{ flexBasis: '100%', display: 'grid', gap: 4 }}>
      {st.halted && (
        <div role="alert" style={{ padding: 10, border: '1px solid var(--danger)', borderRadius: 8, background: 'var(--danger-soft)' }}>
          <b>Halted: no order will be sent.</b> <span className="sub">{st.halted}</span>
          <div><button className="btn sm" disabled={clearing} onClick={onClear}>I checked Angel One: carry on</button></div>
        </div>
      )}
      <div className="sub">
        {p ? <>Real position: <b>{p.side} {p.qty}</b> {p.contract} at {p.entry_price ?? '–'}{st.stop_target != null ? ` · stop ${st.stop_target}` : ''}</> : st.pending_entry ? 'Entry is being placed at Angel One…' : 'No real position.'}
        {st.released && ' · real switch is off: this position keeps its stop at Angel One, manage it there'}
      </div>
      {st.orders.slice(-3).map((o) => <div key={o.id} className="sub">{o.role} {o.side} {o.qty} {o.type}{o.price != null ? ` @ ${o.price}` : ''}{o.trigger != null ? ` (trigger ${o.trigger})` : ''} · {o.state}</div>)}
      {st.missed.length > 0 && <div className="sub">Last missed entry: {st.missed[st.missed.length - 1].reason}</div>}
    </div>
  )
}
