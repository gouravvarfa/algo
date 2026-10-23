import { useQuery } from '@tanstack/react-query'
import { lazy, Suspense, useMemo } from 'react'
import { Link, useParams } from 'react-router-dom'
import { get } from '../../lib/api'
import { adminViewDataSource, DataSourceProvider } from '../../lib/datasource'
import type { Overview } from '../../lib/types'
import './admin.css'

const TopBottomPage = lazy(() => import('../topbottom/TopBottomPage'))
const SimulatorPage = lazy(() => import('../simulator/SimulatorPage'))

/** "View as user": the user's own pages, mounted with a read-only data source for that user. The admin never gets the user's session or secrets. */
export default function ViewAsPage() {
  const { id = '', page = 'top-bottom' } = useParams()
  const who = useQuery({ queryKey: ['admin', 'viewing', id], queryFn: () => get<Overview>(`/api/admin/users/${id}/overview`), refetchInterval: 30_000 })
  const name = who.data?.viewing?.name ?? '…'
  const source = useMemo(() => adminViewDataSource(id, name), [id, name])
  if (who.error) return <div className="page"><div className="alert">Cannot open this user: {(who.error as Error).message}</div><Link to="/admin/users">Back to users</Link></div>
  return (
    <>
      <div className="viewbanner row" style={{ alignItems: 'center' }}>
        <span>👁 Viewing <b>{name}</b> · read-only · nothing here can change their data</span>
        <span className="spacer" />
        <span className="seg">
          <Link to={`/admin/users/${id}/view/top-bottom`} className={'seglink' + (page === 'top-bottom' ? ' on' : '')}>Top Bottom</Link>
          <Link to={`/admin/users/${id}/view/simulator`} className={'seglink' + (page === 'simulator' ? ' on' : '')}>Options Simulator</Link>
        </span>
        <Link className="btn sm" to="/admin/users">Back to users</Link>
      </div>
      <DataSourceProvider source={source}>
        <Suspense fallback={<div className="page"><div className="skeleton" style={{ height: 320 }} /></div>}>
          {page === 'simulator' ? <SimulatorPage /> : <TopBottomPage />}
        </Suspense>
      </DataSourceProvider>
    </>
  )
}
