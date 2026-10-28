import { useMutation, useQuery } from '@tanstack/react-query'
import { Fragment, useState } from 'react'
import { ApiError, get, post } from '../../lib/api'
import { useBrokerName } from '../../lib/broker'
import { n0, num } from '../../lib/format'
import './options.css'

interface Leg { ltp: number | null; bid: number | null; ask: number | null; volume: number | null; oi: number | null; iv: number | null; delta: number | null; greeks_source: string; flags: string[]; liquid: boolean }
interface Row { strike: number; CE: Leg | null; PE: Leg | null }
interface Chain {
  spot: number; atm: number; vix: number | null; pcr: number | null; expiry: string; lot_size: number; market_open: boolean; fetched_at: string
  violations: string[]; stale: string[]; warnings: string[]; rows: Row[]
  helpers: { CE: Record<string, number | null>; PE: Record<string, number | null> }
}
interface Bhav { symbols: { symbol: string; days: number; rows: number; expiries: number; from: string; to: string }[]; days: { date: string; status: string }[] }
interface EodRow { strike: number; CE: { close: number; settle: number; oi: number; volume: number } | null; PE: { close: number; settle: number; oi: number; volume: number } | null }

const COLS = ['oi', 'volume', 'iv', 'delta', 'bid', 'ask', 'ltp'] as const
const DECIMALS: Record<string, number> = { oi: 0, volume: 0, iv: 1, delta: 2 }

function Cell({ leg, k, itm }: { leg: Leg | null; k: (typeof COLS)[number]; itm: boolean }) {
  if (!leg) return <td className="bad">–</td>
  const v = leg[k]
  const d = DECIMALS[k] ?? 2
  const text = v == null ? '–' : d === 0 ? n0(v) : num(v, d)
  const star = (k === 'iv' || k === 'delta') && leg.greeks_source === 'calc' ? '*' : ''
  return <td className={(leg.liquid ? '' : 'bad ') + (itm ? 'itm' : '')} title={leg.liquid ? undefined : leg.flags.join(', ')}>{text}{star}</td>
}

