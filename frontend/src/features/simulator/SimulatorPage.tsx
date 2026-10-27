import { useEffect, useState } from 'react'
import { useDataSource } from '../../lib/datasource'
import { dmy, n0, num } from '../../lib/format'
import { ChainPanel } from './ChainPanel'
import { PositionsPanel } from './PositionsPanel'
import { RiskPanel } from './RiskPanel'
import { TimeBar } from './TimeBar'
import { useSimulator } from './useSimulator'
import type { Sym } from './sim'
import './simulator.css'

export default function SimulatorPage() {
  const ds = useDataSource()
  const s = useSimulator(ds)
  const [strikeView, setStrikeView] = useState<'near' | 'all'>('near')
  const [onlyTraded, setOnlyTraded] = useState(false)
  const c = s.chain

  useEffect(() => {          // arrow keys step through time like the old page
    const on = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).matches('input,select,textarea') || s.live) return
      if (s.useIntra && !e.shiftKey && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) { s.goMinute(s.mins.indexOf(s.tm ?? '') + (e.key === 'ArrowLeft' ? -1 : 1)); return }
      if (e.key === 'ArrowLeft') s.goIndex(s.idx - 1)
      if (e.key === 'ArrowRight') s.goIndex(s.idx + 1)
    }
    window.addEventListener('keydown', on)
    return () => window.removeEventListener('keydown', on)
  })

  const isLive = c?.mode === 'live'
  const asof = !c ? '–' : isLive ? `LIVE ${c.time} · ${c.live_legs} live legs` : c.mode === 'intraday' ? `As of ${dmy(c.date)} ${c.time} (1-min)` : `As of ${dmy(c.date)} close`
  const dataBadge = isLive ? (c?.feed_connected ? 'LIVE ticks' : 'feed offline') : s.useIntra ? '1-min data' : 'EOD data'
  const chg = isLive ? c?.spot_change ?? null : c && s.prevSpot != null ? c.underlying - s.prevSpot : null
  const chgPct = isLive ? c?.spot_pct ?? null : c && s.prevSpot ? ((c.underlying - s.prevSpot) / s.prevSpot) * 100 : null

  return (
    <div className="page simpage">
      <div className="titlebar">
        <div className="tt"><h1>Options Strategy Simulator</h1><p className="sub">Analyse historical index option data (NIFTY, BANKNIFTY), test positions and understand the risk. Simulation only, no broker orders.</p></div>
        <div className="ctrl">
          <label className="field">Index<select value={s.sym} onChange={(e) => s.setSym(e.target.value as Sym)}><option>NIFTY</option><option>BANKNIFTY</option></select></label>
          <label className="field">Expiry<select value={s.curExp ?? ''} onChange={(e) => s.pickExpiry(e.target.value)} style={{ width: 210 }}>
            {c?.expiries.map((e) => <option key={e} value={e}>{dmy(e)} (DTE {Math.round((new Date(e).getTime() - new Date(c.date).getTime()) / 864e5)})</option>)}</select></label>
          <label className="field">Historical date
            <span className="row" style={{ gap: 6, alignItems: 'center' }}>
              <input type="date" value={s.date} min={s.dates[0]} max={s.dates[s.dates.length - 1]} disabled={s.live} onChange={(e) => { const i = s.dates.indexOf(e.target.value); if (i >= 0) s.goIndex(i) }} />
              <button className="ib" disabled={s.idx <= 0 || s.live} onClick={() => s.goIndex(s.idx - 1)} title="Previous trading day">‹</button>
              <button className="ib" disabled={s.idx >= s.dates.length - 1 || s.live} onClick={() => s.goIndex(s.idx + 1)} title="Next trading day">›</button>
            </span></label>
          <label className="field">Lot<span className="lotv">{c?.lot_size ?? '–'}</span></label>
          <button className={'livebtn' + (s.live ? ' on' : '')} onClick={() => s.setLive(!s.live)} title="Live chain from Angel One WebSocket ticks (paper only, no orders). Updates every second."><i />LIVE</button>
          <span className={'eod' + (isLive ? ' livechip' : '')}>{dataBadge}</span>
        </div>
      </div>

      <div className="stats">
        <div className="stat"><div className="k">{isLive ? 'Index (live)' : 'Index close'}</div><div className="v">{c ? num(c.underlying) : '–'}</div>
          <div className="s">{chg == null ? ' ' : <span className={chg >= 0 ? 'pos' : 'neg'}>{chg >= 0 ? '▲ +' : '▼ '}{num(chg)} ({chgPct?.toFixed(2)}%)</span>} {chg != null ? 'vs prev. close' : ''}</div></div>
        <div className="stat"><div className="k">PCR (OI)</div><div className="v">{c?.pcr ?? '–'}</div><div className="s">{c?.mode === 'eod' ? 'Put OI / Call OI, this expiry' : c?.oi_day ? `OI of ${dmy(c.oi_day)} EOD (no OI in ${isLive ? 'live ticks' : '1-min data'})` : 'no OI available'}</div></div>
        <div className="stat"><div className="k">ATM IV <span className="chip muted">calc</span></div><div className="v">{c?.atm_iv ? `${c.atm_iv}%` : 'n/a'}</div><div className="s">Black-Scholes estimate, avg CE/PE</div></div>
        <div className="stat"><div className="k">Max pain</div><div className="v">{c ? n0(c.max_pain) : '–'}</div><div className="s">{isLive ? 'not computed in live mode' : c?.mode === 'intraday' ? 'from previous EOD OI' : "from this expiry's OI"}</div></div>
        <div className="stat"><div className="k">ATM straddle</div><div className="v">{c?.straddle == null ? 'n/a' : `₹${num(c.straddle)}`}</div><div className="s">{c ? `ATM ${n0(c.atm)} CE + PE ${c.mode === 'eod' ? 'close' : 'last trade'}` : ' '}</div></div>
      </div>

      {s.initError && <div className="alert">{s.initError}</div>}
      <div className="simmain">
        <ChainPanel chain={c} error={s.chainError} loading={s.loading} live={s.live} canTrade={!s.readOnly} onAdd={s.addPos}
          strikeView={strikeView} setStrikeView={setStrikeView} onlyTraded={onlyTraded} setOnlyTraded={setOnlyTraded}
          title={`Option chain · ${s.sym}${c ? ' · ' + dmy(c.expiry) : ''}`} asof={asof} atmKey={isLive ? 'live' : `${s.date}|${s.tm}|${c?.expiry}`} />
        <div className="rcol">
          <PositionsPanel chain={c} positions={s.positions} valued={s.valued} readOnly={s.readOnly} live={s.live} date={s.date} useIntra={s.useIntra} tm={s.tm} mult={s.mult}
            setMult={s.setMult} onPreset={(n) => void s.placePreset(n)} onLots={s.changeLots} onExit={s.exitPos} onRemove={s.removePos}
            onExitAll={s.exitAll} onClear={s.clearAll} onExport={s.exportCsv} />
          <RiskPanel s={s} />
        </div>
      </div>
      <TimeBar s={s} />
    </div>
  )
}
