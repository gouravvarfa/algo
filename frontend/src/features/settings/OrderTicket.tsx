import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { ApiError, get, post } from '../../lib/api'

interface Contracts { symbol: string; expiries: string[]; lot_size: number | null; strikes: Record<string, number[]> }
interface Check { name: string; ok: boolean; detail: string }
interface Order {
  id: string; symbol: string; expiry: string; strike: number; opt: string; side: string; lots: number; qty: number; price: number; product: string; intent: string
  trading_symbol: string; status: string; broker_order_id: string | null; message: string | null; created_at: number; checks: Check[]
}
interface Preview extends Order { ok: boolean; expires_in: number; ltp: number | null; margin_required: number | null }

const inr = (v: number | null | undefined) => (v == null ? '–' : '₹' + v.toLocaleString('en-IN', { maximumFractionDigits: 2 }))
const when = (t: number) => new Date(t * 1000).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
const STATUS: Record<string, string> = { placed: 'Sent', rejected: 'Refused', unknown: 'Unknown', cancelled: 'Cancelled', sending: 'Sending', expired: 'Expired', previewed: 'Previewed' }
const TONE: Record<string, string> = { placed: 'ok', rejected: 'bad', unknown: 'bad', cancelled: 'muted', sending: 'warn', expired: 'muted', previewed: 'muted' }

interface Fail { message: string; checks: Check[] | null }
async function call<T>(fn: () => Promise<T>): Promise<T> {
  try { return await fn() } catch (e) {
    if (e instanceof ApiError) {
      const body = (e as ApiError & { body?: { checks?: Check[] | null } }).body
      throw Object.assign(new Error(e.message), { checks: body?.checks ?? null })
    }
    throw e
  }
}

