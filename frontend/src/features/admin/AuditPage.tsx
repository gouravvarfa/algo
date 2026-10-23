import { useInfiniteQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { get } from '../../lib/api'
import './admin.css'

interface Entry { id: number; ts: string; actor_id: string | null; actor_role: string | null; action: string; target_type: string | null; target_id: string | null; detail_json: string | null }

const tone = (a: string) => (a.includes('failed') || a.includes('locked') || a.includes('refused') ? 'bad' : a.startsWith('dev.') || a.includes('takeover') || a.includes('view_as') || a.includes('force') ? 'warn' : 'muted')

/** Everything admins and developers did, plus logins and failures. Append-only on the server (the database refuses edits). */
export default function AuditPage() {
  const [filter, setFilter] = useState('')
  const q = useInfiniteQuery({
    queryKey: ['admin', 'audit'],
    initialPageParam: undefined as number | undefined,
    queryFn: ({ pageParam }) => get<{ entries: Entry[] }>(`/api/admin/audit?limit=100${pageParam ? `&before_id=${pageParam}` : ''}`),
    getNextPageParam: (last) => (last.entries.length === 100 ? last.entries[last.entries.length - 1].id : undefined),
    refetchInterval: 15_000,
  })
  const all = useMemo(() => q.data?.pages.flatMap((p) => p.entries) ?? [], [q.data])
  const rows = useMemo(() => {
    const f = filter.trim().toLowerCase()
    return f ? all.filter((e) => `${e.action} ${e.actor_role ?? ''} ${e.target_type ?? ''} ${e.target_id ?? ''} ${e.detail_json ?? ''}`.toLowerCase().includes(f)) : all
  }, [all, filter])
  return (
    <div className="page">
      <div className="row" style={{ alignItems: 'center' }}>
        <h1>Audit log</h1>
        <span className="spacer" />
        <a className="btn sm" href={`/api/admin/audit.csv?q=${encodeURIComponent(filter.trim())}`} download>Export CSV</a>
        <input placeholder="Filter: login, user.disable, dev.kill_switch …" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ width: 320 }} />
      </div>
      <p className="sub">Append-only: the database itself refuses to change or remove a row, and each row carries a hash of the one before it.</p>
      <div className="card">
        <div className="scroll">
          <table>
            <thead><tr><th>#</th><th className="l">Time (UTC)</th><th className="l">Action</th><th className="l">By</th><th className="l">Target</th><th className="l">Detail</th></tr></thead>
            <tbody>
              {rows.map((e) => (
                <tr key={e.id}>
                  <td className="num dim">{e.id}</td><td className="l mono">{e.ts.replace('T', ' ').slice(0, 19)}</td>
                  <td className="l"><span className={`chip ${tone(e.action)}`}>{e.action}</span></td>
                  <td className="l">{e.actor_role ?? '–'}</td>
                  <td className="l mono dim">{e.target_type ? `${e.target_type}:${(e.target_id ?? '').slice(0, 10)}` : '–'}</td>
                  <td className="l mono dim wrap">{e.detail_json ?? ''}</td>
                </tr>
              ))}
              {!rows.length && <tr><td colSpan={6} className="empty">{q.isLoading ? 'Loading…' : 'No entries.'}</td></tr>}
            </tbody>
          </table>
        </div>
        {q.hasNextPage && <div style={{ marginTop: 10 }}><button className="btn" onClick={() => void q.fetchNextPage()} disabled={q.isFetchingNextPage}>Load older</button></div>}
      </div>
    </div>
  )
}
