import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SessionProvider, useSession } from './session'

const ME = { user: { id: 'u1', username: 'rahul', name: 'Rahul', role: 'user', owner: false }, permissions: ['session.self'], session: { device: 'Edge', started_at: '', last_active: '' }, csrf_token: 't' }

function Probe() {
  const s = useSession()
  return <div><span data-testid="mode">{String(s.mode)}</span><span data-testid="who">{s.me ? s.me.user.username : 'nobody'}</span><span data-testid="why">{s.endReason ?? '-'}</span><span data-testid="admin">{String(s.can('users.read'))}</span></div>
}
const mount = () => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><SessionProvider><Probe /></SessionProvider></QueryClientProvider>)

function fakeServer(opts: { mode: string; signedIn: boolean }) {
  const calls: string[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation((url, init) => {
    const u = String(url)
    calls.push(`${init?.method ?? 'GET'} ${u}${init?.body ? ' ' + String(init.body) : ''}`)
    if (u === '/api/auth/mode') return Promise.resolve(new Response(JSON.stringify({ mode: opts.mode })))
    if (u === '/api/auth/me') return Promise.resolve(opts.signedIn ? new Response(JSON.stringify(ME)) : new Response(JSON.stringify({ error: 'LOGIN_REQUIRED', reason: 'idle' }), { status: 401 }))
    return Promise.resolve(new Response('{"ok":true}'))
  })
  return calls
}

describe('SessionProvider', () => {
  beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: true }) })
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

  it('pings the server every 30 s while signed in, and says whether the person touched the page', async () => {
    const calls = fakeServer({ mode: 'platform', signedIn: true })
    mount()
    await waitFor(() => expect(screen.getByTestId('who')).toHaveTextContent('rahul'))
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(calls.filter((c) => c.startsWith('POST /api/auth/heartbeat'))).toEqual(['POST /api/auth/heartbeat {"active":false}'])
    act(() => { window.dispatchEvent(new Event('pointerdown')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(calls.filter((c) => c.startsWith('POST /api/auth/heartbeat')).pop()).toBe('POST /api/auth/heartbeat {"active":true}')
  })

  it('never pings when nobody is signed in', async () => {
    const calls = fakeServer({ mode: 'platform', signedIn: false })
    mount()
    await waitFor(() => expect(screen.getByTestId('mode')).toHaveTextContent('platform'))
    await act(async () => { await vi.advanceTimersByTimeAsync(95_000) })
    expect(calls.some((c) => c.includes('heartbeat'))).toBe(false)
    expect(screen.getByTestId('who')).toHaveTextContent('nobody')
  })

  it('remembers why a session ended, taken from the 401 of any request', async () => {
    fakeServer({ mode: 'platform', signedIn: true })
    mount()
    await waitFor(() => expect(screen.getByTestId('who')).toHaveTextContent('rahul'))
    act(() => { window.dispatchEvent(new CustomEvent('auth:required', { detail: { body: { reason: 'replaced' } } })) })
    await waitFor(() => expect(screen.getByTestId('why')).toHaveTextContent('replaced'))
  })

  it('does no account calls at all when the server runs without accounts', async () => {
    const calls = fakeServer({ mode: 'open', signedIn: false })
    mount()
    await waitFor(() => expect(screen.getByTestId('mode')).toHaveTextContent('open'))
    await act(async () => { await vi.advanceTimersByTimeAsync(35_000) })
    expect(calls).toEqual(['GET /api/auth/mode'])
    expect(screen.getByTestId('admin')).toHaveTextContent('false')            // no admin or developer panels without accounts
  })
})