/** A real order in two steps: Preview runs every check and shows exactly what would be sent; Send sends that one preview, once. Also lists the orders placed from here. */
export function OrderTicket({ symbols, maxLots, canTrade }: { symbols: string[]; maxLots: number; canTrade: boolean }) {
  const qc = useQueryClient()
  const [symbol, setSymbol] = useState(symbols[0] ?? 'NIFTY')
  const [expiry, setExpiry] = useState('')
  const [strike, setStrike] = useState('')
  const [opt, setOpt] = useState<'CE' | 'PE'>('CE')
  const [side, setSide] = useState<'BUY' | 'SELL'>('BUY')
  const [lots, setLots] = useState('1')
  const [price, setPrice] = useState('')
  const [product, setProduct] = useState('CARRYFORWARD')
  const [pv, setPv] = useState<Preview | null>(null)
  const [left, setLeft] = useState(0)
  const [fail, setFail] = useState<Fail | null>(null)
  const [done, setDone] = useState<Order | null>(null)
  const contracts = useQuery({ queryKey: ['broker-contracts', symbol], queryFn: () => get<Contracts>(`/api/me/broker/contracts?symbol=${symbol}`), staleTime: 300_000 })
  const log = useQuery({ queryKey: ['broker-orders'], queryFn: () => get<{ orders: Order[] }>('/api/me/broker/orders'), refetchInterval: 15_000 })
  const exps = contracts.data?.expiries ?? []
  const strikes = contracts.data?.strikes[expiry || exps[0]] ?? []
  useEffect(() => { if (exps.length && !exps.includes(expiry)) setExpiry(exps[0]) }, [exps.join(','), expiry]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setPv(null); setFail(null); setDone(null) }, [symbol, expiry, strike, opt, side, lots, price, product])
  useEffect(() => {
    if (!pv) return
    setLeft(pv.expires_in)
    const t = setInterval(() => setLeft((x) => Math.max(0, x - 1)), 1000)
    return () => clearInterval(t)
  }, [pv?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  const body = { symbol, expiry: expiry || exps[0], strike: Number(strike), opt, side, lots: Number(lots), price: Number(price), product }
  const ready = !!(body.expiry && strike && Number(price) > 0 && Number(lots) >= 1)
  const fromErr = (e: unknown): Fail => ({ message: e instanceof Error ? e.message : 'Something went wrong', checks: (e as { checks?: Check[] | null }).checks ?? null })
  const prev = useMutation({
    mutationFn: () => call(() => post<Preview>('/api/me/broker/orders/preview', body)),
    onSuccess: (p) => { setPv(p); setFail(null); setDone(null) },
    onError: (e) => { setPv(null); setFail(fromErr(e)) },
  })
  const send = useMutation({
    mutationFn: (id: string) => call(() => post<Order>(`/api/me/broker/orders/${id}/send`)),
    onSuccess: (o) => { setDone(o); setPv(null); setFail(null); void qc.invalidateQueries({ queryKey: ['broker-orders'] }) },
    onError: (e) => { setFail(fromErr(e)); setPv(null); void qc.invalidateQueries({ queryKey: ['broker-orders'] }) },
  })
  const cancel = useMutation({
    mutationFn: (id: string) => call(() => post<Order>(`/api/me/broker/orders/${id}/cancel`)),
    onSettled: () => { void qc.invalidateQueries({ queryKey: ['broker-orders'] }) },
    onError: (e) => setFail(fromErr(e)),
  })
  const line = (o: Pick<Order, 'side' | 'lots' | 'qty' | 'symbol' | 'strike' | 'opt' | 'expiry' | 'price'>) => `${o.side} ${o.lots} lot${o.lots > 1 ? 's' : ''} (${o.qty}) ${o.symbol} ${o.strike} ${o.opt} · exp ${o.expiry} · limit ${inr(o.price)}`

  return (
    <div className="card" style={{ marginTop: 12, borderColor: 'var(--danger)' }}>
      <div className="card-head"><h2>Real orders</h2><span className="spacer" /><span className={'chip ' + (canTrade ? 'bad' : 'muted')}>{canTrade ? 'REAL MONEY' : 'REAL MODE OFF'}</span></div>
      {!canTrade && <p className="sub">Real mode is off right now, so no new order can be sent. Orders you already placed are listed below and can still be cancelled.</p>}
      {canTrade && <><p className="sub">Limit orders on NIFTY / BANKNIFTY options. First <b>Preview</b>: every safety check runs and you see exactly what would be sent. Then <b>Send</b>. Your limit: <b>{maxLots} lot{maxLots > 1 ? 's' : ''}</b>.</p>
      <form className="col" style={{ gap: 8, maxWidth: 520 }} onSubmit={(e) => { e.preventDefault(); if (ready) prev.mutate() }}>
        <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
          <label className="inline">Index<select value={symbol} onChange={(e) => { setSymbol(e.target.value); setStrike(''); setExpiry('') }}>{symbols.map((s) => <option key={s}>{s}</option>)}</select></label>
          <label className="inline">Expiry<select value={expiry || exps[0] || ''} onChange={(e) => { setExpiry(e.target.value); setStrike('') }}>{exps.map((x) => <option key={x}>{x}</option>)}</select></label>
          <label className="inline">Strike<input list="bk-strikes" inputMode="decimal" value={strike} onChange={(e) => setStrike(e.target.value)} placeholder="e.g. 25000" style={{ width: 110 }} /></label>
          <datalist id="bk-strikes">{strikes.map((k) => <option key={k} value={k} />)}</datalist>
        </div>
        <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <span className="seg"><button type="button" className={opt === 'CE' ? 'on' : ''} onClick={() => setOpt('CE')}>CE</button><button type="button" className={opt === 'PE' ? 'on' : ''} onClick={() => setOpt('PE')}>PE</button></span>
          <span className="seg"><button type="button" className={side === 'BUY' ? 'on' : ''} onClick={() => setSide('BUY')}>BUY</button><button type="button" className={side === 'SELL' ? 'on' : ''} onClick={() => setSide('SELL')}>SELL</button></span>
          <label className="inline">Lots<input type="number" min={1} value={lots} onChange={(e) => setLots(e.target.value)} style={{ width: 70 }} /></label>
          <label className="inline">Limit ₹<input inputMode="decimal" value={price} onChange={(e) => setPrice(e.target.value)} style={{ width: 100 }} /></label>
          <label className="inline"><select value={product} onChange={(e) => setProduct(e.target.value)}><option value="CARRYFORWARD">Carry forward (NRML)</option><option value="INTRADAY">Intraday (MIS)</option></select></label>
        </div>
        <div><button className="btn" disabled={!ready || prev.isPending}>{prev.isPending ? 'Checking…' : 'Preview'}</button></div>
      </form>

      {fail && (
        <div role="alert" style={{ marginTop: 10 }}>
          <p className="err">{fail.message}</p>
          {fail.checks && <Checks checks={fail.checks} />}
        </div>
      )}
      {pv && (
        <div style={{ marginTop: 10, padding: 12, background: 'var(--bg-hover)', borderRadius: 10 }}>
          <b>{line(pv)}</b>
          <div className="sub">{pv.intent === 'exit' ? 'This closes (reduces) an open position.' : 'This opens or adds to a position.'} · {pv.product} · last traded {inr(pv.ltp)} · margin needed {inr(pv.margin_required)}</div>
          <Checks checks={pv.checks} />
          {pv.ok ? (
            <div className="row" style={{ gap: 10, marginTop: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <button className="btn danger" disabled={send.isPending || left === 0}
                onClick={() => { if (window.confirm(`Send this REAL order to Angel One?\n\n${line(pv)}`)) send.mutate(pv.id) }}>{send.isPending ? 'Sending…' : 'Send to Angel One'}</button>
              <span className="sub">{left > 0 ? `valid for ${left}s` : 'expired, preview again'}</span>
            </div>
          ) : <p className="err" style={{ marginTop: 8 }}>Not sendable: fix the items marked ✗.</p>}
        </div>
      )}
      {done && <p className="ok" style={{ marginTop: 10 }}>Sent. Angel One order id <b>{done.broker_order_id}</b>. A limit order may stay open until the price is reached: watch its status below.</p>}
      </>}

      {log.data && log.data.orders.length > 0 && (
        <div style={{ marginTop: 12, overflowX: 'auto' }}>
          <table className="tbl"><thead><tr><th>Time</th><th>Order</th><th>Price</th><th>Status</th><th /></tr></thead>
            <tbody>{log.data.orders.map((o) => (
              <tr key={o.id}><td>{when(o.created_at)}</td><td>{o.side} {o.lots}×{o.symbol} {o.strike} {o.opt}<div className="sub">{o.expiry} · {o.intent}</div></td><td>{inr(o.price)}</td>
                <td><span className={'chip ' + (TONE[o.status] ?? 'muted')} title={o.message ?? ''}>{STATUS[o.status] ?? o.status}</span>{o.status === 'unknown' && <div className="sub err">Check your Angel One orders</div>}</td>
                <td>{o.status === 'placed' && <button className="btn sm" disabled={cancel.isPending} onClick={() => { if (window.confirm('Cancel this order?')) cancel.mutate(o.id) }}>Cancel</button>}</td></tr>
            ))}</tbody></table>
        </div>
      )}
    </div>
  )
}

function Checks({ checks }: { checks: Check[] }) {
  return (
    <ul style={{ listStyle: 'none', margin: '8px 0 0', padding: 0, display: 'grid', gap: 3 }}>
      {checks.map((c) => (
        <li key={c.name} style={{ display: 'flex', gap: 8, color: c.ok ? 'var(--text-secondary)' : 'var(--danger)', fontWeight: c.ok ? 400 : 600 }}>
          <span aria-hidden>{c.ok ? '✓' : '✗'}</span><span><b>{c.name}</b> · <span style={{ fontWeight: 400 }}>{c.detail}</span></span>
        </li>
      ))}
    </ul>
  )
}
