import { useEffect } from 'react'
import { useLocation } from 'react-router-dom'

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
