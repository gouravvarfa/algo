import { useState } from 'react'
import { dmy, money, n0, num } from '../../lib/format'
import { PayoffChart } from './PayoffChart'
import type { Sim } from './useSimulator'

const rupee = (v: number | null | undefined) => (v == null ? '–' : <span className={v >= 0 ? 'pos' : 'neg'}>{money(Math.round(v) || 0)}</span>)
const sg = (v: number) => <span className={v >= 0 ? 'pos' : 'neg'}>{v >= 0 ? '+' : '−'}{num(Math.abs(v), 2)}</span>
const SRC: Record<string, string> = { own: "own IV (from this leg's close)", atm_fallback: 'ATM IV fallback', none: 'no IV: held at last price', settled: 'expired: settlement value' }

/** The right-hand risk column: payoff / OI chart, max profit & loss, break-evens, capital / margin, return on capital and the Black-Scholes what-if. */
export function RiskPanel({ s }: { s: Sim }) {
  const [view, setView] = useState<'payoff' | 'oi'>('payoff')
  const [adv, setAdv] = useState(false)
  const open = s.positions.filter((p) => !p.closed)
  const real = s.positions.filter((p) => p.closed).reduce((a, p) => a + (p.realized ?? 0), 0)
  const unreal = open.reduce((a, p) => a + (s.valued[String(p.id)]?.pnl ?? 0), 0)
  const m = open.length ? s.margin : null
  const p = s.mark?.payoff
  const wi = s.wi
  const mgText = !m ? (open.length ? 'loading…' : 'no open legs')
    : m.margin == null ? (m.note ?? '') + (m.unlisted.length ? ` (${m.unlisted.join(', ')})` : '')
      : m.has_sell ? `Angel One · today${m.hedge_benefit ? ` · hedge benefit ₹${num(m.hedge_benefit)}` : ''} · premium recv ₹${num(m.premium_received)}` : 'premium paid'
  const rr = p && p.max_profit != null && p.max_loss != null && p.max_loss < 0 && p.max_profit > 0 ? `1 : ${(p.max_profit / -p.max_loss).toFixed(2)}` : p ? 'n/a (unlimited side)' : '–'
  return (
    <div className="card">
      <div className="card-head">
        <h2>Strategy payoff</h2><span className="spacer" />
        <span className="plegend"><span><i className="pl pos" />Profit</span><span><i className="pl neg" />Loss</span><span><i className="pl acc" />Expiry</span>{wi?.curve && <span><i className="pl pur" />Target date (est.)</span>}<span><i className="pl ink" />Spot</span></span>
        <span className="seg"><button className={view === 'payoff' ? 'on' : ''} onClick={() => setView('payoff')}>Payoff</button><button className={view === 'oi' ? 'on' : ''} onClick={() => setView('oi')}>OI</button></span>
      </div>
      {s.chain
        ? <PayoffChart view={view} chain={s.chain} mark={s.mark} wi={wi} positions={s.positions} date={s.date} curExp={s.curExp} />
        : <div className="chartbox"><div className="empty">No chain loaded.</div></div>}
      <div className="rk">
        <div><span className="ic2 pos">↑</span><div><div className="k">Max profit</div><b>{!p ? '–' : p.max_profit == null ? <span className="pos">Unlimited</span> : rupee(p.max_profit)}</b></div></div>
        <div><span className="ic2 neg">↓</span><div><div className="k">Max loss</div><b>{!p ? '–' : p.max_loss == null ? <span className="neg">Unlimited</span> : rupee(p.max_loss)}</b></div></div>
        <div><span className="ic2 pur">◎</span><div><div className="k">Breakeven</div><b>{!p ? '–' : p.breakevens.length ? p.breakevens.map(n0).join(' / ') : 'none'}</b></div></div>
        <div title="Money blocked by the open legs. BUY legs: premium paid. With SELL legs: Angel One's margin calculator (SPAN + exposure, hedge benefit included), today's margin, listed contracts only.">
          <span className="ic2 acc">₹</span><div style={{ minWidth: 0 }}><div className="k">Capital / margin</div><b>{!m ? '–' : m.margin != null ? `₹${num(m.margin)}` : <span className="sub">n/a</span>}</b><div className="mg-s" title={mgText}>{mgText}</div></div>
        </div>
      </div>
      <div className="rk2">
        <span>{p && p.net_credit < 0 ? 'Net debit' : 'Net credit'}: <b>{p ? rupee(Math.abs(p.net_credit)) : '–'}</b></span>
        <span>Risk / reward: <b>{rr}</b></span>
        <span>Realized: <b>{s.positions.some((x) => x.closed) ? rupee(real) : '–'}</b></span>
        <span>Return on capital: <b>{m && m.margin ? <span className={unreal + real >= 0 ? 'pos' : 'neg'}>{(((unreal + real) / m.margin) * 100).toFixed(2)}%</span> : '–'}</b></span>
      </div>
      <p className="note">{view === 'oi' ? `Open interest per strike for ${s.chain ? dmy(s.chain.expiry) : ''}${s.chain?.mode === 'intraday' || s.chain?.mode === 'live' ? ` (Bhavcopy OI of ${s.chain.oi_day ?? 'the previous day'})` : ' (NSE Bhavcopy)'}.`
        : p ? "Solid line: P&L at expiry of the open legs (exact). Dashed purple: theoretical P&L on the target date (Black-Scholes estimate, see Advanced). ±σ bands use the calculated ATM IV." : ''}</p>

      <div className="adv">
        <button className="advhead" onClick={() => setAdv(!adv)} aria-expanded={adv}><span>{adv ? '▾' : '▸'}</span> Advanced: target date, IV &amp; spot what-if, Greeks <span className="chip muted">Black-Scholes estimate</span></button>
        {adv && (
          <div className="ab">
            <div className="row">
              <label className="field">Target date<input type="date" value={s.wiDate} min={s.date} max={s.minOpenExpiry ?? s.date} onChange={(e) => s.setWiDate(e.target.value)} /></label>
              <label className="field">Change IV: <b>{s.wiIv >= 0 ? '+' : ''}{s.wiIv.toFixed(1)} pts</b><input type="range" min={-10} max={10} step={0.5} value={s.wiIv} onChange={(e) => s.setWiIv(Number(e.target.value))} /></label>
              <label className="field">Change spot: <b>{s.wiSp >= 0 ? '+' : ''}{s.wiSp.toFixed(2)}%</b><input type="range" min={-5} max={5} step={0.25} value={s.wiSp} onChange={(e) => s.setWiSp(Number(e.target.value))} /></label>
              <button className="btn sm" onClick={() => { s.setWiIv(0); s.setWiSp(0); s.setWiDate(s.date) }}>Reset what-if</button>
            </div>
            {wi?.greeks ? (
              <>
                <p className="sub" style={{ margin: '8px 0' }}>Target {dmy(wi.target_date!)} · spot {n0(wi.what_spot)}{wi.spot_shift_pct ? ` (${wi.spot_shift_pct > 0 ? '+' : ''}${wi.spot_shift_pct}%)` : ''} · est. P&amp;L {rupee(wi.pnl_at_what_spot)}</p>
                <div className="gk">
                  <div><div className="k">Net Δ delta</div><b>{sg(wi.greeks.delta)}</b><span className="u">₹ per index point</span></div>
                  <div><div className="k">Net Γ gamma</div><b>{sg(wi.greeks.gamma)}</b><span className="u">Δ change per point</span></div>
                  <div><div className="k">Net Θ theta</div><b>{sg(wi.greeks.theta)}</b><span className="u">₹ per calendar day</span></div>
                  <div><div className="k">Net ν vega</div><b>{sg(wi.greeks.vega)}</b><span className="u">₹ per 1 IV point</span></div>
                </div>
                <div className="wilegs">{open.map((x, i) => { const l = wi.legs?.[i]; return <div key={String(x.id)}>{x.side} {n0(x.strike)} {x.opt} · {l?.iv == null ? '–' : `IV ${num(l.iv, 1)}%${l.iv_used !== l.iv ? ` → ${num(l.iv_used, 1)}%` : ''}`} · <span className="sub">{SRC[l?.iv_source ?? ''] ?? ''}</span></div> })}</div>
                {wi.warnings && wi.warnings.length > 0 && <p className="sub warn">⚠ {wi.warnings.join(' · ')}</p>}
              </>
            ) : <p className="sub" style={{ margin: '8px 0' }}>{wi?.error ? `What-if unavailable: ${wi.error}` : open.length ? '' : 'Add a position to see the target-date line and Greeks.'}</p>}
            <p className="note">Theoretical estimates from the selected date&apos;s real EOD closes. Not market data, and not used for P&amp;L above.</p>
          </div>
        )}
      </div>
    </div>
  )
}
