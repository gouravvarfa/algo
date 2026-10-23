import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { get, post } from '../../lib/api'
import { useToast } from '../../lib/toast'
import { statusClass } from './HealthPage'
import './developer.css'

interface Circuit { name: string; state: string; forced: string | null; failures_in_window: number; threshold: number; cooldown_s: number; retry_in_s: number | null; opened_count: number; total_calls: number; total_failures: number; last_error: string | null }
interface Engines {
  running: boolean; paused_globally?: boolean
  feed?: { connected: boolean; last_tick_age_s: number | null; reconnects: number; subscribed: number }
  instruments: { id: string; symbol: string; trading_symbol: string; timeframe: string; owner: string | null; paused: boolean; fault: { message: string; since: string } | null }[]
}

/** Fault isolation: what the system has cut off on its own, and the manual controls (circuit breakers, per-user pause, global kill switch). */
export default function IsolationPage() {
  const qc = useQueryClient()
  const toast = useToast()
  const circuits = useQuery({ queryKey: ['dev', 'circuits'], queryFn: () => get<{ circuits: Circuit[] }>('/api/dev/circuits'), refetchInterval: 3000 })
  const engines = useQuery({ queryKey: ['dev', 'engines'], queryFn: () => get<Engines>('/api/dev/engines'), refetchInterval: 3000 })
  const refresh = () => { void qc.invalidateQueries({ queryKey: ['dev'] }) }
  const circuit = useMutation({ mutationFn: (v: { name: string; action: 'open' | 'close' | 'release' }) => post(`/api/dev/circuits/${encodeURIComponent(v.name)}`, { action: v.action }), onSuccess: () => { toast('Circuit updated'); refresh() } })
  const pauseUser = useMutation({ mutationFn: (v: { owner: string; paused: boolean }) => post(`/api/dev/users/${v.owner}/pause-engine`, { paused: v.paused }), onSuccess: () => { toast('Engine updated'); refresh() } })
  const killAll = useMutation({ mutationFn: (paused: boolean) => post('/api/dev/engines/pause-all', { paused }), onSuccess: (_d, paused) => { toast(paused ? 'Kill switch ON: no strategy evaluates' : 'Kill switch OFF: engines resumed'); refresh() } })
  const e = engines.data
  return (
    <div className="page">
      <h1>Fault isolation</h1>
      <p className="sub">A problem in one place must not spread. Failing instruments are cut off on their own and listed here; circuit breakers stop calling a service that keeps failing.</p>

      <div className={`card killcard ${e?.paused_globally ? 'on' : ''}`}>
        <div className="card-head">
          <h2>Global kill switch</h2>
          <span className={`chip ${e?.paused_globally ? 'bad' : 'ok'}`}>{e?.paused_globally ? 'ENGINES PAUSED' : 'engines running'}</span>
          <span className="spacer" />
          {e?.paused_globally
            ? <button className="btn primary" onClick={() => killAll.mutate(false)} disabled={killAll.isPending}>Resume all engines</button>
            : <button className="btn danger" onClick={() => { if (confirm('Stop every strategy engine? Screens keep showing the last stored state.')) killAll.mutate(true) }} disabled={killAll.isPending}>Pause all engines</button>}
        </div>
        <p className="sub">Stops strategy evaluation for everyone. Logins, pages and stored data keep working. Every use is written to the audit log.</p>
      </div>

      <div className="card">
        <div className="card-head"><h2>Circuit breakers</h2><span className="sub">CLOSED = normal · OPEN = calls blocked · HALF_OPEN = one trial call</span></div>
        <div className="scroll">
          <table>
            <thead><tr><th className="l">Service</th><th>State</th><th>Failures / limit</th><th>Retry in</th><th>Opened</th><th>Calls</th><th className="l">Last error</th><th>Control</th></tr></thead>
            <tbody>
              {(circuits.data?.circuits ?? []).map((c) => (
                <tr key={c.name}>
                  <td className="l mono">{c.name}{c.forced && <span className="chip warn" style={{ marginLeft: 6 }}>forced {c.forced}</span>}</td>
                  <td><span className={`chip ${c.state === 'CLOSED' ? 'ok' : c.state === 'HALF_OPEN' ? 'warn' : 'bad'}`}>{c.state}</span></td>
                  <td className="num">{c.failures_in_window} / {c.threshold}</td>
                  <td className="num">{c.retry_in_s == null ? '∞' : c.state === 'CLOSED' ? '–' : `${c.retry_in_s}s`}</td>
                  <td className="num">{c.opened_count}</td>
                  <td className="num">{c.total_calls} <span className="sub">({c.total_failures} failed)</span></td>
                  <td className="l dim" title={c.last_error ?? ''}>{c.last_error?.slice(0, 60) ?? '–'}</td>
                  <td>
                    <span className="btnrow">
                      <button className="btn sm" onClick={() => circuit.mutate({ name: c.name, action: 'open' })}>Force open</button>
                      <button className="btn sm" onClick={() => circuit.mutate({ name: c.name, action: 'close' })}>Force closed</button>
                      <button className="btn sm" onClick={() => circuit.mutate({ name: c.name, action: 'release' })}>Automatic</button>
                    </span>
                  </td>
                </tr>
              ))}
              {!circuits.data?.circuits.length && <tr><td colSpan={8} className="empty">No circuit has been used yet.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card">
        <div className="card-head"><h2>Strategy engines (one per instrument)</h2>{e?.feed && <span className={`chip ${e.feed.connected ? 'ok' : 'bad'}`}>feed {e.feed.connected ? 'connected' : 'DOWN'}</span>}</div>
        <div className="scroll">
          <table>
            <thead><tr><th className="l">Instrument</th><th className="l">Owner</th><th>Status</th><th className="l">Fault</th><th>Control</th></tr></thead>
            <tbody>
              {(e?.instruments ?? []).map((i) => (
                <tr key={i.id}>
                  <td className="l mono">{i.trading_symbol} · {i.timeframe}</td>
                  <td className="l mono dim">{i.owner ?? 'local'}</td>
                  <td><span className={`chip ${statusClass(i.fault ? 'DOWN' : i.paused ? 'DEGRADED' : 'OK')}`}>{i.fault ? 'FAULT' : i.paused ? 'PAUSED' : 'running'}</span></td>
                  <td className="l dim" title={i.fault?.message ?? ''}>{i.fault ? `${i.fault.message.slice(0, 70)} (since ${i.fault.since.slice(11, 19)})` : '–'}</td>
                  <td>{i.owner && <button className="btn sm" onClick={() => pauseUser.mutate({ owner: i.owner!, paused: !i.paused })}>{i.paused ? 'Resume' : 'Pause'}</button>}</td>
                </tr>
              ))}
              {!e?.instruments.length && <tr><td colSpan={5} className="empty">{e?.running === false ? 'The engine is not running.' : 'No instruments are being watched.'}</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
