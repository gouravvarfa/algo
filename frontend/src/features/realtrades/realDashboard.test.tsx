import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Snapshot } from '../../lib/types'
import { RealSwitch } from '../topbottom/RealSwitch'
import { RealEventLog, realSnap } from '../topbottom/TopBottomPage'
import { realInstrumentIds, type RealData } from './RealTradesPage'

const inst = (id: string, real_on: boolean, extra = {}) => ({ instrument_id: id, symbol: id.toUpperCase(), real_on, position: null, stop_target: null, halted: null, ...extra })
const trade = (id: string) => ({ instrument_id: id, symbol: id, direction: 'BUY', qty: 10, contract: 'X', entry_price: 1, entry_at: 1, exit_price: null, exit_at: null, exit_by: null, pnl_points: null, pnl_amount: null, status: 'OPEN' })

describe('the Real Trades dashboard shows only instruments with real money on them', () => {
  it('armed, holding, halted or traded: yes; armed once and switched off with no trade: no', () => {
    const d: RealData = {
      instruments: [inst('goldten', true), inst('silvermic', false), inst('dlf', false, { position: { side: 'BUY', qty: 950, contract: 'DLF', entry_price: 700 } }), inst('canbk', false, { halted: 'mismatch' }), inst('sona', false)],
      trades: [trade('sona')], realised: 0, closed: 0, wins: 0, open: 1,
    }
    expect(realInstrumentIds(d)).toEqual(['goldten', 'dlf', 'canbk', 'sona'])
  })
})

describe('the real view never shows a paper number', () => {
  const paper = {
    instrument: { quantity: 10, lots: 1, lot_size: 10, point_value: 1 }, ltp: 137354, candles: [{ time: 1000, open: 1, high: 1, low: 1, close: 1 }],
    position: { direction: 'SELL', entry_price: 142303, active_stop: 142303 }, trades: [{ n: 1, direction: 'SELL', status: 'OPEN', live: true }, { n: 2, direction: 'BUY', status: 'CLOSED', live: false }],
    markers: [{ time: 1000, text: 'SELL 142303' }], stats: { live_realized: 5000, live_trades: 7, live_win_rate: 60, replay_trades: 40, replay_realized: 90000 }, waiting: false,
  } as unknown as Snapshot
  it('GOLDTEN just armed, nothing filled yet: no paper position, no paper trade, no paper marker, no history', () => {
    const r = realSnap(paper, { trades: [], real_on: true, since: 1, state: null })
    expect(r.position).toBeNull()
    expect(r.trades).toEqual([])
    expect(r.markers).toEqual([])
    expect(r.stats.live_realized).toBe(0)
    expect(r.stats.replay_trades).toBe(0)
    expect(r.stats.replay_realized).toBe(0)
  })
})

describe('RealEventLog', () => {
  it('lists the real order steps newest first', () => {
    render(<RealEventLog events={[{ at: '2026-11-16 09:00:05', text: 'ENTRY sent' }, { at: '2026-11-16 09:00:07', text: 'ENTRY FILLED' }]} />)
    const cells = screen.getAllByRole('row').slice(1).map((r) => r.textContent)
    expect(cells[0]).toContain('ENTRY FILLED')
    expect(cells[1]).toContain('ENTRY sent')
  })
  it('says what happens next when nothing has been sent yet', () => {
    render(<RealEventLog events={undefined} />)
    expect(screen.getByText(/first one goes out at the next new System Point/i)).toBeInTheDocument()
  })
})

describe('RealSwitch while the account is on paper', () => {
  afterEach(() => vi.restoreAllMocks())
  const answer = (account_mode: string, real_on: boolean) => (url: string) => {
    const body = url.includes('/api/me/broker/instruments')
      ? { account_mode, real_allowed: true, max_lots: 5, blockers: [], instruments: [{ instrument_id: 'g', symbol: 'GOLDTEN', real_on, real_effective: real_on && account_mode === 'real', real_lots: 1, lot_size: 10, paper_lots: 1 }] }
      : url.includes('/margin') ? { connected: false } : { instruments: [] }
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
  }
  const mount = () => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><RealSwitch instrumentId="g" symbol="GOLDTEN" /></MemoryRouter></QueryClientProvider>)

  it('an armed instrument stays armed and says it is paused, and can still be switched off', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((u) => answer('paper', true)(String(u)))
    mount()
    expect(await screen.findByText(/REAL armed · paused/)).toBeInTheDocument()
    expect(screen.getByText(/nothing is sent now/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Switch to paper' })).toBeEnabled()
  })
  it('an instrument that is not armed cannot be turned on while the account is on paper', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((u) => answer('paper', false)(String(u)))
    mount()
    expect(await screen.findByText(/Real trading is off/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Turn REAL on' })).toBeDisabled()
  })
  it('on REAL it is ON', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((u) => answer('real', true)(String(u)))
    mount()
    expect(await screen.findByText(/REAL trading is ON/)).toBeInTheDocument()
  })
})
