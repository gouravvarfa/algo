import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'
import { get } from '../lib/api'
import { num, sign } from '../lib/format'
import { useSession } from '../lib/session'
import { fresh, useLiveTicks } from '../lib/stream'
import type { IndexQuote } from '../lib/types'
import { Icon, type IconName } from './Icon'
import { Hamburger, NavBackdrop } from './MobileNav'
import { ThemeToggle } from './ThemeToggle'
import '../features/admin/admin.css'

interface Item { to: string; label: string; icon: IconName }

const MAIN: Item[] = [
  { to: '/top-bottom', label: 'Top Bottom', icon: 'chart' },
  { to: '/simulator', label: 'Options Simulator', icon: 'sim' },
  { to: '/options-journal', label: 'Options Journal', icon: 'chain' },
  { to: '/dry-run', label: 'Dry Run', icon: 'backtest' },
  { to: '/builder', label: 'Strategy Builder', icon: 'builder' },
  { to: '/backtesting', label: 'Backtesting', icon: 'backtest' },
  { to: '/history', label: 'History', icon: 'history' },
  { to: '/options', label: 'Option Chain', icon: 'chain' },
  { to: '/settings', label: 'Settings', icon: 'settings' },
]
function titleFor(pathname: string): string {
  const all = [...MAIN, { to: '/account', label: 'Account', icon: 'user' as IconName }]
  return all.find((i) => pathname === i.to || pathname.startsWith(i.to + '/'))?.label ?? 'Algo Trading'
}

function AnnouncementBanner() {
  const q = useQuery({ queryKey: ['announcement'], queryFn: () => get<{ announcement: { message: string; level: string } | null }>('/api/announcement'), refetchInterval: 60_000, retry: false })
  const a = q.data?.announcement
  return a ? <div className={`banner ${a.level}`} role="status" style={{ margin: '12px 24px 0' }}><span>{a.message}</span></div> : null
}

function IndexCard({ name, label }: { name: 'NIFTY' | 'BANKNIFTY'; label: string }) {
  const q = useQuery({ queryKey: ['index', name], queryFn: () => get<IndexQuote>(`/api/index/${name}`), refetchInterval: 15_000, retry: false })
  const d = q.data
  // the price itself is pushed tick by tick on every page; the 15 s query brings the previous close
  const token = name === 'BANKNIFTY' ? '99926009' : '99926000'
  const { ticks } = useLiveTicks([`NSE:${token}`])
  const t = ticks[token]
  const price = fresh(t) ? t.ltp : d?.price
  const change = price != null && d?.prev_close ? price - d.prev_close : null
  const pct = change != null && d?.prev_close ? (change / d.prev_close) * 100 : d?.pct
  return (
    <div className="idxcard" title={d ? `${d.live ? 'Live tick' : 'Last REST candle'} · previous close ${num(d.prev_close)}` : ''}>
      <b>{label}</b>
      <span className="p num">{price != null ? num(price) : '–'}</span>{' '}
      {pct != null && <span className={`num ${sign(change ?? d?.change)}`} style={{ fontSize: 12 }}>{pct >= 0 ? '+' : ''}{pct.toFixed(2)}%</span>}
    </div>
  )
}

export function Sidebar() {
  const { me, mode, logout, can } = useSession()
  return (
    <aside className="sidebar" id="app-sidebar">
      <NavLink className="logo" to="/top-bottom">
        <i><Icon name="chart" /></i>
        <span>Algo Trading<small>Research · paper trading</small></span>
      </NavLink>
      {MAIN.map((i) => <NavItem key={i.to} item={i} />)}
      <div className="idxcards">
        <IndexCard name="NIFTY" label="NIFTY 50" />
        <IndexCard name="BANKNIFTY" label="BANKNIFTY" />
      </div>
      {mode === 'platform' && me && (
        <div className="accountbox">
          {can('system.read') && <NavLink className="navlink consolelink" to="/dev"><Icon name="shield" /><span>Developer console</span></NavLink>}
          {can('users.read') && <NavLink className="navlink consolelink" to="/admin"><Icon name="users" /><span>Admin console</span></NavLink>}
          <NavLink className="navlink" to="/account" title="Account and password"><Icon name="user" /><span>{me.user.name}</span></NavLink>
          <button className="navlink linkbtn" onClick={() => void logout()}><Icon name="logout" /><span>Sign out</span></button>
        </div>
      )}
    </aside>
  )
}

function NavItem({ item }: { item: Item }) {
  return (
    <NavLink to={item.to} title={item.label} className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
      <Icon name={item.icon} />
      <span>{item.label}</span>
    </NavLink>
  )
}

function StatusBar({ navOpen, onNav }: { navOpen: boolean; onNav: () => void }) {
  const { pathname } = useLocation()
  const ov = useQuery({ queryKey: ['shell', 'market'], queryFn: () => get<{ market_open: boolean }>('/api/overview'), refetchInterval: 60_000, retry: false })
  const open = ov.data?.market_open
  const br = useQuery({ queryKey: ['broker'], queryFn: () => get<{ effective_mode: string }>('/api/me/broker'), refetchInterval: 60_000, retry: false })
  const real = br.data?.effective_mode === 'real'
  const connected = (br.data as { status?: string } | undefined)?.status === 'connected'
  const fu = useQuery({ queryKey: ['broker-funds'], queryFn: () => get<{ connected: boolean; available_cash?: number | null; span_margin?: number | null; exposure_margin?: number | null; error?: string }>('/api/me/broker/funds'),
    enabled: connected, refetchInterval: 60_000, retry: false })
  const f = fu.data
  const rs = (v: number | null | undefined) => (v == null ? '–' : '₹' + Math.round(v).toLocaleString('en-IN'))
  return (
    <header className="topbar">
      <Hamburger open={navOpen} onClick={onNav} />
      <span className="title">{titleFor(pathname)}</span>
      <span className="spacer" />
      <span className={'pill' + (open ? ' open' : '')} title="NSE session 09:15-15:30 IST, Mon-Fri. Exchange holidays are not detected.">
        <i />{ov.data ? (open ? 'Market open' : 'Market closed') : 'Market'}
      </span>
      {connected && f?.connected && (
        <Link to="/settings" className="pill" title={f.error ?? `Angel One margin available. Used: SPAN ${rs(f.span_margin)} · Exposure ${rs(f.exposure_margin)}. Updated every minute.`}>
          <b>Angel One</b>&nbsp;{f.error ? 'not reachable' : `margin ${rs(f.available_cash)}`}
        </Link>
      )}
      {real ? (
        <Link to="/settings" className="pill real" title="Real mode is switched on for your Angel One account. Click to see your limits."><b>REAL</b>&nbsp;armed</Link>
      ) : (
        <span className="pill paper" title="Paper trading and research only. No order is sent to a broker."><b>PAPER</b>&nbsp;no orders</span>
      )}
      <ThemeToggle />
    </header>
  )
}

/** The frame around every signed-in page: left navigation (admin and developer groups only for those roles), status bar, and the page. */
export function Shell() {
  const [nav, setNav] = useState(false)
  return (
    <div className={'shell' + (nav ? ' nav-open' : '')}>
      <Sidebar />
      <NavBackdrop open={nav} onClose={() => setNav(false)} />
      <StatusBar navOpen={nav} onNav={() => setNav((v) => !v)} />
      <AnnouncementBanner />
      <Outlet />
    </div>
  )
}

