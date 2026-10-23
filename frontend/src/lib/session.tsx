import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post } from './api'
import type { Me, Role } from './types'

export type AuthMode = 'open' | 'legacy' | 'platform'

interface SessionValue {
  mode: AuthMode | null
  me: Me | null
  loading: boolean
  role: Role | null
  can: (permission: string) => boolean
  /** why the last session ended ('idle', 'replaced', 'forced', ...), taken from the server's 401, so the login page can say it */
  endReason: string | null
  refresh: () => Promise<void>
  logout: () => Promise<void>
}

const Ctx = createContext<SessionValue | null>(null)
const HEARTBEAT_MS = 30_000

/** Who is signed in. Mode 'open' = local use without any login (everything except the admin and developer panels works);
 * 'platform' = password + TOTP accounts with server-side sessions. While signed in, a heartbeat every 30 s keeps the session alive. */
export function SessionProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient()
  const modeQ = useQuery({ queryKey: ['auth', 'mode'], queryFn: () => get<{ mode: AuthMode }>('/api/auth/mode'), staleTime: 60_000, retry: 1 })
  const mode = modeQ.data?.mode ?? null
  const meQ = useQuery({
    queryKey: ['auth', 'me'],
    enabled: mode === 'platform',
    queryFn: async () => {
      try {
        return await get<Me>('/api/auth/me')
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return null
        throw e
      }
    },
    staleTime: 15_000,
    retry: false,
  })
  const me = meQ.data ?? null

  const [endReason, setEndReason] = useState<string | null>(null)
  const touched = useRef(false)
  useEffect(() => {
    const mark = () => { touched.current = true }
    for (const ev of ['pointerdown', 'keydown', 'wheel', 'touchstart']) window.addEventListener(ev, mark, { passive: true })
    return () => { for (const ev of ['pointerdown', 'keydown', 'wheel', 'touchstart']) window.removeEventListener(ev, mark) }
  }, [])

  useEffect(() => {
    if (!me) return
    const id = window.setInterval(() => {
      const active = touched.current
      touched.current = false
      post('/api/auth/heartbeat', { active }).catch(() => { /* a 401 triggers auth:required below */ })
    }, HEARTBEAT_MS)
    return () => window.clearInterval(id)
  }, [me])

  useEffect(() => {
    const onAuth = (ev: Event) => {
      const reason = (ev as CustomEvent<ApiError>).detail?.body?.reason
      if (typeof reason === 'string') setEndReason(reason)
      if (mode === 'legacy') window.location.assign('/login')           // the old single-user login: a form post handled by the server
      else void qc.invalidateQueries({ queryKey: ['auth', 'me'] })
    }
    window.addEventListener('auth:required', onAuth)
    return () => window.removeEventListener('auth:required', onAuth)
  }, [qc, mode])

  const refresh = useCallback(async () => {
    await qc.invalidateQueries({ queryKey: ['auth'] })
  }, [qc])

  const logout = useCallback(async () => {
    try { await post('/api/auth/logout') } catch { /* already signed out */ }
    qc.clear()
    await qc.invalidateQueries({ queryKey: ['auth'] })
  }, [qc])

  const value = useMemo<SessionValue>(() => ({
    mode,
    me,
    loading: modeQ.isLoading || (mode === 'platform' && meQ.isLoading),
    role: me?.user.role ?? null,
    can: (p) => !!me?.permissions.includes(p),
    endReason,
    refresh,
    logout,
  }), [mode, me, modeQ.isLoading, meQ.isLoading, refresh, logout, endReason])

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useSession(): SessionValue {
  const v = useContext(Ctx)
  if (!v) throw new Error('useSession outside SessionProvider')
  return v
}
