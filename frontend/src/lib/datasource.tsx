import { createContext, useContext, useMemo, type ReactNode } from 'react'
import { get } from './api'
import type { JournalEvent, Overview, Snapshot } from './types'

/** Where a page reads its data from. The user's own pages use OwnDataSource; the admin's "view as user" mounts the SAME page components
 * with AdminViewDataSource(userId), so the admin sees exactly the same screen (read-only) without any copy of the page code. */
export interface DataSource {
  key: string
  readOnly: boolean
  viewingName?: string
  overview: () => Promise<Overview>
  instrument: (id: string) => Promise<Snapshot>
  journal: (limit?: number) => Promise<JournalEvent[]>
  simPositions: (symbol: string) => Promise<{ positions: unknown[] }>
}

export const ownDataSource: DataSource = {
  key: 'own',
  readOnly: false,
  overview: () => get<Overview>('/api/overview'),
  instrument: (id) => get<Snapshot>(`/api/instruments/${id}`),
  journal: (limit = 300) => get<JournalEvent[]>(`/api/journal?limit=${limit}`),
  simPositions: (symbol) => get<{ positions: unknown[] }>(`/api/me/sim-positions?symbol=${symbol}`),
}

export function adminViewDataSource(userId: string, name: string): DataSource {
  const base = `/api/admin/users/${userId}`
  return {
    key: `view:${userId}`,
    readOnly: true,
    viewingName: name,
    overview: () => get<Overview>(`${base}/overview`),
    instrument: (id) => get<Snapshot>(`${base}/instruments/${id}`),
    journal: (limit = 300) => get<JournalEvent[]>(`${base}/journal?limit=${limit}`),
    simPositions: (symbol) => get<{ positions: unknown[] }>(`${base}/sim-positions?symbol=${symbol}`),
  }
}

const Ctx = createContext<DataSource>(ownDataSource)

export function DataSourceProvider({ source, children }: { source: DataSource; children: ReactNode }) {
  const value = useMemo(() => source, [source])
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export const useDataSource = () => useContext(Ctx)
