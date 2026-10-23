import { useMemo } from 'react'
import { dmy, money, moneyDec, n0, num, sign } from '../../lib/format'
import { SIM_PRESETS, type Chain, type SimPos, type Valued } from './sim'

const rupee = (v: number | null | undefined) => (v == null ? '–' : <span className={v >= 0 ? 'pos' : 'neg'}>{money(Math.round(v) || 0)}</span>)

interface Props {
  chain: Chain | null; positions: SimPos[]; valued: Record<string, Valued>; readOnly: boolean; live: boolean; date: string; useIntra: boolean; tm: string | null; mult: number
  autoSl: number | null; setAutoSl: (v: number | null) => void; setMult: (v: number) => void
  onPreset: (name: string) => void; onLots: (id: SimPos['id'], d: number) => void; onExit: (id: SimPos['id']) => void; onRemove: (id: SimPos['id']) => void
  onExitAll: () => void; onClear: () => void; onExport: () => void
}

export function PositionsPanel(p: Props) {
  const open = p.positions.filter((x) => !x.closed)
  const real = p.positions.filter((x) => x.closed).reduce((s, x) => s + (x.realized ?? 0), 0)
  const unreal = open.reduce((s, x) => s + (p.valued[String(x.id)]?.pnl ?? 0), 0)
  const missing = open.some((x) => p.valued[String(x.id)]?.pnl == null)
  const total = unreal + real
  const rows = useMemo(() => p.positions, [p.positions])
  return (
    <div className="card">
      <div className="card-head">
        <h2>Positions <span className="sub">{open.length} open · {p.positions.length - open.length} closed</span></h2><span className="spacer" />
        {p.readOnly && <span className="chip warn">read-only</span>}
      </div>
      {!p.readOnly && (
        <div className="posctl">
          <label className="inline" title="Adds all legs of a ready-made strategy at this date/time, at real chain prices">Preset
            <select value="" onChange={(e) => { if (e.target.value) p.onPreset(e.target.value) }}>
              <option value="">— add strategy —</option>{Object.keys(SIM_PRESETS).map((k) => <option key={k}>{k}</option>)}
            </select></label>
          <label className="inline" title="Stop-loss put on every leg you add (SELL: entry +x%, BUY: entry −x%). Empty = no stop.">Auto SL %
            <input type="number" min={1} max={500} value={p.autoSl ?? ''} style={{ width: 62 }} onChange={(e) => p.setAutoSl(e.target.value === '' ? null : Number(e.target.value))} /></label>
          <label className="inline" title="Multiplies the lots of every open leg (simulation only)">Lot multiplier
            <input type="number" min={1} max={100} value={p.mult} style={{ width: 62 }} onChange={(e) => p.setMult(Number(e.target.value))} /></label>
          <button className="btn sm" onClick={p.onExitAll}>Exit all</button><button className="btn sm" onClick={p.onClear}>Clear</button><button className="btn sm" onClick={p.onExport} title="Download the simulated positions as CSV">Export CSV</button>
        </div>
      )}
      <div className="scroll">
        <table className="pt">
          <thead><tr><th className="l">Leg</th><th>Lots</th><th>Entry ₹</th><th>{p.live ? 'Live ₹' : 'Now / exit ₹'}</th><th>P&amp;L</th><th /></tr></thead>
          <tbody>
            {rows.map((x) => {
              const v = p.valued[String(x.id)]
              const off = p.chain && x.expiry === p.chain.expiry ? x.strike - p.chain.atm : null
              const warn: string[] = []
              if (!x.closed && (p.date < x.entry_date || (p.date === x.entry_date && p.useIntra && x.entry_time && p.tm && p.tm < x.entry_time))) warn.push('viewing a time before entry')
              if (x.stale_entry) warn.push(`entry price was a trade ${x.stale_entry} min old`)
              if (x.untraded_entry) warn.push('entered on an untraded close')
              if (!x.closed && v?.note && v.note !== 'settled at expiry') warn.push(v.note)
              const note = x.closed ? `${x.exit_reason ? x.exit_reason + ' · ' : 'exited '}${dmy(x.exit_date!)}${x.exit_time ? ' ' + x.exit_time : ''}` : x.sl ? `SL ₹${num(x.sl)} (${x.sl_pct}%)` : ''
              const info = [`exp ${dmy(x.expiry)}`, `in ${dmy(x.entry_date)}${x.entry_time ? ' ' + x.entry_time : ''}`, note, warn.length ? '⚠ ' + warn.join(' · ') : ''].filter(Boolean).join(' · ')
              return (
                <tr key={String(x.id)} className={x.closed ? 'closed' : ''} title={info}>
                  <td className="l">
                    <span className={`badge ${x.side}`}>{x.side}</span> <b>{n0(x.strike)} {x.opt}</b>
                    {off != null && <span className="sub"> {off === 0 ? 'ATM' : (off > 0 ? '+' : '−') + n0(Math.abs(off))}</span>}
                    <div className="sub legnote">{info}</div>
                  </td>
                  <td>{x.closed ? x.lots : p.readOnly ? x.lots : <span className="acts"><button className="ib" title="Fewer lots" onClick={() => p.onLots(x.id, -1)}>−</button><b>{x.lots}{p.mult > 1 && <span className="sub">×{p.mult}</span>}</b><button className="ib" title="More lots" onClick={() => p.onLots(x.id, 1)}>+</button></span>}</td>
                  <td className="num">₹{num(x.entry)}</td>
                  <td className="num"><b>{x.closed ? `₹${num(x.exit)}` : v?.ltp == null ? <span className="sub">n/a</span> : `₹${num(v.ltp)}`}</b></td>
                  <td className="num"><b>{rupee(x.closed ? x.realized : v?.pnl)}</b></td>
                  <td>{!p.readOnly && <span className="acts">{!x.closed && <button className="ib" title="Exit at this price" onClick={() => p.onExit(x.id)}>⇥</button>}<button className="ib" title="Remove" onClick={() => p.onRemove(x.id)}>✕</button></span>}</td>
                </tr>
              )
            })}
            {!rows.length && <tr><td colSpan={6} className="empty">No simulated positions yet. Hover a strike in the option chain and click <b>B</b> or <b>S</b>.</td></tr>}
          </tbody>
        </table>
      </div>
      <div className="ptot">
        <span className="note" style={{ margin: 0 }}>Simulated positions only (paper): valued at the selected date&apos;s close, or at live prices in LIVE mode. No broker orders.</span>
        <span className="spacer" />
        <span>Net P&amp;L <span className="sub">{!p.positions.length ? 'no positions' : (p.live ? 'valued at live prices' : `valued at ${p.date ? dmy(p.date) : ''} close`) + (missing ? ' · some legs have no price (excluded)' : '')}</span></span>
        <span className={`big num ${sign(total)}`}>{p.positions.length ? moneyDec(total).replace(/\.00$/, '') : '₹0'}</span>
      </div>
    </div>
  )
}
