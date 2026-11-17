import { useEffect } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { Icon, type IconName } from './Icon'

/** The hamburger button in the top bar (phones and small tablets, hidden on wide screens by CSS). */
export function Hamburger({ open, onClick }: { open: boolean; onClick: () => void }) {
  return (
    <button type="button" className="hamburger" onClick={onClick} aria-label={open ? 'Close menu' : 'Open menu'} aria-expanded={open} aria-controls="app-sidebar">
      <span /><span /><span />
    </button>
  )
}

/** Dimmed area behind the open menu: tap it to close. Also closes on Escape and whenever the page changes. */
export function NavBackdrop({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { pathname } = useLocation()
  useEffect(() => { onClose() }, [pathname]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!open) return
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [open, onClose])
  return <div className="navbackdrop" onClick={onClose} aria-hidden="true" />
}

const TABS: { to: string; label: string; icon: IconName }[] = [
  { to: '/top-bottom', label: 'Strategy', icon: 'chart' },
  { to: '/real-trades', label: 'Real', icon: 'money' },
  { to: '/positions', label: 'Positions', icon: 'stack' },
  { to: '/options', label: 'Chain', icon: 'chain' },
]

/** Phones: the four pages used every day, one tap away at the bottom (thumb reach); "More" opens the full menu. Hidden on wide screens by CSS. */
export function BottomNav({ onMore, moreOpen }: { onMore: () => void; moreOpen: boolean }) {
  return (
    <nav className="bottomnav" aria-label="Main">
      {TABS.map((t) => (
        <NavLink key={t.to} to={t.to} className={({ isActive }) => 'bn' + (isActive ? ' active' : '')}>
          <Icon name={t.icon} /><span>{t.label}</span>
        </NavLink>
      ))}
      <button type="button" className={'bn' + (moreOpen ? ' active' : '')} onClick={onMore} aria-label="More pages" aria-expanded={moreOpen}>
        <Icon name="menu" /><span>More</span>
      </button>
    </nav>
  )
}
