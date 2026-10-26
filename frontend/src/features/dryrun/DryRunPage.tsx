import { useQuery } from '@tanstack/react-query'
import { Fragment, useMemo, useState } from 'react'
import { get } from '../../lib/api'
import { n2 } from '../../lib/format'

interface Order { call: string; why: string; payload?: Record<string, string> }
interface Row {
  at: string; symbol: string; contract: string | null; event: string; direction: string; quantity: number
  strategy_price: number | null; ltp_at_signal: number | null; slippage_points: number | null; orders: Order[]
}

/** Dry run: the Angel One orders a real account WOULD have sent for each Top-Bottom signal (exit first, then the new entry),
 * with the live price at that moment. Nothing is ever sent to the broker. With `userId` an admin sees another person's, read-only. */
export default function DryRunPage({ userId }: { userId?: string }) {
  const url = userId ? `/api/admin/users/${userId}/dry-run` : '/api/dry-run'
  const q = useQuery({ queryKey: ['dryrun', userId ?? 'me'], queryFn: () => get<Row[]>(url), refetchInterval: 20_000 })
  const [open, setOpen] = useState<number | null>(null)
  const rows = q.data ?? []
  const slips = useMemo(() => rows.map((r) => r.slippage_points).filter((x): x is number => x != null), [rows])
  const avg = slips.length ? slips.reduce((a, b) => a + b, 0) / slips.length : null

  return (
    <div className="page">
      <div className="row" style={{ alignItems: 'center' }}>
        <h1>Dry run</h1><span className="chip warn">no orders are sent</span>{userId && <span className="chip warn">read-only</span>}
      </div>
      <p className="sub">For every live Top-Bottom signal: the exact Angel One orders a real account would send (the stop order already on the exchange closes the position, then the new entry goes as a LIMIT order with a small allowance, then its stop order), and the live price at that moment. Paper trading is not affected.</p>
      <div className="kpis">
        <div className="kpi"><span className="k">Signals recorded</span><b className="num">{rows.length}</b></div>
        <div className="kpi"><span className="k">Average slippage</span><b className="num">{avg == null ? '–' : `${n2(avg)} pts`}</b></div>
        <div className="kpi"><span className="k">Worst slippage</span><b className="num">{slips.length ? `${n2(Math.max(...slips))} pts` : '–'}</b></div>
      </div>
      <div className="card">
        <div className="card-head"><h2>Would-be orders</h2><span className="chip muted">{rows.length}</span></div>
        {q.isLoading ? <div className="skeleton" style={{ height: 120 }} /> : rows.length === 0 ? (
          <p className="empty">Nothing yet. The first live entry or exit from now on appears here with its orders.</p>
        ) : (
          <div className="scroll"><table>
            <thead><tr><th className="l">Time</th><th className="l">Contract</th><th className="l">Event</th><th className="l">Side</th><th>Qty</th><th>Strategy price</th><th>Live price</th><th>Slippage</th><th className="l">Orders</th></tr></thead>
            <tbody>
              {rows.map((r, i) => (
                <Fragment key={i}>
                  <tr>
                    <td className="l">{r.at}</td><td className="l">{r.contract ?? r.symbol}</td><td className="l">{r.event}</td><td className="l">{r.direction}</td>
                    <td className="num">{r.quantity}</td><td className="num">{r.strategy_price == null ? '–' : n2(r.strategy_price)}</td>
                    <td className="num">{r.ltp_at_signal == null ? '–' : n2(r.ltp_at_signal)}</td>
                    <td className={'num ' + (r.slippage_points != null && r.slippage_points > 0 ? 'neg' : '')}>{r.slippage_points == null ? '–' : n2(r.slippage_points)}</td>
                    <td className="l"><button className="btn sm" onClick={() => setOpen(open === i ? null : i)}>{r.orders.length} {open === i ? '▲' : '▼'}</button></td>
                  </tr>
                  {open === i && (
                    <tr><td colSpan={9} className="l">
                      {r.orders.map((o, k) => (
                        <div key={k} style={{ marginBottom: 6 }}>
                          <b>{k + 1}. {o.call}</b> · {o.why}
                          {o.payload && <pre className="num" style={{ whiteSpace: 'pre-wrap', margin: '4px 0 0', fontSize: 12 }}>{JSON.stringify(o.payload, null, 1)}</pre>}
                        </div>
                      ))}
                    </td></tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table></div>
        )}
      </div>
    </div>
  )
}
