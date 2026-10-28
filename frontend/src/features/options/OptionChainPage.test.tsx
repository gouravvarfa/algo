import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import OptionChainPage from './OptionChainPage'

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status })
const server = (dataSource?: string) => vi.spyOn(globalThis, 'fetch').mockImplementation((u) => {
  const url = String(u)
  if (url.includes('/api/overview')) return Promise.resolve(json(200, dataSource ? { data_source: dataSource } : {}))
  if (url.includes('/expiries')) return Promise.resolve(json(200, { symbol: 'BANKNIFTY', expiries: ['23NOV2026'] }))
  return Promise.resolve(json(200, { days: [], symbols: [] }))
})
const mount = () => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><OptionChainPage /></QueryClientProvider>)

describe('OptionChainPage', () => {
  afterEach(() => vi.restoreAllMocks())

  it('names the data source the server runs on', async () => {
    server('Upstox')
    mount()
    expect(await screen.findByRole('heading', { name: 'Live option chain (Upstox)' })).toBeInTheDocument()
  })

  it('says Angel One when the server says so, and while it does not know yet', async () => {
    server('Angel One')
    mount()
    expect(await screen.findByRole('heading', { name: 'Live option chain (Angel One)' })).toBeInTheDocument()
  })

  it('falls back to Angel One for an older server that sends no data source', async () => {
    server()
    mount()
    expect(await screen.findByRole('heading', { name: 'Live option chain (Angel One)' })).toBeInTheDocument()
  })
})
