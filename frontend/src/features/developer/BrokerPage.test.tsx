import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import BrokerPage from './BrokerPage'

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status })
const NONE = { configured: false, source: 'none', client_code: null, connected_at: null, updated_by: null, encrypted: false, feed: null }
const LIVE = { configured: true, source: 'saved', client_code: 'G••••••06', connected_at: '2026-10-26T10:00:00+05:30', updated_by: 'owner', encrypted: true,
  feed: { connected: true, last_tick_age_s: 2.4, reconnects: 0, subscribed: 3, error: null } }
const type = (label: RegExp | string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } })
const fill = () => { type(/api key/i, 'fdizvl3Y'); type(/client code/i, 'G56798406'); type(/^pin/i, '9424'); type(/totp secret/i, '7VQCXK44UJO3742SCNVFKQD2CY') }
const mount = () => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><BrokerPage /></QueryClientProvider>)

describe('BrokerPage', () => {
  afterEach(() => vi.restoreAllMocks())

  it('shows "not connected" and keeps the button off until all four values are typed', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve(json(200, NONE)))
    mount()
    expect(await screen.findByText('Not connected')).toBeInTheDocument()
    const button = screen.getByRole('button', { name: /test and connect/i })
    expect(button).toBeDisabled()
    type(/api key/i, 'fdizvl3Y'); type(/client code/i, 'G56798406'); type(/^pin/i, '9424')
    expect(button).toBeDisabled()
    type(/totp secret/i, '7VQCXK44UJO3742SCNVFKQD2CY')
    expect(button).toBeEnabled()
  })

  it('hides the key, PIN and secret as password fields', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve(json(200, NONE)))
    mount()
    await screen.findByText('Not connected')
    for (const l of [/api key/i, /^pin/i, /totp secret/i]) expect(screen.getByLabelText(l)).toHaveAttribute('type', 'password')
  })

  it('sends the values once, then clears the form and shows only the masked client code', async () => {
    const puts: Record<string, unknown>[] = []
    let state: unknown = NONE
    vi.spyOn(globalThis, 'fetch').mockImplementation((_url, init) => {
      if (init?.method === 'PUT') { puts.push(JSON.parse(String(init.body))); state = LIVE; return Promise.resolve(json(200, LIVE)) }
      return Promise.resolve(json(200, state))
    })
    mount()
    await screen.findByText('Not connected')
    fill()
    fireEvent.click(screen.getByRole('button', { name: /test and connect/i }))
    expect(await screen.findByText('Live data connected')).toBeInTheDocument()
    expect(puts).toEqual([{ api_key: 'fdizvl3Y', client_code: 'G56798406', pin: '9424', totp_secret: '7VQCXK44UJO3742SCNVFKQD2CY' }])
    expect(screen.getByText('G••••••06')).toBeInTheDocument()
    expect(screen.getByLabelText(/api key/i)).toHaveValue('')
    expect(screen.getByLabelText(/totp secret/i)).toHaveValue('')
    expect(document.body.textContent).not.toMatch(/fdizvl3Y|9424|7VQCXK44/)
  })

  it("shows Angel One's refusal, keeps what was typed so it can be fixed, and the old status stays", async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((_url, init) => Promise.resolve(init?.method === 'PUT'
      ? json(400, { error: 'BROKER_REJECTED', message: 'Angel One refused these details: Invalid totp. Nothing was saved.' })
      : json(200, LIVE)))
    mount()
    await screen.findByText('Live data connected')
    fill()
    fireEvent.click(screen.getByRole('button', { name: /test and connect/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/Invalid totp. Nothing was saved/)
    expect(screen.getByLabelText(/^pin/i)).toHaveValue('9424')
    expect(screen.getByText('Live data connected')).toBeInTheDocument()
  })

  it('tells a saved-but-offline feed apart from a live one and shows the feed message', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve(json(200, { ...LIVE, feed: { ...LIVE.feed, connected: false, error: 'AngelOneApiError: Invalid Token' } })))
    mount()
    expect(await screen.findByText(/not connected/i, { selector: '.chip' })).toHaveTextContent(/Details saved, live feed not connected/)
    expect(screen.getByText(/Invalid Token/)).toBeInTheDocument()
  })

  it('asks before removing the saved details, and does nothing if the answer is no', async () => {
    const f = vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve(json(200, LIVE)))
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    mount()
    fireEvent.click(await screen.findByRole('button', { name: /remove saved details/i }))
    expect(window.confirm).toHaveBeenCalled()
    await waitFor(() => expect(f.mock.calls.every(([, init]) => init?.method !== 'DELETE')).toBe(true))
  })
})
