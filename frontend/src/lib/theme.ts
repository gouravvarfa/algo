import { useCallback, useEffect, useState } from 'react'

const KEY = 'theme'

function stored(): 'light' | 'dark' | null {
  try {
    const t = localStorage.getItem(KEY)
    return t === 'light' || t === 'dark' ? t : null
  } catch {
    return null
  }
}

export function isDark(): boolean {
  const t = document.documentElement.getAttribute('data-theme')
  return t ? t === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches
}

/** Light / dark with the choice remembered; follows the system until the person picks one. Other components re-read it on 'themechange'. */
export function useTheme() {
  const [dark, setDark] = useState(isDark)
  useEffect(() => {
    const on = () => setDark(isDark())
    window.addEventListener('themechange', on)
    return () => window.removeEventListener('themechange', on)
  }, [])
  const set = useCallback((t: 'light' | 'dark') => {
    try { localStorage.setItem(KEY, t) } catch { /* private mode: still applies for this page */ }
    document.documentElement.setAttribute('data-theme', t)
    window.dispatchEvent(new CustomEvent('themechange'))
  }, [])
  return { dark, set, saved: stored() }
}

export const cssVar = (name: string): string => getComputedStyle(document.documentElement).getPropertyValue(name).trim()

const MOTION_KEY = 'bgmotion'
/** Animated background on / off. Default ON; it does not follow the system "reduce motion" setting, the switch in the corner does. */
export function applyMotion(): void {
  let off = false
  try { off = localStorage.getItem(MOTION_KEY) === 'off' } catch { /* ignore */ }
  document.documentElement.setAttribute('data-motion', off ? 'off' : 'on')
}
export function useMotion() {
  const [on, setOn] = useState(() => document.documentElement.getAttribute('data-motion') !== 'off')
  const toggle = useCallback(() => {
    const next = !on
    try { localStorage.setItem(MOTION_KEY, next ? 'on' : 'off') } catch { /* ignore */ }
    document.documentElement.setAttribute('data-motion', next ? 'on' : 'off')
    setOn(next)
  }, [on])
  return { on, toggle }
}
