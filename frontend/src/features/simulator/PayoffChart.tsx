import { useEffect, useRef, useState, type MouseEvent } from 'react'
import { dmy, lakh, money, n0 } from '../../lib/format'
import { daysBetween, kfmt, type Chain, type Mark, type SimPos, type WhatIf } from './sim'

function useSize<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  const [size, setSize] = useState({ w: 600, h: 300 })
  useEffect(() => {
    if (!ref.current) return
    const ro = new ResizeObserver(() => { if (ref.current) setSize({ w: ref.current.clientWidth, h: ref.current.clientHeight }) })
    ro.observe(ref.current)
    setSize({ w: ref.current.clientWidth, h: ref.current.clientHeight })
    return () => ro.disconnect()
  }, [])
  return { ref, ...size }
}

interface Props { view: 'payoff' | 'oi'; chain: Chain; mark: Mark | null; wi: WhatIf | null; positions: SimPos[]; date: string; curExp: string | null }

/** Expiry payoff (exact, from the open legs) with the target-date Black-Scholes estimate, break-evens, max profit / loss and ±σ bands; or open interest per strike. */
export function PayoffChart(props: Props) {
  const { ref, w, h } = useSize<HTMLDivElement>()
  return (
    <div className="chartbox" ref={ref}>
      {props.view === 'oi' ? <OiChart {...props} w={w} h={h} /> : <Payoff {...props} w={w} h={h} />}
    </div>
  )
}

