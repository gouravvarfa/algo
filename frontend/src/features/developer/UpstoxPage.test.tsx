import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import UpstoxPage from './UpstoxPage'

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status })
const NONE = { state: 'none', api_key: null, redirect_uri: null, user_id: null, user_name: null, connected_at: null, token_expires_at: null, updated_by: null }
const APP_ONLY = { ...NONE, state: 'app_only', api_key: '7c1•••••56', redirect_uri: 'http://localhost:3000/app/dev/upstox', updated_by: 'owner' }
const LIVE = { ...APP_ONLY, state: 'connected', user_id: 'AB1234', user_name: 'Tanisha Rathod', connected_at: '2026-10-26T10:00:00+05:30', token_expires_at: '2026-10-27T03:30:00+05:30' }
const type = (label: RegExp | string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } })
const mount = (url = '/dev/upstox') => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={[url]}><UpstoxPage /></MemoryRouter>
  </QueryClientProvider>)

describe('UpstoxPage', () => {
  afterEach(() => vi.restoreAllMocks())

  it('starts empty: the save button waits for the key and the secret, and the redirect address is filled in', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve(json(200, NONE)))
    mount()
    expect(await screen.findByText('Not set up')).toBeInTheDocument()
    expect(screen.getByLabelText(/redirect address/i)).toHaveValue(`${window.location.origin}/app/dev/upstox`)
    const save = screen.getByRole('button', { name: /save app details/i })
    expect(save).toBeDisabled()
    type(/api key/i, '7c1f3e2a-5b8d'); type(/api secret/i, 's3cr3tvalue99')
    expect(save).toBeEnabled()
    expect(screen.getByLabelText(/api key/i)).toHaveAttribute('type', 'password')
    expect(screen.getByLabelText(/api secret/i)).toHaveAttribute('type', 'password')
    expect(screen.queryByRole('button', { name: /connect with upstox/i })).not.toBeInTheDocument()
  })

  it('saves the app details once, empties the secret fields and only shows the masked key', async () => {
    const puts: Record<string, unknown>[] = []
    let state: unknown = NONE
    vi.spyOn(globalThis, 'fetch').mockImplementation((_u, init) => {
      if (init?.method === 'PUT') { puts.push(JSON.parse(String(init.body))); state = APP_ONLY; return Promise.resolve(json(200, APP_ONLY)) }
      return Promise.resolve(json(200, state))
    })
    mount()
    await screen.findByText('Not set up')
    type(/api key/i, '7c1f3e2a-5b8d'); type(/api secret/i, 's3cr3tvalue99')
    fireEvent.click(screen.getByRole('button', { name: /save app details/i }))
    expect(await screen.findByText('App saved, not logged in')).toBeInTheDocument()
    expect(puts).toEqual([{ api_key: '7c1f3e2a-5b8d', api_secret: 's3cr3tvalue99', redirect_uri: `${window.location.origin}/app/dev/upstox` }])
    expect(screen.getByText('7c1•••••56')).toBeInTheDocument()
    expect(screen.getByLabelText(/api secret/i)).toHaveValue('')
    expect(document.body.textContent).not.toMatch(/s3cr3tvalue99|7c1f3e2a/)
  })

  it('sends the browser to the Upstox login page the server gave', async () => {
    const assign = vi.fn()
    const real = window.location
    Object.defineProperty(window, 'location', { configurable: true, value: { ...real, assign, origin: real.origin, search: '' } })
    try {
      vi.spyOn(globalThis, 'fetch').mockImplementation((url) => Promise.resolve(String(url).endsWith('/login')
        ? json(200, { url: 'https://api.upstox.com/v2/login/authorization/dialog?state=abc' }) : json(200, APP_ONLY)))
      mount()
      fireEvent.click(await screen.findByRole('button', { name: /connect with upstox/i }))
      await waitFor(() => expect(assign).toHaveBeenCalledWith('https://api.upstox.com/v2/login/authorization/dialog?state=abc'))
    } finally {
      Object.defineProperty(window, 'location', { configurable: true, value: real })
    }
  })

  it('coming back from Upstox with a code hands it to the server exactly once and shows Connected', async () => {
    const posts: Record<string, unknown>[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation((url, init) => {
      if (String(url).endsWith('/callback')) { posts.push(JSON.parse(String(init?.body))); return Promise.resolve(json(200, LIVE)) }
      return Promise.resolve(json(200, APP_ONLY))
    })
    mount('/dev/upstox?code=one-time&state=xyz')
    expect(await screen.findByText('Connected', { selector: '.chip' })).toBeInTheDocument()
    expect(posts).toEqual([{ code: 'one-time', state: 'xyz' }])
    expect(screen.getByText(/Tanisha Rathod \(AB1234\)/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /log in again/i })).toBeInTheDocument()
  })

  it('a refused way back shows the server\'s sentence', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => Promise.resolve(String(url).endsWith('/callback')
      ? json(400, { error: 'BAD_STATE', message: 'This Upstox login was not started here or has expired. Press Connect with Upstox again.' }) : json(200, APP_ONLY)))
    mount('/dev/upstox?code=c&state=forged')
    expect(await screen.findByRole('alert')).toHaveTextContent(/not started here or has expired/)
  })

  it('a login the person cancelled on Upstox says so and calls nothing', async () => {
    const f = vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve(json(200, APP_ONLY)))
    mount('/dev/upstox?error=access_denied')
    expect(await screen.findByRole('alert')).toHaveTextContent(/did not log you in/i)
    expect(f.mock.calls.some(([u]) => String(u).endsWith('/callback'))).toBe(false)
  })

  it('shows an ended login as such and offers to connect again', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve(json(200, { ...LIVE, state: 'expired' })))
    mount()
    expect(await screen.findByText('Login ended: connect again')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /connect with upstox/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /today's trades/i })).not.toBeInTheDocument()
  })

  it("lists today's trades and nothing else from them", async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => Promise.resolve(String(url).endsWith('/trades')
      ? json(200, { count: 1, trades: [{ trade_id: 'T1', trading_symbol: 'BANKNIFTY26OCT55000CE', transaction_type: 'BUY', quantity: 30, average_price: 101.5 }] })
      : json(200, LIVE)))
    mount()
    fireEvent.click(await screen.findByRole('button', { name: /show today's trades/i }))
    expect(await screen.findByText('BANKNIFTY26OCT55000CE')).toBeInTheDocument()
    expect(screen.getByText('101.5')).toBeInTheDocument()
  })

  it('asks before removing the details and does nothing if the answer is no', async () => {
    const f = vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve(json(200, LIVE)))
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    mount()
    fireEvent.click(await screen.findByRole('button', { name: /remove details/i }))
    expect(window.confirm).toHaveBeenCalled()
    await waitFor(() => expect(f.mock.calls.every(([, init]) => init?.method !== 'DELETE')).toBe(true))
  })
})
