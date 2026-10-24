import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { dmy, lakh, n0, num } from '../../lib/format'
import type { Chain, ChainRow, Leg, Opt, Side } from './sim'

/** A price that flashes green when it rose and red when it fell since the previous refresh (LIVE mode only). */
function Px({ v, live }: { v: number | null; live: boolean }) {
  const prev = useRef(v)
  const [flash, setFlash] = useState<{ dir: 'up' | 'dn'; n: number } | null>(null)
  useEffect(() => {
    if (live && prev.current != null && v != null && v !== prev.current) setFlash((f) => ({ dir: v > prev.current! ? 'up' : 'dn', n: (f?.n ?? 0) + 1 }))
    prev.current = v
  }, [v, live])
  return <span key={flash?.n ?? 0} className={'lv' + (flash ? ` fl-${flash.dir}` : '')}>{num(v)}</span>
}

function Buttons({ opt, k, onAdd }: { opt: Opt; k: number; onAdd: (s: Side, o: Opt, k: number) => void }) {
  return (
    <span className="bs">
      <button className="b" title="Simulated BUY" onClick={() => onAdd('BUY', opt, k)}>B</button>
      <button className="s" title="Simulated SELL" onClick={() => onAdd('SELL', opt, k)}>S</button>
    </span>
  )
}

function LtpCell({ leg, opt, strike, chain, live, canTrade, onAdd, itm }: { leg: Leg | null; opt: Opt; strike: number; chain: Chain; live: boolean; canTrade: boolean; onAdd: (s: Side, o: Opt, k: number) => void; itm: boolean }) {
  if (!leg) return <td className="na">–</td>
  const cls = `ltp ${opt.toLowerCase()}${itm ? ' itm' : ''}`
  if (leg.ltp == null) return <td className={cls + ' na'} title="no trade in this contract yet today at this minute">–</td>
  const buttons = canTrade && leg.ltp > 0 ? <Buttons opt={opt} k={strike} onAdd={onAdd} /> : null
  const title = chain.mode === 'live' ? (leg.src === 'live' ? `live tick ${leg.ts}` : `no live tick yet: last stored 1-minute close ${leg.ts ?? ''}`) : `Volume ${n0(leg.volume)} · OI change ${n0(leg.chg_oi)}`
  return (
    <td className={cls}>
      {opt === 'CE' && buttons}
      <span title={title}><Px v={leg.ltp} live={live} /></span>
      {leg.delta != null && <small> ({num(leg.delta, 2)})</small>}
      {chain.mode === 'live' ? (leg.src === 'stored' ? <span className="stv">st</span> : null)
        : chain.mode === 'intraday' ? ((leg.age_min ?? 0) > 1 ? <span className="age" title={`last trade at ${leg.ts}`}>{leg.age_min}m</span> : null)
          : (!leg.volume ? <span className="nt" title="No trades this day. NSE close shown.">NT</span> : null)}
      {opt === 'PE' && buttons}
    </td>
  )
}

const Plain = ({ v, f, itm, cls = '', bar }: { v: number | null | undefined; f: (n: number) => string; itm: boolean; cls?: string; bar?: number }) => (
  <td className={(itm ? 'itm ' : '') + (v == null ? 'na ' : '') + cls} style={bar != null ? ({ '--bar': `${bar}%` } as CSSProperties) : undefined}>{v == null ? '–' : f(v)}</td>
)

interface Props {
  chain: Chain | null; error: string; loading: boolean; live: boolean; canTrade: boolean; onAdd: (s: Side, o: Opt, k: number) => void
  strikeView: 'near' | 'all'; setStrikeView: (v: 'near' | 'all') => void; onlyTraded: boolean; setOnlyTraded: (v: boolean) => void; title: string; asof: string; atmKey: string
}