function Payoff({ chain, mark, wi, positions, date, curExp, w: W, h: H }: Props & { w: number; h: number }) {
  const [tip, setTip] = useState<{ x: number; y: number; html: { at: number; pnl: number; target?: number } } | null>(null)
  const [hx, setHx] = useState<number | null>(null)
  const p = mark?.payoff
  const spot = mark?.underlying ?? chain.underlying
  if (!p) return <svg width={W} height={H}><text x={W / 2} y={H / 2} textAnchor="middle" className="ct">No open positions. Click B / S in the option chain to simulate a trade.</text></svg>

  const ks = positions.filter((q) => !q.closed).map((q) => q.strike)
  const keys = [...ks, ...p.breakevens, spot]
  const span = Math.max(spot * 0.012, (Math.max(...keys) - Math.min(...keys)) * 0.35)
  let lo = Math.min(...keys) - span, hi = Math.max(...keys) + span
  if (wi?.what_spot && wi.spot_shift_pct) { lo = Math.min(lo, wi.what_spot - span * 0.3); hi = Math.max(hi, wi.what_spot + span * 0.3) }
  let curve = p.curve.filter((c) => c[0] >= lo && c[0] <= hi)
  if (curve.length < 20) curve = p.curve
  const tcurve = wi?.curve ? wi.curve.filter((c) => c[0] >= curve[0][0] && c[0] <= curve[curve.length - 1][0]) : []
  const L = 58, R = 16, T = 30, B = 34
  const xs = curve.map((c) => c[0]), ys = curve.map((c) => c[1]), tys = tcurve.map((c) => c[1])
  const x0 = xs[0], x1 = xs[xs.length - 1]
  let y0 = Math.min(0, ...ys, ...tys), y1 = Math.max(0, ...ys, ...tys)
  const pad = (y1 - y0 || 1) * 0.1; y0 -= pad; y1 += pad
  const X = (x: number) => L + ((x - x0) / (x1 - x0)) * (W - L - R)
  const Y = (y: number) => T + ((y1 - y) / (y1 - y0)) * (H - T - B)
  const zero = Y(0)
  const pts = curve.map((c) => `${X(c[0]).toFixed(1)},${Y(c[1]).toFixed(1)}`).join(' ')
  const poly = `${X(x0)},${zero} ${pts} ${X(x1)},${zero}`
  const iv = chain.atm_iv
  const days = Math.max(0.25, daysBetween(date, curExp ?? chain.expiry))
  const mi = ys.indexOf(Math.max(...ys)), li = ys.indexOf(Math.min(...ys))

  const move = (e: MouseEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect(), mx = e.clientX - r.left
    if (mx < L || mx > W - R) return
    const x = x0 + ((mx - L) / (W - L - R)) * (x1 - x0)
    const c = curve.reduce((a, b) => (Math.abs(b[0] - x) < Math.abs(a[0] - x) ? b : a))
    const tc = tcurve.find((q) => q[0] === c[0])
    setHx(X(c[0]))
    setTip({ x: Math.min(W - 150, mx + 12), y: Math.max(0, Y(c[1]) - 30), html: { at: c[0], pnl: c[1], target: tc?.[1] } })
  }
  return (
    <div style={{ position: 'relative' }}>
      <svg width={W} height={H} onMouseMove={move} onMouseLeave={() => { setTip(null); setHx(null) }} className="payoff">
        <defs>
          <clipPath id="cu"><rect x="0" y="0" width={W} height={Math.max(0, zero)} /></clipPath>
          <clipPath id="cd"><rect x="0" y={zero} width={W} height={Math.max(0, H - zero)} /></clipPath>
        </defs>
        {[0, 1, 2, 3, 4].map((j) => { const v = y0 + ((y1 - y0) * j) / 4; return <g key={j}><line x1={L} x2={W - R} y1={Y(v)} y2={Y(v)} className="gridline" /><text x={L - 6} y={Y(v) + 3} textAnchor="end" className="ct">{kfmt(v)}</text></g> })}
        {[0, 1, 2, 3, 4, 5, 6].map((j) => { const x = x0 + ((x1 - x0) * j) / 6; return <text key={j} x={X(x)} y={H - 12} textAnchor="middle" className="ct">{n0(x)}</text> })}
        <text x={W / 2} y={H - 1} textAnchor="middle" className="ct">Underlying at expiry</text>
        {iv ? [-2, -1, 1, 2].map((k) => { const x = spot * (1 + ((k * iv) / 100) * Math.sqrt(days / 365)); return x > x0 && x < x1 ? <g key={k}><line x1={X(x)} x2={X(x)} y1={T} y2={H - B} className="sigma" /><text x={X(x)} y={H - B - 28} textAnchor="middle" className="ct">{k > 0 ? '+' : '−'}{Math.abs(k)}σ</text></g> : null }) : null}
        <polygon points={poly} clipPath="url(#cu)" className="fillpos" />
        <polygon points={poly} clipPath="url(#cd)" className="fillneg" />
        <line x1={L} x2={W - R} y1={zero} y2={zero} className="zeroline" />
        <polyline points={pts} className="expiryline" />
        {tcurve.length > 0 && <polyline points={tcurve.map((c) => `${X(c[0]).toFixed(1)},${Y(c[1]).toFixed(1)}`).join(' ')} className="targetline" />}
        {p.breakevens.filter((b) => b > x0 && b < x1).map((b, i) => (
          <g key={b}>
            <line x1={X(b)} x2={X(b)} y1={T} y2={H - B} className="beline" /><circle cx={X(b)} cy={zero} r="4" className="bedot" />
            <text x={X(b)} y={T + 10 + (i % 2) * 26} textAnchor="middle" className="betext">Break-even</text><text x={X(b)} y={T + 22 + (i % 2) * 26} textAnchor="middle" className="betext">{n0(b)}</text>
          </g>
        ))}
        {p.max_profit != null && p.max_profit > 0 && <g><circle cx={X(xs[mi])} cy={Y(ys[mi])} r="4.5" className="mpdot" /><text x={X(xs[mi])} y={Y(ys[mi]) - 9} textAnchor="middle" className="mptext">Max profit ₹{n0(p.max_profit)}</text></g>}
        {p.max_loss != null && p.max_loss < 0 && <g><circle cx={X(xs[li])} cy={Y(ys[li])} r="4.5" className="bedot" /><text x={X(xs[li])} y={Y(ys[li]) + 16} textAnchor="middle" className="betext">Max loss ₹{n0(-p.max_loss)}</text></g>}
        {p.max_loss == null && <text x={W - R - 4} y={H - B - 6} textAnchor="end" className="betext">Max loss: unlimited</text>}
        <line x1={X(spot)} x2={X(spot)} y1={T} y2={H - B} className="spotline" />
        <rect x={X(spot) - 44} y={H - B - 22} width="88" height="18" rx="4" className="spotbox" /><text x={X(spot)} y={H - B - 9} textAnchor="middle" className="spottext">Spot {n0(spot)}</text>
        {wi?.what_spot && wi.spot_shift_pct && wi.what_spot > x0 && wi.what_spot < x1 && (() => {
          const wx = X(wi.what_spot), wy = Y(wi.pnl_at_what_spot ?? 0)
          return <g><line x1={wx} x2={wx} y1={T} y2={H - B} className="wiline" /><circle cx={wx} cy={wy} r="4.5" className="widot" /><text x={wx} y={wy - 9} textAnchor="middle" className="witext">What-if {n0(wi.what_spot)}: ₹{n0(wi.pnl_at_what_spot)}</text></g>
        })()}
        {hx != null && <line x1={hx} x2={hx} y1={T} y2={H - B} className="crosshair" />}
      </svg>
      {tip && (
        <div className="simtip" style={{ left: tip.x, top: tip.y }}>
          At {n0(tip.html.at)}<br />Expiry: <b className={tip.html.pnl >= 0 ? 'pos' : 'neg'}>{money(tip.html.pnl)}</b>
          {tip.html.target != null && <><br /><span style={{ color: 'var(--purple)' }}>Target {dmy(wi?.target_date ?? date)} (est.)</span>: <b className={tip.html.target >= 0 ? 'pos' : 'neg'}>{money(tip.html.target)}</b></>}
        </div>
      )}
    </div>
  )
}

