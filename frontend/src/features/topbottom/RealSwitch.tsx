import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type ReactNode } from 'react'
import './realswitch.css'
import { createPortal } from 'react-dom'
import { Link } from 'react-router-dom'
import { ApiError, get, post, put } from '../../lib/api'
import { useDataSource } from '../../lib/datasource'
import { useToast } from '../../lib/toast'

interface Item { instrument_id: string; symbol: string; real_on: boolean; real_effective: boolean; real_lots: number; lot_size: number; paper_lots: number }
interface TbState {
  instrument_id: string; position: { side: string; qty: number; contract: string; entry_price: number | null } | null; pending_entry: boolean; halted: string | null; stop_target: number | null
  released: boolean; exiting?: boolean; missed: { at: string; reason: string }[]; events: { at: string; text: string }[]; orders: { id: string; role: string; side: string; qty: number; type: string; price: number | null; trigger: number | null; state: string }[]
}
interface Data { account_mode: string; real_allowed: boolean; max_lots: number; blockers: string[]; instruments: Item[] }

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
    mutationFn: (real: boolean) => put<Data>(`/api/me/broker/instruments/${instrumentId}`, real ? { real, lots } : { real }),
    onSuccess: (d, real) => { qc.setQueryData(['broker-instruments'], d); void qc.invalidateQueries({ queryKey: ['broker-tb-trades'] }); void qc.invalidateQueries({ queryKey: ['broker-tb'] }); void qc.invalidateQueries({ queryKey: ['broker-margin'] }); toast(real ? `Real orders armed for ${symbol}: ${lots} lot${lots > 1 ? 's' : ''}` : `${symbol} is on paper`) },
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Could not change the switch'),
  })
  const d = q.data
  const [pickLots, setPickLots] = useState<number | null>(null)
  const [ticket, setTicket] = useState(false)
  const it = d?.instruments.find((i) => i.instrument_id === instrumentId)
  const mg = useQuery({ queryKey: ['broker-margin', instrumentId], queryFn: () => get<Margin>(`/api/me/broker/tb/${instrumentId}/margin`), enabled: !ds.readOnly && !!d?.real_allowed, refetchInterval: 120_000, retry: false })
  const lots = pickLots ?? it?.real_lots ?? 1
  if (ds.readOnly || !d || !it) return null
  const marginLine = mg.data?.connected ? <MarginLine m={mg.data} lots={lots} /> : null
  if (!d.real_allowed) return (
    <div className="realpanel off">
      <div className="rp-main">
        <span className="rp-dot" aria-hidden />
        <div className="rp-text"><b>Real trading · {symbol}</b><span>Not available yet: {d.blockers.length ? d.blockers.join(' · ') : 'an admin has not allowed real orders for your account'}. <Link to="/settings">Settings</Link></span></div>
        <button className="rp-btn" disabled title={d.blockers.join(' ')}>Turn REAL on</button>
      </div>
    </div>
  )
  const st = tb.data?.instruments.find((x) => x.instrument_id === instrumentId)
  const accountReal = d.account_mode === 'real'
  const on = it.real_on && accountReal
  return (
    <div className={'realpanel ' + (on ? 'on' : 'off')}>
      <div className="rp-main">
        <span className="rp-dot" aria-hidden />
        <div className="rp-text">
          <b>{on ? 'REAL trading is ON' : 'Real trading is off'} · {symbol}</b>
          <span>{on ? <>Every new System Point goes to Angel One · <b>{it.real_lots} lot{it.real_lots > 1 ? 's' : ''}</b> (qty {it.real_lots * it.lot_size}) · limit orders, automatic stop-loss</>
            : !accountReal ? <>Your account is on paper. <Link to="/settings">Switch to REAL in Settings</Link> first.</>
            : 'Paper keeps running either way. Turning real on sends nothing now: the first order goes at the next new System Point.'}</span>
        </div>
        <div className="rp-actions">
          {(on || st?.position || st?.pending_entry) && (
            <button className="rp-btn exit" disabled={exit.isPending || st?.exiting}
              title="Turns real OFF for this instrument and closes the real position at Angel One now with a LIMIT order"
              onClick={() => { if (window.confirm(`EXIT NOW for ${symbol}?

Real orders go OFF for this instrument, the stop is cancelled and the open real position is closed at Angel One with a LIMIT order at the live price.`)) exit.mutate() }}>
              {st?.exiting ? 'Exiting…' : exit.isPending ? 'Sending…' : 'Exit now'}
            </button>
          )}
          <button className={'rp-btn ' + (on ? 'pause' : 'go')} disabled={set.isPending || (!on && !accountReal)}
            title={!accountReal ? d.blockers.join(' ') || 'Switch your account to REAL in Settings first.' : ''}
            onClick={() => {
              if (on) set.mutate(false)
              else { setPickLots(it.real_lots || 1); setTicket(true); void mg.refetch() }
            }}>{set.isPending ? '…' : on ? 'Switch to paper' : 'Turn REAL on'}</button>
        </div>
      </div>
      {on && marginLine && <div className="rp-foot">{marginLine}</div>}
      {ticket && <RealTicket symbol={symbol} contract={mg.data?.contract ?? ''} lotSize={it.lot_size} maxLots={d.max_lots} lots={lots} setLots={setPickLots} m={mg.data}
        busy={set.isPending} onClose={() => setTicket(false)} onArm={() => set.mutate(true, { onSuccess: () => setTicket(false) })} />}
      {st && <TbStatus st={st} onClear={() => { if (window.confirm('Have you looked at your Angel One orders and positions? Clearing the halt lets the app send orders again.')) clear.mutate() }} clearing={clear.isPending} />}
    </div>
  )
}

