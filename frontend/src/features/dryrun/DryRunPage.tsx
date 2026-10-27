import { useQuery } from '@tanstack/react-query'
import { Fragment, useMemo, useState } from 'react'
import { get } from '../../lib/api'
import { money, n2 } from '../../lib/format'
import '../journal/journal.css'      // the .kpis boxes live here

interface Order { call: string; why: string; payload?: Record<string, string | null> }
interface Row {
  at: string; symbol: string; contract: string | null; event: string; direction: string; quantity: number
  strategy_price: number | null; ltp_at_signal: number | null; slippage_points: number | null; orders: Order[]
  late?: boolean; delay_seconds?: number | null; feed_live?: boolean | null; slippage_valid?: boolean; contract_unresolved?: boolean
}

interface SOrder {
  id: string; role: string; type: string; side: string; qty: number; price: number; trigger: number | null; state: string; placed_at: string
  avg_price: number | null; fill_at: string | null; charges: number; why: string; reject_reason?: string
}
interface STrade { side: string; qty: number; entry_time: string; entry: number; exit_time: string; exit: number; gross: number; charges: number; net: number; exit_by: string; slippage_points: number }
interface Account {
  instrument_id: string; symbol: string; started: string; status: 'FLAT' | 'LONG' | 'SHORT'
  position: { side: string; qty: number; entry_price: number; entry_time: string; contract: string; strategy_entry: number | null; slippage_points: number } | null
  stop: { id: string; trigger: number; price: number; state: string } | null
  paper: { direction: string; entry_price: number; active_stop: number } | null
  divergence: string; flags: string[]; pending_entry: { side: string; strategy_price: number; attempts: number } | null
  summary: { gross: number; charges: number; net: number; closed_trades: number; capital_now: number; paper_net: number | null; missed_entries: number; unrealised: number | null }
  orders: SOrder[]; trades: STrade[]; missed: { at: string; side: string; strategy_price: number; reason: string }[]; events: { at: string; text: string }[]
}
interface Shadow {
  enabled: boolean; symbols: string[]; accounts: Account[]
  config?: { capital: number; margin_check: boolean; latency_s: number; max_entry_slippage: Record<string, number>; entry_buffer_points: Record<string, number>
    retry_after_s: number; max_attempts: number; stop_grace_s: number; through_tick: boolean; stop_basis: string; charges_as_of: string; charges_verified: boolean; assumptions: string[] }
}

const tone = (state: string) => (state === 'COMPLETE' ? 'ok' : state === 'REJECTED' || state === 'EXPIRED' ? 'warn' : 'muted')
const time = (s: string | null) => (s ? s.slice(5, 19) : '–')

