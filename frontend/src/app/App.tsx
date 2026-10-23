import { lazy, Suspense, type ReactElement } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { AccountPage } from '../features/auth/AccountPage'
import { LoginPage } from '../features/auth/LoginPage'
import { SetupPage } from '../features/auth/SetupPage'
import { useSession } from '../lib/session'
import { Shell } from './Shell'

// every page is its own chunk: opening the Simulator does not download the Developer panel
const TopBottomPage = lazy(() => import('../features/topbottom/TopBottomPage'))
const SimulatorPage = lazy(() => import('../features/simulator/SimulatorPage'))
const BuilderPage = lazy(() => import('../features/builder/BuilderPage'))
const OptionChainPage = lazy(() => import('../features/options/OptionChainPage'))
const SettingsPage = lazy(() => import('../features/settings/SettingsPage'))
const UsersPage = lazy(() => import('../features/admin/UsersPage'))
const ViewAsPage = lazy(() => import('../features/admin/ViewAsPage'))
const AuditPage = lazy(() => import('../features/admin/AuditPage'))
const HealthPage = lazy(() => import('../features/developer/HealthPage'))
const FunctionsPage = lazy(() => import('../features/developer/FunctionsPage'))
const IsolationPage = lazy(() => import('../features/developer/IsolationPage'))
const ErrorsPage = lazy(() => import('../features/developer/ErrorsPage'))
const FlagsPage = lazy(() => import('../features/developer/FlagsPage'))

const Loading = () => <div className="page"><div className="skeleton" style={{ height: 320 }} /></div>

/** Signed-in pages only. With accounts on, a visitor without a session is sent to the login page and returns here afterwards. */
function Guard({ children, need }: { children: ReactElement; need?: string }) {
  const { mode, me, loading, can } = useSession()
  const loc = useLocation()
  if (loading) return <Loading />
  if (mode === 'platform' && !me) return <Navigate to="/login" replace state={{ from: loc.pathname }} />
  if (need && !can(need)) return <div className="page"><div className="card"><h1>Not allowed</h1><p className="sub" style={{ marginTop: 8 }}>Your role does not include this page.</p></div></div>
  return <Suspense fallback={<Loading />}>{children}</Suspense>
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/setup/:token" element={<SetupPage />} />
      <Route element={<Shell />}>
        <Route path="/" element={<Navigate to="/top-bottom" replace />} />
        <Route path="/top-bottom" element={<Guard><TopBottomPage /></Guard>} />
        <Route path="/simulator" element={<Guard><SimulatorPage /></Guard>} />
        <Route path="/builder" element={<Guard><BuilderPage view="builder" /></Guard>} />
        <Route path="/backtesting" element={<Guard><BuilderPage view="backtest" /></Guard>} />
        <Route path="/history" element={<Guard><BuilderPage view="history" /></Guard>} />
        <Route path="/options" element={<Guard><OptionChainPage /></Guard>} />
        <Route path="/settings" element={<Guard><SettingsPage /></Guard>} />
        <Route path="/account" element={<Guard><AccountPage /></Guard>} />
        <Route path="/admin/users" element={<Guard need="users.read"><UsersPage /></Guard>} />
        <Route path="/admin/users/:id/view/:page" element={<Guard need="users.view_as"><ViewAsPage /></Guard>} />
        <Route path="/admin/audit" element={<Guard need="audit.read"><AuditPage /></Guard>} />
        <Route path="/dev/health" element={<Guard need="system.read"><HealthPage /></Guard>} />
        <Route path="/dev/functions" element={<Guard need="system.read"><FunctionsPage /></Guard>} />
        <Route path="/dev/isolation" element={<Guard need="system.read"><IsolationPage /></Guard>} />
        <Route path="/dev/errors" element={<Guard need="system.read"><ErrorsPage /></Guard>} />
        <Route path="/dev/flags" element={<Guard need="system.read"><FlagsPage /></Guard>} />
        <Route path="*" element={<Navigate to="/top-bottom" replace />} />
      </Route>
    </Routes>
  )
}