interface Margin { connected: boolean; error?: string; contract?: string; ltp?: number | null; lots?: number; qty?: number; need_buy?: number | null; need_sell?: number | null; available?: number | null; enough?: boolean | null; short_by?: number | null }
const rs = (v: number | null | undefined) => (v == null ? '–' : '₹' + Math.round(v).toLocaleString('en-IN'))

/** Angel One's own margin calculator for one real order of this instrument, next to the cash in the account. */
function MarginLine({ m, lots }: { m: Margin; lots: number }) {
  if (m.error && m.need_buy == null) return <span className="sub" style={{ flexBasis: "100%" }}>{m.error} · available in Angel One <b>{rs(m.available)}</b></span>
  const k = m.lots ? lots / m.lots : 1                                    // Angel One quoted m.lots lots; futures margin grows with the lots
  const buy = m.need_buy == null ? null : m.need_buy * k, sell = m.need_sell == null ? null : m.need_sell * k
  const worst = Math.max(buy ?? 0, sell ?? 0), short = m.available != null && worst > m.available ? worst - m.available : null
  return (
    <span className="sub" style={{ flexBasis: '100%' }}>
      Margin for <b>{lots} lot{lots > 1 ? 's' : ''}</b> {m.contract}: BUY <b>{rs(buy)}</b> · SELL <b>{rs(sell)}</b> · available in Angel One <b>{rs(m.available)}</b>{' '}
      {m.available == null || !worst ? '' : short == null ? <span className="chip ok">enough</span> : <span className="chip bad">short by {rs(short)}: entries will be skipped</span>}
      <span title="From Angel One's margin calculator, refreshed every 2 minutes"> ⓘ</span>
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

/** The order ticket shown when real is switched on, laid out like a broker's: what each real order will be, what it needs, what it costs.
 * Arming sends nothing: the first order goes out at the next new System Point, in the strategy's direction. */
function RealTicket({ symbol, contract, lotSize, maxLots, lots, setLots, m, busy, onClose, onArm }: {
  symbol: string; contract: string; lotSize: number; maxLots: number; lots: number; setLots: (n: number) => void; m?: Margin; busy: boolean; onClose: () => void; onArm: () => void
}) {
  const k = m?.lots ? lots / m.lots : 1
  const buy = m?.need_buy != null ? m.need_buy * k : null
  const sell = m?.need_sell != null ? m.need_sell * k : null
  const need = Math.max(buy ?? 0, sell ?? 0) || null
  const short = need != null && m?.available != null && need > m.available ? need - m.available : null
  const qty = lots * lotSize
  const charges = m?.ltp ? estCharges(m.ltp * qty) : null
  return createPortal(
    <div role="dialog" aria-modal="true" onClick={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.55)', zIndex: 10000, display: 'grid', placeItems: 'center', padding: 16 }}>
      <div onClick={(e) => e.stopPropagation()} style={{ width: 'min(520px, 100%)', maxHeight: '92vh', overflowY: 'auto', padding: 0, background: 'var(--bg-surface, #fff)', borderRadius: 16, boxShadow: '0 20px 60px rgba(0,0,0,.35)', border: '1px solid var(--border)' }}>
        <div className="row" style={{ alignItems: 'center', padding: '18px 20px 6px' }}>
          <div><b style={{ fontSize: 17 }}>{contract || symbol}</b>{m?.ltp != null && <div className="num">{m.ltp.toLocaleString('en-IN', { minimumFractionDigits: 2 })}</div>}</div>
          <span className="spacer" />
          <span className="chip" title="The strategy decides BUY or SELL at the next System Point">BUY / SELL by strategy</span>
        </div>
        <div style={{ padding: '0 20px' }}>
          <TRow label="Quantity">
            <span className="row" style={{ alignItems: 'center', gap: 6, border: '1px solid var(--border)', borderRadius: 999, padding: '2px 6px' }}>
              <button type="button" className="btn sm" aria-label="one lot less" disabled={lots <= 1} onClick={() => setLots(lots - 1)}>−</button>
              <span style={{ textAlign: 'center', minWidth: 60 }}><b className="num">{qty}</b><div className="sub" style={{ fontSize: 11 }}>{lots} lot{lots > 1 ? 's' : ''}</div></span>
              <button type="button" className="btn sm" aria-label="one lot more" disabled={lots >= maxLots} onClick={() => setLots(lots + 1)}>+</button>
            </span>
          </TRow>
          <TRow label="Order"><span style={{ textAlign: 'right' }}><b>Limit · Normal (carry forward) · Day</b><br /><span className="sub">price set at the System Point, at most 0.5% from the strategy</span></span></TRow>
          <div className="card" style={{ margin: '12px 0', padding: 12 }}>
            <div className="row" style={{ alignItems: 'center' }}><span className="chip bad">SL</span>&nbsp;<b>Stop Loss</b><span className="spacer" /><span className="chip ok">ON · automatic</span></div>
            <div className="sub" style={{ marginTop: 4 }}>Placed at Angel One right after the entry fills, at the strategy&apos;s stop, and moved whenever the strategy moves it.</div>
          </div>
          <div className="card" style={{ margin: '0 0 12px', padding: 12 }}>
            <div className="row" style={{ alignItems: 'center' }}><b>Reversal</b><span className="spacer" /><span className="chip muted">exit first, then the new entry</span></div>
          </div>
        </div>
        <div className="sub" style={{ background: 'var(--bg-subtle, rgba(0,0,0,.04))', padding: '10px 20px' }}>
          Nothing is sent now. The first real order goes out at the <b>next new System Point</b>; the paper position already open is not copied.
        </div>
        {short != null && <div style={{ background: 'var(--danger-soft)', color: 'var(--danger)', padding: '10px 20px', fontWeight: 600 }}>⚠ Insufficient funds · add {rs(short)} or entries will be skipped</div>}
        {m?.error && <div style={{ color: 'var(--danger)', padding: '10px 20px' }}>{m.error}</div>}
        <div className="row" style={{ padding: '14px 20px', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <div className="sub" style={{ lineHeight: 1.6 }}>
            Est. balance: <b>{rs(m?.available)}</b><br />
            {need != null && <><span style={{ color: short != null ? 'var(--danger)' : undefined }}>Req margin: <b>{rs(need)}</b></span>{buy != null && sell != null && <span title="BUY / SELL"> ({rs(buy)} / {rs(sell)})</span>}<br /></>}
            {charges != null && <>Charges ≈ <b>{rs(charges)}</b> <span title="Estimate for one entry order: brokerage ₹20, exchange, SEBI, stamp duty and GST. The exit costs about the same plus STT.">ⓘ</span></>}
          </div>
          <span className="spacer" />
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button type="button" className="btn danger" disabled={busy} onClick={onArm} style={{ minWidth: 150 }}>{busy ? 'Arming…' : `Arm real · ${lots} lot${lots > 1 ? 's' : ''}`}</button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

function TRow({ label, children }: { label: string; children: ReactNode }) {
  return <div className="row" style={{ alignItems: 'center', minHeight: 44, borderBottom: '1px solid var(--border)' }}><span>{label}</span><span className="spacer" />{children}</div>
}


/** Rough cost of one futures entry order at Angel One: ₹20 brokerage, NSE transaction 0.00173%, SEBI ₹10 per crore, stamp 0.002% (buy side), GST 18% on brokerage + fees. */
export function estCharges(turnover: number): number {
  const brok = 20, txn = turnover * 0.0000173, sebi = turnover * 0.000001, stamp = turnover * 0.00002
  return brok + txn + sebi + stamp + 0.18 * (brok + txn + sebi)
}