function ShadowAccount({ a }: { a: Account }) {
  const s = a.summary
  const p = a.position
  return (
    <div className="card">
      <div className="card-head">
        <h2>BankNifty · shadow account</h2>
        <span className={'chip ' + (a.status === 'FLAT' ? 'muted' : 'warn')}>{a.status}</span>
        {p && <span className="chip muted">{p.qty} @ {n2(p.entry_price)} · {p.contract}</span>}
        <span className="spacer" />
        <span className="sub">since {a.started.slice(0, 16)}</span>
      </div>
      {a.flags.map((f) => <div key={f} className="alert" style={{ margin: '8px 0' }}>{f}</div>)}
      <p className="sub" style={{ margin: '6px 0 10px' }}>
        <b>Paper vs shadow:</b> {a.divergence}.
        {a.paper && <> Paper holds {a.paper.direction} from {n2(a.paper.entry_price)} (stop {n2(a.paper.active_stop)}).</>}
        {a.stop && <> Our stop order: trigger {n2(a.stop.trigger)}, limit {n2(a.stop.price)} ({a.stop.state}).</>}
        {a.pending_entry && <> Entry {a.pending_entry.side} working, attempt {a.pending_entry.attempts}.</>}
      </p>
      <div className="kpis">
        <div className="kpi"><span className="k">Shadow gross P&amp;L</span><b className="num">{money(s.gross)}</b></div>
        <div className="kpi"><span className="k">Charges (to verify)</span><b className="num">{money(s.charges)}</b></div>
        <div className="kpi"><span className="k">Shadow net P&amp;L (closed)</span><b className="num">{money(s.net)}</b></div>
        <div className="kpi"><span className="k">Open position P&amp;L (live price)</span><b className="num">{s.unrealised == null ? '–' : money(s.unrealised)}</b></div>
        <div className="kpi"><span className="k">Paper P&amp;L (strategy prices)</span><b className="num">{s.paper_net == null ? '–' : money(s.paper_net)}</b></div>
        <div className="kpi"><span className="k">Closed trades</span><b className="num">{s.closed_trades}</b></div>
        <div className="kpi"><span className="k">Missed entries</span><b className="num">{s.missed_entries}</b></div>
      </div>

      <details open={a.orders.length > 0}>
        <summary><b>Orders</b> ({a.orders.length})</summary>
        <div className="scroll"><table>
          <thead><tr><th className="l">Id</th><th className="l">Placed</th><th className="l">Role</th><th className="l">Type</th><th className="l">Side</th><th>Qty</th><th>Limit</th><th>Trigger</th><th className="l">State</th><th>Fill</th><th>Charges</th><th className="l">Note</th></tr></thead>
          <tbody>{a.orders.map((o) => (
            <tr key={o.id}>
              <td className="l">{o.id}</td><td className="l">{time(o.placed_at)}</td><td className="l">{o.role}</td><td className="l">{o.type}</td><td className="l">{o.side}</td><td className="num">{o.qty}</td>
              <td className="num">{n2(o.price)}</td><td className="num">{o.trigger == null ? '–' : n2(o.trigger)}</td>
              <td className="l"><span className={'chip ' + tone(o.state)}>{o.state}</span></td>
              <td className="num">{o.avg_price == null ? '–' : n2(o.avg_price)}</td><td className="num">{o.charges ? n2(o.charges) : '–'}</td>
              <td className="l">{o.reject_reason ?? o.why}</td>
            </tr>
          ))}</tbody>
        </table></div>
      </details>

      <details open={a.trades.length > 0}>
        <summary><b>Closed trades</b> ({a.trades.length})</summary>
        <div className="scroll"><table>
          <thead><tr><th className="l">Entered</th><th className="l">Exited</th><th className="l">Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Entry slippage</th><th>Gross</th><th>Charges</th><th>Net</th><th className="l">Closed by</th></tr></thead>
          <tbody>{a.trades.map((t, i) => (
            <tr key={i}>
              <td className="l">{time(t.entry_time)}</td><td className="l">{time(t.exit_time)}</td><td className="l">{t.side}</td><td className="num">{t.qty}</td>
              <td className="num">{n2(t.entry)}</td><td className="num">{n2(t.exit)}</td><td className="num">{n2(t.slippage_points)} pts</td>
              <td className="num">{money(t.gross)}</td><td className="num">{money(t.charges)}</td><td className={'num ' + (t.net >= 0 ? 'pos' : 'neg')}>{money(t.net)}</td><td className="l">{t.exit_by}</td>
            </tr>
          ))}</tbody>
        </table></div>
      </details>

      {a.missed.length > 0 && (
        <details open>
          <summary><b>Missed entries</b> ({a.missed.length})</summary>
          <ul className="sub">{a.missed.map((m, i) => <li key={i}>{time(m.at)} · {m.side} at the strategy price {n2(m.strategy_price)}: {m.reason}</li>)}</ul>
        </details>
      )}

      <details>
        <summary><b>Event log</b> ({a.events.length})</summary>
        <ul className="sub" style={{ maxHeight: 260, overflowY: 'auto' }}>{a.events.map((e, i) => <li key={i}>{time(e.at)} · {e.text}</li>)}</ul>
      </details>
    </div>
  )
}

function ShadowSection({ userId }: { userId?: string }) {
  const url = userId ? `/api/admin/users/${userId}/shadow` : '/api/shadow'
  const q = useQuery({ queryKey: ['shadow', userId ?? 'me'], queryFn: () => get<Shadow>(url), refetchInterval: 5_000 })
  const c = q.data?.config
  return (
    <>
      <div className="card">
        <div className="card-head"><h2>Shadow account</h2><span className="chip warn">BankNifty only</span><span className="chip muted">simulation, no broker</span></div>
        <p className="sub">
          It follows the Top-Bottom signals like a real Angel One account: exit first, then the new entry (method B), with its own orders, fills, stop, margin and charges.
          Every fill comes from the live ticks that arrive after the order, never from the strategy's price. So it can differ from the paper position (a missed entry, a stop that does not fill in a gap, a margin shortfall).
        </p>
        {c && (
          <ul className="sub">
            <li>Entry: a LIMIT order at the live price ±{n2(c.entry_buffer_points.BANKNIFTY ?? 2)} points, never more than {n2(c.max_entry_slippage.BANKNIFTY ?? 30)} points past the strategy's trigger (no chasing); not filled in {c.retry_after_s} s: re-priced, at most {c.max_attempts} attempts.</li>
            <li>Latency {c.latency_s} s each way · a LIMIT fills only when a tick trades {c.through_tick ? 'strictly past' : 'at or past'} its price · stop basis: {c.stop_basis === 'REAL_FILL' ? 'the 1% entry-day stop is taken from the real fill' : "the strategy's price"}.</li>
            <li>Capital {money(c.capital)} · margin check <b>{c.margin_check ? 'ON' : 'OFF (margin per lot not set, so no entry is rejected for margin)'}</b> · charges rates as of {c.charges_as_of}{c.charges_verified ? '' : ' (NOT verified)'}.</li>
            {c.assumptions.map((x) => <li key={x}>{x}</li>)}
          </ul>
        )}
      </div>
      {q.isLoading ? <div className="skeleton" style={{ height: 120 }} /> : (q.data?.accounts ?? []).length === 0 ? (
        <div className="card"><p className="empty">No BankNifty strategy has produced a signal for this account yet. The shadow account starts with the first live signal after it was switched on.</p></div>
      ) : q.data!.accounts.map((a) => <ShadowAccount key={a.instrument_id} a={a} />)}
    </>
  )
}

