import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, csrfToken } from './api'
import { ago, dt, lakh, money, n2, num, sign, words } from './format'

describe('format', () => {
  it('groups digits the Indian way and keeps decimals', () => {
    expect(num(1234567.891)).toBe('12,34,567.89')
    expect(num(null)).toBe('–')
    expect(n2(55795.8)).toBe('55,795.8')
  })
  it('shows rupees with a real minus sign and no decimals', () => {
    expect(money(-16739.4)).toBe('−₹16,739')
    expect(money(1500)).toBe('₹1,500')
    expect(money(undefined)).toBe('–')
  })
  it('colours by sign', () => {
    expect([sign(5), sign(-5), sign(0), sign(null)]).toEqual(['pos', 'neg', '', ''])
  })
  it('formats dates, lakhs and words', () => {
    expect(dt('2026-10-21T10:12:00')).toBe('2026-10-21 10:12')
    expect(dt(null)).toBe('–')
    expect([lakh(4_100_000), lakh(12_345_678), lakh(900)]).toEqual(['41.0L', '1.23Cr', '900'])
    expect(words('STOP_LOSS_REVERSAL')).toBe('STOP LOSS REVERSAL')
  })
  it('says how long ago', () => {
    expect(ago(null)).toBe('never')
    expect(ago(1000, 1010)).toBe('10s ago')
    expect(ago(1000, 1000 + 600)).toBe('10m ago')
  })
})

describe('api client', () => {
  afterEach(() => { vi.restoreAllMocks(); document.cookie = 'algo_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT' })

  it('reads the CSRF cookie', () => {
    document.cookie = 'algo_csrf=abc123'
    expect(csrfToken()).toBe('abc123')
  })

  it('sends the CSRF header on writes and not on reads', async () => {
    document.cookie = 'algo_csrf=tok'
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve(new Response('{"ok":true}', { status: 200 })))
    await api('/api/x')
    await api('/api/x', { method: 'POST', json: { a: 1 } })
    const [get, post] = fetchMock.mock.calls
    expect((get[1]!.headers as Record<string, string>)['X-CSRF-Token']).toBeUndefined()
    expect((post[1]!.headers as Record<string, string>)['X-CSRF-Token']).toBe('tok')
    expect(post[1]!.body).toBe('{"a":1}')
  })

  it('turns a server error into one ApiError with the code and message', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ error: 'ALREADY_LOGGED_IN', message: 'Log out there first.', device: 'Chrome' }), { status: 409 }))
    await expect(api('/api/auth/login', { json: {} })).rejects.toMatchObject({ status: 409, code: 'ALREADY_LOGGED_IN', message: 'Log out there first.', body: { device: 'Chrome' } })
  })

  it('announces a 401 so the session can be re-checked', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{"detail":"login required"}', { status: 401 }))
    const seen = vi.fn()
    window.addEventListener('auth:required', seen)
    await expect(api('/api/overview')).rejects.toBeInstanceOf(ApiError)
    window.removeEventListener('auth:required', seen)
    expect(seen).toHaveBeenCalledOnce()
  })

  it('survives a non-JSON error page', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('<html>bad gateway</html>', { status: 502 }))
    await expect(api('/api/x')).rejects.toMatchObject({ status: 502, message: 'HTTP 502' })
  })

  it('reports a dead network as one clear error', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('failed'))
    await expect(api('/api/x')).rejects.toMatchObject({ code: 'NETWORK' })
  })
})

describe('route guard', () => {
  async function renderGuard(session: Record<string, unknown>, need?: string) {
    vi.resetModules()
    vi.doMock('./session', () => ({ useSession: () => session }))
    const { App } = await import('../app/App')
    void need
    render(<QueryClientProvider client={new QueryClient()}><MemoryRouter initialEntries={['/admin/users']}><Routes><Route path="/*" element={<App />} /></Routes></MemoryRouter></QueryClientProvider>)
  }
  afterEach(() => { vi.doUnmock('./session') })

  it('sends a visitor without a session to the login page', async () => {
    await renderGuard({ mode: 'platform', me: null, loading: false, can: () => false, refresh: vi.fn(), logout: vi.fn() })
    expect(await screen.findByText(/Welcome back/i)).toBeInTheDocument()
  })
})
