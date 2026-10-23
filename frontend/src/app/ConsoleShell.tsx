import { useQuery } from '@tanstack/react-query'
import { Suspense } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { get } from '../lib/api'
import { useSession } from '../lib/session'
import { useTheme } from '../lib/theme'
import { Icon, type IconName } from './Icon'

export type ConsoleKind = 'admin' | 'dev'
interface Item { to: string; label: string; icon: IconName; end?: boolean }

const ITEMS: Record<ConsoleKind, Item[]> = {
  admin: [
    { to: '/admin', label: 'Overview', icon: 'health', end: true },
    { to: '/admin/users', label: 'Users', icon: 'users' },
    { to: '/admin/audit', label: 'Audit log', icon: 'audit' },
  ],
  dev: [
    { to: '/dev', label: 'System health', icon: 'health', end: true },
    { to: '/dev/functions', label: 'Functions', icon: 'func' },
    { to: '/dev/isolation', label: 'Fault isolation', icon: 'shield' },
    { to: '/dev/errors', label: 'Errors', icon: 'bug' },
    { to: '/dev/flags', label: 'Switches', icon: 'flag' },
    { to: '/dev/sessions', label: 'Sessions', icon: 'user' },
    { to: '/dev/logins', label: 'Login attempts', icon: 'audit' },
    { to: '/dev/roles', label: 'Roles & permissions', icon: 'users' },
    { to: '/dev/info', label: 'System info', icon: 'settings' },
    { to: '/dev/logs', label: 'Live logs', icon: 'history' },
  ],
}
const BRAND: Record<ConsoleKind, { name: string; sub: string }> = {
  admin: { name: 'Admin console', sub: 'People and access' },
  dev: { name: 'Developer console', sub: 'Health and fault isolation' },
}

function titleFor(kind: ConsoleKind, pathname: string): string {
  if (pathname.includes('/view/')) return 'View as user'
  return [...ITEMS[kind]].reverse().find((i) => pathname === i.to || pathname.startsWith(i.to + '/'))?.label ?? BRAND[kind].name
}

/** A separate application frame for the admin and the developer: its own navigation, its own accent colour and a clear way back to the trading app.
 * The trading app never shows these screens in its own sidebar. */
export function ConsoleShell({ kind }: { kind: ConsoleKind }) {
  const { me, can, logout } = useSession()
  const { dark, set } = useTheme()
  const { pathname } = useLocation()
  const ov = useQuery({ queryKey: ['shell', 'market'], queryFn: () => get<{ market_open: boolean }>('/api/overview'), refetchInterval: 60_000, retry: false })
  const sys = useQuery({ queryKey: ['console', 'summary'], queryFn: () => get<{ overall: string }>('/api/admin/system-summary'), refetchInterval: 15_000, retry: false, enabled: can('users.read') })
  const open = ov.data?.market_open
  const other: ConsoleKind | null = kind === 'admin' ? (can('system.read') ? 'dev' : null) : can('users.read') ? 'admin' : null
  return (
    <div className={`shell console console-${kind}`}>
      <aside className="sidebar">
        <NavLink className="logo" to={kind === 'admin' ? '/admin' : '/dev'}>
          <i><Icon name={kind === 'admin' ? 'users' : 'shield'} /></i>
          <span>{BRAND[kind].name}<small>{BRAND[kind].sub}</small></span>
        </NavLink>
        {ITEMS[kind].map((i) => (
          <NavLink key={i.to} to={i.to} end={i.end} className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}><Icon name={i.icon} /><span>{i.label}</span></NavLink>
        ))}
        <div className="navgroup">Go to</div>
        <NavLink className="navlink" to="/top-bottom"><Icon name="chart" /><span>Trading app</span></NavLink>
        {other && <NavLink className="navlink" to={other === 'admin' ? '/admin' : '/dev'}><Icon name={other === 'admin' ? 'users' : 'shield'} /><span>{BRAND[other].name}</span></NavLink>}
        <div className="idxcards" />
        <div className="themeseg" role="group" aria-label="Theme">
          <button className={!dark ? 'on' : ''} onClick={() => set('light')}>Light</button>
          <button className={dark ? 'on' : ''} onClick={() => set('dark')}>Dark</button>
        </div>
        {me && (
          <div className="accountbox">
            <NavLink className="navlink" to="/account" title="Account and password"><Icon name="user" /><span>{me.user.name}</span></NavLink>
            <button className="navlink linkbtn" onClick={() => void logout()}><Icon name="logout" /><span>Sign out</span></button>
          </div>
        )}
      </aside>
      <header className="topbar">
        <span className={`consolebadge ${kind}`}>{kind === 'admin' ? 'ADMIN' : 'DEVELOPER'}</span>
        <span className="title">{titleFor(kind, pathname)}</span>
        <span className="spacer" />
        {sys.data && <span className={`pill sys ${sys.data.overall === 'OK' ? 'okk' : sys.data.overall === 'DEGRADED' ? 'degr' : 'down'}`} title="Overall system health (details in the Developer console)"><i />System {sys.data.overall}</span>}
        <span className={'pill' + (open ? ' open' : '')}><i />{ov.data ? (open ? 'Market open' : 'Market closed') : 'Market'}</span>
        <span className="pill paper" title="Paper trading only. No order is ever sent to a broker."><b>PAPER</b>&nbsp;no orders</span>
      </header>
      <Suspense fallback={<div className="page"><div className="skeleton" style={{ height: 320 }} /></div>}><Outlet /></Suspense>
    </div>
  )
}