/** Dry run: the Angel One orders a real account WOULD send for each live Top-Bottom signal, and a shadow account that simulates what would really happen
 * to them. Nothing is ever sent to the broker. With `userId` an admin sees another person's, read-only. */
export default function DryRunPage({ userId }: { userId?: string }) {
  const url = userId ? `/api/admin/users/${userId}/dry-run` : '/api/dry-run'
  const q = useQuery({ queryKey: ['dryrun', userId ?? 'me'], queryFn: () => get<Row[]>(url), refetchInterval: 20_000 })
  const [open, setOpen] = useState<number | null>(null)
  const rows = q.data ?? []
  const slips = useMemo(() => rows.filter((r) => r.slippage_valid === true).map((r) => r.slippage_points).filter((x): x is number => x != null), [rows])
  const avg = slips.length ? slips.reduce((a, b) => a + b, 0) / slips.length : null

  return (
    <div className="page">
      <div className="row" style={{ alignItems: 'center' }}>
        <h1>Dry run</h1><span className="chip warn">no orders are sent</span>{userId && <span className="chip warn">read-only</span>}
      </div>
      <p className="sub">Paper trading only. The first part simulates a real account step by step; the second lists the exact Angel One orders that would be sent for every signal.</p>

      <ShadowSection userId={userId} />

      <div className="card">
        <div className="card-head"><h2>Would-be orders</h2><span className="chip muted">{rows.length}</span></div>
        <p className="sub">The orders themselves (LIMIT and STOPLOSS_LIMIT only, in points). Slippage here is the live price when the signal was noticed against the strategy's price; a signal noticed more than 2 minutes late, or priced while the tick feed was not live, is marked and left out of the numbers.</p>
        <div className="kpis">
          <div className="kpi"><span className="k">Signals recorded</span><b className="num">{rows.length}</b></div>
          <div className="kpi"><span className="k">Average slippage (valid rows)</span><b className="num">{avg == null ? '–' : `${n2(avg)} pts`}</b></div>
          <div className="kpi"><span className="k">Worst slippage (valid rows)</span><b className="num">{slips.length ? `${n2(Math.max(...slips))} pts` : '–'}</b></div>
          <div className="kpi"><span className="k">Left out (late / not live)</span><b className="num">{rows.filter((r) => r.slippage_points != null && r.slippage_valid !== true).length}</b></div>
        </div>
        {q.isLoading ? <div className="skeleton" style={{ height: 120 }} /> : rows.length === 0 ? (
          <p className="empty">Nothing yet. The first live entry or exit from now on appears here with its orders.</p>
        ) : (
          <div className="scroll"><table>
            <thead><tr><th className="l">Time</th><th className="l">Contract</th><th className="l">Event</th><th className="l">Side</th><th>Qty</th><th>Strategy price</th><th>Live price</th><th>Slippage</th><th className="l">Orders</th></tr></thead>
            <tbody>
              {rows.map((r, i) => (
                <Fragment key={i}>
                  <tr>
                    <td className="l">{r.at}</td><td className="l">{r.contract ?? r.symbol}{r.contract_unresolved && <span className="chip warn" title="the old contract's token is unknown, so its orders carry no token"> contract?</span>}</td>
                    <td className="l">{r.event}</td><td className="l">{r.direction}</td>
                    <td className="num">{r.quantity}</td><td className="num">{r.strategy_price == null ? '–' : n2(r.strategy_price)}</td>
                    <td className="num">{r.ltp_at_signal == null ? '–' : n2(r.ltp_at_signal)}</td>
                    <td className={'num ' + (r.slippage_valid && r.slippage_points != null && r.slippage_points > 0 ? 'neg' : '')}>
                      {r.slippage_points == null ? '–' : n2(r.slippage_points)}
                      {r.late && <span className="chip warn" title={`noticed ${Math.round(r.delay_seconds ?? 0)} s after its candle closed`}> late</span>}
                      {!r.late && r.slippage_points != null && r.feed_live !== true && <span className="chip warn" title="the live tick feed was not connected, the price may be old"> not live</span>}
                    </td>
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