function OiChart({ chain: c, w: W, h: H }: Props & { w: number; h: number }) {
  const rows = c.rows.filter((r) => Math.abs(r.strike - c.underlying) <= c.underlying * 0.04)
  if (!rows.length) return <svg width={W} height={H}><text x={W / 2} y={H / 2} textAnchor="middle" className="ct">No strikes near spot</text></svg>
  const L = 52, R = 12, T = 20, B = 34
  const max = Math.max(1, ...rows.flatMap((r) => [r.CE?.oi || 0, r.PE?.oi || 0]))
  const bw = (W - L - R) / rows.length
  const Y = (v: number) => T + (1 - v / max) * (H - T - B)
  const si = rows.findIndex((r) => r.strike >= c.underlying)
  const sx = L + (si < 0 ? rows.length : si) * bw
  return (
    <svg width={W} height={H}>
      {[0, 1, 2, 3, 4].map((j) => { const v = (max * j) / 4; return <g key={j}><line x1={L} x2={W - R} y1={Y(v)} y2={Y(v)} className="gridline" /><text x={L - 6} y={Y(v) + 3} textAnchor="end" className="ct">{lakh(v)}</text></g> })}
      {rows.map((r, i) => {
        const x = L + i * bw, w = Math.max(1, bw * 0.38)
        return (
          <g key={r.strike}>
            <rect x={x + bw * 0.1} y={Y(r.CE?.oi || 0)} width={w} height={H - B - Y(r.CE?.oi || 0)} className="oice"><title>{n0(r.strike)} CE OI {n0(r.CE?.oi)}</title></rect>
            <rect x={x + bw * 0.1 + w} y={Y(r.PE?.oi || 0)} width={w} height={H - B - Y(r.PE?.oi || 0)} className="oipe"><title>{n0(r.strike)} PE OI {n0(r.PE?.oi)}</title></rect>
            {i % Math.ceil(rows.length / 10) === 0 && <text x={x + bw / 2} y={H - 14} textAnchor="middle" className="ct">{n0(r.strike)}</text>}
          </g>
        )
      })}
      <line x1={sx} x2={sx} y1={T} y2={H - B} className="spotline" /><text x={sx + 4} y={T + 10} className="ct" style={{ fill: 'var(--text-primary)' }}>Spot {n0(c.underlying)}</text>
      <rect x={W - R - 150} y="2" width="9" height="9" className="oice" /><text x={W - R - 137} y="10" className="ct">Call OI</text><rect x={W - R - 85} y="2" width="9" height="9" className="oipe" /><text x={W - R - 72} y="10" className="ct">Put OI</text>
    </svg>
  )
}
