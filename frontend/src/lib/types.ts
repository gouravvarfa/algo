/** Shapes of the server payloads the pages read. Only the fields the UI uses are listed; extra fields are tolerated. */
export type Role = 'user' | 'admin' | 'developer'

export interface Me {
  user: { id: string; username: string; name: string; role: Role; owner: boolean }
  permissions: string[]
  session: { device: string; started_at: string; last_active: string }
  csrf_token: string
}

export interface Trade {
  n: number
  direction: 'BUY' | 'SELL'
  status: string
  live: boolean
  entry_date: string
  entry_price: number
  active_stop: number
  initial_sl: number
  exit_date: string | null
  exit_price: number | null
  exit_reason: string | null
  pnl_points: number | null
  pnl_amount: number | null
  pnl_pct: number | null
  breakeven: boolean
  gap_state: string | null
  contract?: string
  rolled_in?: boolean
}

export interface Position {
  direction: 'BUY' | 'SELL'
  entry_price: number
  entry_date: string
  active_stop: number
  initial_sl: number
  breakeven: boolean
  gap_state: string | null
  protective_sl: number | null
  pnl_amount: number
  pnl_points: number
  pnl_pct: number
}

export interface Marker {
  time: number
  position: 'aboveBar' | 'belowBar' | 'inBar'
  shape: string
  color: string
  text: string
}

export interface Snapshot {
  instrument: { id: string; symbol: string; trading_symbol: string; timeframe: string; quantity: number; lots?: number; expiry: string; token?: string; exch_seg?: string }
  ltp: number
  last_confirmed_bar: string
  updated_at: string
  latest_top: number | null
  latest_bottom: number | null
  position: Position | null
  waiting: boolean
  stats: {
    live_trades: number
    live_wins: number
    live_win_rate: number | null
    live_realized: number
    unrealized: number
    replay_trades: number
    replay_realized: number
  }
  engine_note: string
  engine_warning: boolean
  trades: Trade[]
  candles: { time: number; open: number; high: number; low: number; close: number }[]
  feed: { live: boolean; age_s: number | null; connected: boolean }
  rolls?: { time: string; from: string; to: string; old_price: number; new_price: number }[]
  system_points?: { time: number; kind: 'TOP' | 'BOTTOM'; price: number }[]
  markers: Marker[]
}

export interface OverviewItem {
  instrument: Snapshot['instrument'] & { from_date: string }
  error: string | null
  ltp: number | null
  position: Position | null
  stats: Snapshot['stats'] | null
}

export interface Overview {
  feed: { connected: boolean; last_tick_age_s: number | null; reconnects: number; subscribed: number; error: string | null }
  market_open: boolean
  last_tick: string | null
  items: OverviewItem[]
  totals: Record<string, number | null>
  viewing?: { id: string; username: string; name: string; role: Role }
  summary?: Record<string, unknown>
}

export interface JournalEvent {
  at: string
  backfill?: boolean
  event: string
  trading_symbol?: string
  symbol?: string
  direction: string
  entry_price: number
  initial_sl?: number
  active_stop?: number
  exit_price?: number
  exit_date?: string
  reason?: string
  pnl_amount?: number
  gap_state?: string
  protective_sl?: number
}

export interface IndexQuote {
  symbol: string
  price: number | null
  prev_close: number | null
  change: number | null
  pct: number | null
  live: boolean
  age_s: number | null
  source: string
}
