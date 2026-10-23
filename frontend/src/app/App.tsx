import { lazy, Suspense, type ReactElement } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { AccountPage } from '../features/auth/AccountPage'
import { LoginPage } from '../features/auth/LoginPage'
import { SetupPage } from '../features/auth/SetupPage'
import { useSession } from '../lib/session'
import { ConsoleShell } from './ConsoleShell'
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
const AnnouncementsPage = lazy(() => import('../features/admin/AnnouncementsPage'))
const AdminOverview = lazy(() => import('../features/admin/OverviewPage'))
const SessionsPage = lazy(() => import('../features/developer/MorePages').then((m) => ({ default: m.SessionsPage })))
const LoginAttemptsPage = lazy(() => import('../features/developer/MorePages').then((m) => ({ default: m.LoginAttemptsPage })))
const RolesPage = lazy(() => import('../features/developer/MorePages').then((m) => ({ default: m.RolesPage })))
const SystemInfoPage = lazy(() => import('../features/developer/MorePages').then((m) => ({ default: m.SystemInfoPage })))
const LiveLogsPage = lazy(() => import('../features/developer/MorePages').then((m) => ({ default: m.LiveLogsPage })))

const Loading = () => <div className="page"><div className="skeleton" style={{ height: 320 }} /></div>

/** Signed-in pages only. With accounts on, a visitor without a session is sent to the login page and returns here afterwards. */
function Guard({ children, need }: { children: ReactElement; need?: string }) {
  const { mode, me, loading, can, endReason } = useSession()
  const loc = useLocation()
  if (loading) return <Loading />
  if (mode === 'platform' && !me) return <Navigate to={endReason ? `/login?why=${encodeURIComponent(endReason)}` : '/login'} replace state={{ from: loc.pathname }} />
  if (need && !can(need)) return <div className="page"><div className="card"><h1>Not allowed</h1><p className="sub" style={{ marginTop: 8 }}>Your role does not include this page.</p></div></div>
  return <Suspense fallback={<Loading />}>{children}</Suspense>
}

/** Admins and developers land in their own console; the trading pages carry no admin or developer entries at all. */
function Home() {
  const { me, can, loading } = useSession()
  if (loading) return <Loading />
  return <Navigate to={me && can('system.read') ? '/dev' : me && can('users.read') ? '/admin' : '/top-bottom'} replace />
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/setup/:token" element={<SetupPage />} />
      <Route element={<Shell />}>
        <Route path="/" element={<Home />} />
        <Route path="/top-bottom" element={<Guard><TopBottomPage /></Guard>} />
        <Route path="/simulator" element={<Guard><SimulatorPage /></Guard>} />
        <Route path="/builder" element={<Guard><BuilderPage view="builder" /></Guard>} />
        <Route path="/backtesting" element={<Guard><BuilderPage view="backtest" /></Guard>} />
        <Route path="/history" element={<Guard><BuilderPage view="history" /></Guard>} />
        <Route path="/options" element={<Guard><OptionChainPage /></Guard>} />
        <Route path="/settings" element={<Guard><SettingsPage /></Guard>} />
        <Route path="/account" element={<Guard><AccountPage /></Guard>} />
        <Route path="*" element={<Navigate to="/top-bottom" replace />} />
      </Route>
      <Route path="/admin" element={<Guard need="users.read"><ConsoleShell kind="admin" /></Guard>}>
        <Route index element={<AdminOverview />} />
        <Route path="users" element={<UsersPage />} />
        <Route path="users/:id/view/:page" element={<Guard need="users.view_as"><ViewAsPage /></Guard>} />
        <Route path="audit" element={<Guard need="audit.read"><AuditPage /></Guard>} />
        <Route path="announcements" element={<Guard need="users.manage"><AnnouncementsPage /></Guard>} />
        <Route path="*" element={<Navigate to="/admin" replace />} />
      </Route>
      <Route path="/dev" element={<Guard need="system.read"><ConsoleShell kind="dev" /></Guard>}>
        <Route index element={<HealthPage />} />
        <Route path="health" element={<Navigate to="/dev" replace />} />
        <Route path="functions" element={<FunctionsPage />} />
        <Route path="isolation" element={<IsolationPage />} />
        <Route path="errors" element={<ErrorsPage />} />
        <Route path="flags" element={<FlagsPage />} />
        <Route path="sessions" element={<SessionsPage />} />
        <Route path="logins" element={<LoginAttemptsPage />} />
        <Route path="roles" element={<RolesPage />} />
        <Route path="info" element={<SystemInfoPage />} />
        <Route path="logs" element={<LiveLogsPage />} />
        <Route path="*" element={<Navigate to="/dev" replace />} />
      </Route>
    </Routes>
  )
}
