import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, get } from '../../lib/api'
import { ago } from '../../lib/format'
import { useToast } from '../../lib/toast'
import './developer.css'

interface Flag { key: string; scope: string; value: unknown; default: unknown; updated_by: string | null; updated_at: number | null }

const HELP: Record<string, string> = {
  'engines.paused': 'Global kill switch: no strategy engine evaluates. Pages and logins keep working.',
  'engine.paused': 'Pauses ONE user\'s engine (scope user:<id>). Use the Fault isolation page to set it.',
  'feature.live_chain': 'Off: the live option chain answers "switched off by a developer".',
  'feature.margin': 'Off: the margin calculator is not called.',
  'feature.collector': 'Off: the daily option download does not run.',
}

export default function FlagsPage() {
  const qc = useQueryClient()
  const toast = useToast()
  const q = useQuery({ queryKey: ['dev', 'flags'], queryFn: () => get<{ flags: Flag[] }>('/api/dev/flags') })
  const set = useMutation({
    mutationFn: (v: { key: string; scope: string; value: boolean | null }) =>
      v.value === null ? api(`/api/dev/flags?key=${encodeURIComponent(v.key)}&scope=${encodeURIComponent(v.scope)}`, { method: 'DELETE' }) : api('/api/dev/flags', { method: 'PUT', json: { key: v.key, scope: v.scope, value: v.value } }),
    onSuccess: () => { toast('Saved (audited)'); void qc.invalidateQueries({ queryKey: ['dev'] }) },
  })
  return (
    <div className="page">
      <h1>Switches</h1>
      <p className="sub">Feature flags and kill switches. A change takes effect within a couple of seconds, no deploy needed, and is written to the audit log.</p>
      <div className="card">
        <div className="scroll">
          <table>
            <thead><tr><th className="l">Switch</th><th className="l">Scope</th><th>Value</th><th>Default</th><th className="l">Changed</th><th className="l">What it does</th><th /></tr></thead>
            <tbody>
              {(q.data?.flags ?? []).map((f) => {
                const isBool = typeof f.value === 'boolean'
                const changed = f.updated_at != null
                return (
                  <tr key={`${f.key}@${f.scope}`}>
                    <td className="l mono">{f.key}</td><td className="l mono dim">{f.scope}</td>
                    <td>{isBool ? <button className={`toggle ${f.value ? 'on' : ''}`} onClick={() => set.mutate({ key: f.key, scope: f.scope, value: !f.value })} aria-pressed={!!f.value}><i /></button> : <span className="mono">{String(f.value)}</span>}</td>
                    <td className="mono dim">{String(f.default)}</td>
                    <td className="l dim">{changed ? ago(f.updated_at) : 'default'}</td>
                    <td className="l wrap dim">{HELP[f.key] ?? ''}</td>
                    <td>{changed && <button className="btn sm" onClick={() => set.mutate({ key: f.key, scope: f.scope, value: null })}>Reset</button>}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
