import { useQuery } from '@tanstack/react-query'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { get } from '../lib/api'
import { num, sign } from '../lib/format'
import { useSession } from '../lib/session'
import { useTheme } from '../lib/theme'
import type { IndexQuote } from '../lib/types'
import { Icon, type IconName } from './Icon'

interface Item { to: string; label: string; icon: IconName }

const MAIN: Item[] = [
  { to: '/top-bottom', label: 'Top Bottom', icon: 'chart' },
  { to: '/simulator', label: 'Options Simulator', icon: 'sim' },
  { to: '/builder', label: 'Strategy Builder', icon: 'builder' },
  { to: '/backtesting', label: 'Backtesting', icon: 'backtest' },
  { to: '/history', label: 'History', icon: 'history' },
  { to: '/options', label: 'Option Chain', icon: 'chain' },
  { to: '/settings', label: 'Settings', icon: 'settings' },
]
const ADMIN: Item[] = [
  { to: '/admin/users', label: 'Users', icon: 'users' },
  { to: '/admin/audit', label: 'Audit log', icon: 'audit' },
]
const DEV: Item[] = [
  { to: '/dev/health', label: 'System health', icon: 'health' },
  { to: '/dev/functions', label: 'Functions', icon: 'func' },
  { to: '/dev/isolation', label: 'Fault isolation', icon: 'shield' },
  { to: '/dev/errors', label: 'Errors', icon: 'bug' },
  { to: '/dev/flags', label: 'Switches', icon: 'flag' },
]

function titleFor(pathname: string): string {
  const all = [...MAIN, ...ADMIN, ...DEV, { to: '/account', label: 'Account', icon: 'user' as IconName }]
  return all.find((i) => pathname === i.to || pathname.startsWith(i.to + '/'))?.label ?? 'Algo Trading'
}

function IndexCard({ name, label }: { name: 'NIFTY' | 'BANKNIFTY'; label: string }) {
  const q = useQuery({ queryKey: ['index', name], queryFn: () => get<IndexQuote>(`/api/index/${name}`), refetchInterval: 15_000, retry: false })
  const d = q.data
  return (
    <div className="idxcard" title={d ? `${d.live ? 'Live tick' : 'Last REST candle'} · previous close ${num(d.prev_close)}` : ''}>
      <b>{label}</b>
      <span className="p num">{d?.price != null ? num(d.price) : '–'}</span>{' '}
      {d?.pct != null && <span className={`num ${sign(d.change)}`} style={{ fontSize: 12 }}>{d.pct >= 0 ? '+' : ''}{d.pct.toFixed(2)}%</span>}
    </div>
  )
}

export function Sidebar() {
  const { can, me, mode, logout } = useSession()
  const { dark, set } = useTheme()
  return (
    <aside className="sidebar">
      <NavLink className="logo" to="/top-bottom">
        <i><Icon name="chart" /></i>
        <span>Algo Trading<small>Research · paper trading</small></span>
      </NavLink>
      {MAIN.map((i) => <NavItem key={i.to} item={i} />)}
      {can('users.read') && (<><div className="navgroup">Admin</div>{ADMIN.map((i) => <NavItem key={i.to} item={i} />)}</>)}
      {can('system.read') && (<><div className="navgroup">Developer</div>{DEV.map((i) => <NavItem key={i.to} item={i} />)}</>)}
      <div className="idxcards">
        <IndexCard name="NIFTY" label="NIFTY 50" />
        <IndexCard name="BANKNIFTY" label="BANKNIFTY" />
      </div>
      <div className="themeseg" role="group" aria-label="Theme">
        <button className={!dark ? 'on' : ''} onClick={() => set('light')}>Light</button>
        <button className={dark ? 'on' : ''} onClick={() => set('dark')}>Dark</button>
      </div>
      {mode === 'platform' && me && (
        <div className="accountbox">
          <NavLink className="navlink" to="/account" title="Account and password"><Icon name="user" /><span>{me.user.name}</span></NavLink>
          <button className="navlink linkbtn" onClick={() => void logout()}><Icon name="logout" /><span>Sign out</span></button>
        </div>
      )}
    </aside>
  )
}

function NavItem({ item }: { item: Item }) {
  return (
    <NavLink to={item.to} className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
      <Icon name={item.icon} />
      <span>{item.label}</span>
    </NavLink>
  )
}

function StatusBar() {
  const { pathname } = useLocation()
  const ov = useQuery({ queryKey: ['shell', 'market'], queryFn: () => get<{ market_open: boolean }>('/api/overview'), refetchInterval: 60_000, retry: false })
  const open = ov.data?.market_open
  return (
    <header className="topbar">
      <span className="title">{titleFor(pathname)}</span>
      <span className="spacer" />
      <span className={'pill' + (open ? ' open' : '')} title="NSE session 09:15-15:30 IST, Mon-Fri. Exchange holidays are not detected.">
        <i />{ov.data ? (open ? 'Market open' : 'Market closed') : 'Market'}
      </span>
      <span className="pill paper" title="Paper trading and research only. No order is ever sent to a broker."><b>PAPER</b>&nbsp;no orders</span>
    </header>
  )
}

/** The frame around every signed-in page: left navigation (admin and developer groups only for those roles), status bar, and the page. */
export function Shell() {
  return (
    <div className="shell">
      <Sidebar />
      <StatusBar />
      <Outlet />
    </div>
  )
}