/** Live option chain straight from the broker the server runs on (Angel One or Upstox, read-only), and the NSE Bhavcopy end-of-day data that the Simulator and Builder use. */
export default function OptionChainPage() {
  const broker = useBrokerName()
  const [sym, setSym] = useState('BANKNIFTY')
  const [exp, setExp] = useState('nearest')
  const [n, setN] = useState(10)
  const [auto, setAuto] = useState(false)
  const exps = useQuery({ queryKey: ['oc', 'exp', sym], queryFn: () => get<{ expiries: string[] }>(`/api/options/expiries?symbol=${sym}`) })
  const chain = useQuery({
    queryKey: ['oc', 'chain', sym, exp, n],
    queryFn: () => get<Chain>(`/api/options/chain?symbol=${sym}&expiry=${exp}&strikes=${n}`),
    enabled: false, refetchInterval: auto ? 15_000 : false, retry: false,
  })
  const c = chain.data
  return (
    <div className="page">
      <div className="row" style={{ alignItems: 'center' }}><h1>Option chain</h1><span className="chip">Read-only · real data</span><span className="chip muted">No broker orders</span></div>
      <div className="card">
        <div className="card-head"><h2>Live option chain ({broker})</h2></div>
        <div className="row" style={{ alignItems: 'center' }}>
          <select value={sym} onChange={(e) => { setSym(e.target.value); setExp('nearest') }}><option>BANKNIFTY</option><option>NIFTY</option></select>
          <select value={exp} onChange={(e) => setExp(e.target.value)}><option value="nearest">nearest</option>{exps.data?.expiries.map((e) => <option key={e}>{e}</option>)}</select>
          <label className="inline">Strikes ± <input type="number" value={n} min={1} max={30} style={{ width: 64 }} onChange={(e) => setN(Math.max(1, Math.min(30, Number(e.target.value) || 10)))} /></label>
          <button className="btn primary" onClick={() => void chain.refetch()} disabled={chain.isFetching}>{chain.isFetching ? 'Fetching…' : 'Fetch chain'}</button>
          <label className="inline"><input type="checkbox" checked={auto} onChange={(e) => { setAuto(e.target.checked); if (e.target.checked) void chain.refetch() }} /> auto-refresh 15 s</label>
        </div>
        {chain.error && <div className="alert" style={{ marginTop: 10 }}>{(chain.error as ApiError).message}</div>}
        {c && (
          <>
            <div className="row" style={{ marginTop: 10, gap: 6 }}>
              {([['Spot', num(c.spot)], ['ATM', n0(c.atm)], ['VIX', num(c.vix)], ['PCR', num(c.pcr)], ['Expiry', c.expiry], ['Lot', String(c.lot_size)], ['Market', c.market_open ? 'OPEN' : 'CLOSED'], ['Fetched', c.fetched_at.slice(11, 19)]] as [string, string][])
                .map(([k, v]) => <span key={k} className="chip">{k}: <b>{v}</b></span>)}
            </div>
            <ul className="msgs">
              {!c.violations.length && !c.stale.length && <li>Data quality: 0 hard violations</li>}
              {c.violations.map((v) => <li key={v} className="err">HARD: {v}</li>)}{c.stale.map((v) => <li key={v} className="err">STALE: {v}</li>)}{c.warnings.map((v) => <li key={v}>{v}</li>)}
              <li>Helpers — CE: OTM+100 {n0(c.helpers.CE.otm_100)}, Δ0.30 {n0(c.helpers.CE.delta_30)}, ~₹100 {n0(c.helpers.CE.premium_100)} · PE: OTM+100 {n0(c.helpers.PE.otm_100)}, Δ0.30 {n0(c.helpers.PE.delta_30)}, ~₹100 {n0(c.helpers.PE.premium_100)}</li>
            </ul>
            <div className="scroll">
              <table className="oc">
                <thead>
                  <tr><th colSpan={7} className="mid">CALLS</th><th /><th colSpan={7} className="mid">PUTS</th></tr>
                  <tr>{COLS.map((k) => <th key={k}>{k.toUpperCase()}</th>)}<th className="mid">STRIKE</th>{[...COLS].reverse().map((k) => <th key={k}>{k.toUpperCase()}</th>)}</tr>
                </thead>
                <tbody>
                  {c.rows.map((r) => (
                    <tr key={r.strike} className={r.strike === c.atm ? 'atm' : ''}>
                      {COLS.map((k) => <Cell key={`c${k}`} leg={r.CE} k={k} itm={r.strike < c.spot} />)}
                      <td className="k">{n0(r.strike)}</td>
                      {[...COLS].reverse().map((k) => <Cell key={`p${k}`} leg={r.PE} k={k} itm={r.strike > c.spot} />)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="note">Shaded = ITM. Red cell = flagged leg (illiquid / stale / bad). Greeks marked * are Black-Scholes fallback, not broker.</p>
          </>
        )}
      </div>
      <Bhavcopy />
    </div>
  )
}

function Bhavcopy() {
  const sum = useQuery({ queryKey: ['oc', 'bhav'], queryFn: () => get<Bhav>('/api/options/bhavcopy/summary') })
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [msg, setMsg] = useState('')
  const load = useMutation({
    mutationFn: () => post<{ loaded_days: number; rows: number; rejected: number; no_file_days: string[]; errors: string[] }>(`/api/options/bhavcopy/load?start=${from}&end=${to}`),
    onSuccess: (s) => { setMsg(`${s.loaded_days} days loaded, ${s.rows} rows, ${s.rejected} rejected, no file: ${s.no_file_days.length}, errors: ${s.errors.length}`); void sum.refetch() },
    onError: (e) => setMsg((e as Error).message),
  })
  const [bsym, setBsym] = useState('BANKNIFTY')
  const [day, setDay] = useState('')
  const [bexp, setBexp] = useState('')
  const date = day || sum.data?.symbols[0]?.to || ''
  const exps = useQuery({ queryKey: ['oc', 'bexp', bsym, date], enabled: !!date, queryFn: () => get<{ expiries: string[] }>(`/api/options/bhavcopy/expiries?symbol=${bsym}&date=${date}`) })
  const eod = useQuery({
    queryKey: ['oc', 'eod', bsym, date, bexp || exps.data?.expiries[0]], enabled: false,
    queryFn: () => get<{ underlying: number; lot_size: number; rows: EodRow[] }>(`/api/options/bhavcopy/chain?symbol=${bsym}&date=${date}&expiry=${bexp || exps.data?.expiries[0]}`),
  })
  const d = eod.data
  const near = d?.rows.filter((r) => Math.abs(r.strike - d.underlying) <= d.underlying * 0.04) ?? []
  const missing = sum.data?.days.filter((x) => x.status !== 'loaded').map((x) => x.date) ?? []
  return (
    <div className="card">
      <div className="card-head"><h2>NSE Bhavcopy EOD data</h2></div>
      {sum.data?.symbols.length
        ? <div className="row" style={{ gap: 6 }}>{sum.data.symbols.map((s) => <span key={s.symbol} className="chip">{s.symbol}: {s.days} days, {n0(s.rows)} rows, {s.expiries} expiries ({s.from} → {s.to})</span>)}</div>
        : <p className="sub">{sum.isLoading ? 'loading…' : 'No Bhavcopy data loaded yet.'}</p>}
      {missing.length > 0 && <p className="note">No file: {missing.join(', ')}</p>}
      <div className="row" style={{ marginTop: 10, alignItems: 'flex-end' }}>
        <label className="field">From<input type="date" value={from} onChange={(e) => setFrom(e.target.value)} /></label>
        <label className="field">To<input type="date" value={to} onChange={(e) => setTo(e.target.value)} /></label>
        <button className="btn" disabled={!from || !to || load.isPending} onClick={() => load.mutate()}>{load.isPending ? 'Downloading from NSE (1 s per day)…' : 'Download & load'}</button>
        <span className="sub">{msg}</span>
      </div>
      <div className="row" style={{ marginTop: 10, alignItems: 'flex-end' }}>
        <select value={bsym} onChange={(e) => setBsym(e.target.value)}><option>BANKNIFTY</option><option>NIFTY</option></select>
        <input type="date" value={date} onChange={(e) => setDay(e.target.value)} />
        <select value={bexp} onChange={(e) => setBexp(e.target.value)}>{exps.data?.expiries.map((e) => <option key={e}>{e}</option>)}{!exps.data?.expiries.length && <option value="">(no data this date)</option>}</select>
        <button className="btn" onClick={() => void eod.refetch()} disabled={!date || eod.isFetching}>Show EOD chain</button>
        {d && <span className="sub">Underlying close {num(d.underlying)} · lot {d.lot_size} · {d.rows.length} strikes</span>}
        {eod.error && <span className="neg">{(eod.error as Error).message}</span>}
      </div>
      {d && (
        <div className="scroll" style={{ marginTop: 10 }}>
          <table className="oc">
            <thead><tr><th>CE close</th><th>settle</th><th>OI</th><th>Vol</th><th className="mid">STRIKE</th><th>PE close</th><th>settle</th><th>OI</th><th>Vol</th></tr></thead>
            <tbody>
              {near.map((r) => (
                <tr key={r.strike}>
                  {r.CE ? <Fragment><td>{num(r.CE.close)}</td><td>{num(r.CE.settle)}</td><td>{n0(r.CE.oi)}</td><td>{n0(r.CE.volume)}</td></Fragment> : <td colSpan={4} className="bad">no row</td>}
                  <td className="k">{n0(r.strike)}</td>
                  {r.PE ? <Fragment><td>{num(r.PE.close)}</td><td>{num(r.PE.settle)}</td><td>{n0(r.PE.oi)}</td><td>{n0(r.PE.volume)}</td></Fragment> : <td colSpan={4} className="bad">no row</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