export function ChainPanel({ chain, error, loading, live, canTrade, onAdd, strikeView, setStrikeView, onlyTraded, setOnlyTraded, title, asof, atmKey }: Props) {
  const box = useRef<HTMLDivElement>(null)
  const rows: ChainRow[] = useMemo(() => {
    if (!chain) return []
    const ai = chain.rows.findIndex((r) => r.strike === chain.atm)
    return chain.rows.filter((r, i) => (strikeView === 'all' || Math.abs(i - ai) <= 10) && (!onlyTraded || r.strike === chain.atm || (r.CE?.volume ?? 0) > 0 || (r.PE?.volume ?? 0) > 0))
  }, [chain, strikeView, onlyTraded])

  const maxOi = useMemo(() => ({ ce: Math.max(1, ...rows.map((r) => r.CE?.oi ?? 0)), pe: Math.max(1, ...rows.map((r) => r.PE?.oi ?? 0)) }), [rows])
  const pct = (v: number | null | undefined, m: number) => (v ? Math.max(3, Math.round((v / m) * 100)) : 0)

  useEffect(() => {                         // centre the ATM row when the day / minute / expiry changes, but not on every LIVE refresh
    const el = box.current?.querySelector('tr.atm') as HTMLElement | null
    if (el && box.current) box.current.scrollTop = el.offsetTop - box.current.clientHeight / 2 + el.offsetHeight / 2
  }, [atmKey, strikeView, onlyTraded])

  return (
    <div className="card chainpanel">
      <div className="card-head">
        <h2>{title}</h2><span className="spacer" />
        <span className="seg"><button className={strikeView === 'near' ? 'on' : ''} onClick={() => setStrikeView('near')} title="Near = ATM ± 10 strikes">Near</button><button className={strikeView === 'all' ? 'on' : ''} onClick={() => setStrikeView('all')}>All</button></span>
        <label className="inline" title="Hide strikes that did not trade"><input type="checkbox" checked={onlyTraded} onChange={(e) => setOnlyTraded(e.target.checked)} /> Traded only</label>
        <span className={'chip ' + (chain?.mode === 'live' ? 'bad' : 'warn')} title="IV and delta are computed with Black-Scholes (r = 6.5%, q = 0); they are not broker values. ITM rows are shaded; NT = not traded, the NSE close is shown.">{asof.replace(/^As of /, '').replace(/ 20\d\d/, '')}</span>
      </div>
      <div className="chainbox" ref={box}>
        {loading && <div className="overlay">Loading…</div>}
        {error ? <div className="empty err">{error}</div> : !chain ? <div className="empty">Loading…</div> : (
          <table className="oc">
            <thead>
              <tr><th className="ce">CALL LTP (Δ)</th><th className="iv">IV</th><th>OI</th><th className="mid">Strike<small>spot {num(chain.underlying)}</small></th><th>OI</th><th className="iv">IV</th><th className="pl pe">PUT LTP (Δ)</th></tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const ceItm = r.strike < chain.underlying, peItm = r.strike > chain.underlying, atm = r.strike === chain.atm, off = r.strike - chain.atm
                return (
                  <tr key={r.strike} className={atm ? 'atm' : ''}>
                    <LtpCell leg={r.CE} opt="CE" strike={r.strike} chain={chain} live={live} canTrade={canTrade} onAdd={onAdd} itm={ceItm && !atm} />
                    <Plain v={r.CE?.iv} f={(n) => `${num(n, 1)}%`} itm={ceItm && !atm} cls="iv" /><Plain v={r.CE?.oi} f={lakh} itm={ceItm && !atm} cls="oibar ce" bar={pct(r.CE?.oi, maxOi.ce)} />
                    <td className="k">{n0(r.strike)}<small>{atm ? 'ATM' : `ATM ${off > 0 ? '+' : '−'}${n0(Math.abs(off))}`}</small></td>
                    <Plain v={r.PE?.oi} f={lakh} itm={peItm && !atm} cls="oibar pe" bar={pct(r.PE?.oi, maxOi.pe)} /><Plain v={r.PE?.iv} f={(n) => `${num(n, 1)}%`} itm={peItm && !atm} cls="iv" />
                    <LtpCell leg={r.PE} opt="PE" strike={r.strike} chain={chain} live={live} canTrade={canTrade} onAdd={onAdd} itm={peItm && !atm} />
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>
      {chain && chain.mode !== 'live' && <p className="sr" hidden>{dmy(chain.date)}</p>}
    </div>
  )
}
