import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { get, post } from '../../lib/api'
import { useDataSource } from '../../lib/datasource'
import { dt, money, n2, num, sign, words } from '../../lib/format'
import type { IndexQuote, JournalEvent, Overview, Snapshot, Trade } from '../../lib/types'
import { TradeChart } from './TradeChart'
import './topbottom.css'

const SINCE = '2026-01-01T00:00:00+05:30'      // history starts on 1 Jan 2026

function useInstrumentId() {
  const ds = useDataSource()
  const qc = useQueryClient()
  const ov = useQuery({
    queryKey: ['tb', ds.key, 'overview'],
    queryFn: ds.overview,
    refetchInterval: (q) => (q.state.data?.market_open ? 10_000 : 60_000),
  })
  const found = ov.data?.items.find((i) => i.instrument.symbol === 'BANKNIFTY' && i.instrument.timeframe === '1D')
  const add = useMutation({
    mutationFn: () => post('/api/instruments', { symbol: 'BANKNIFTY', instrument_type: 'FUTURE', timeframe: '1D', size: 1, lookback_days: Math.ceil((Date.now() - new Date(SINCE).getTime()) / 864e5) }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ['tb', ds.key, 'overview'] }) },
  })
  // a person's own page adds the BankNifty futures strategy the first time it is opened; an admin looking at someone else never creates anything
  const needsAdd = ov.isSuccess && !found && !ds.readOnly
  return { ov, found, add, needsAdd }
}

export default function TopBottomPage() {
  const ds = useDataSource()
  const qc = useQueryClient()
  const { ov, found, add, needsAdd } = useInstrumentId()
  const [kind, setKind] = useState<'line' | 'candle'>(() => { try { return localStorage.getItem('dash.ctype') === 'candle' ? 'candle' : 'line' } catch { return 'line' } })
  const [fit, setFit] = useState(0)
  const [showAll, setShowAll] = useState(false)

  if (needsAdd && !add.isPending && !add.isError && !add.isSuccess) add.mutate()

  const snapQ = useQuery({
    queryKey: ['tb', ds.key, 'snap', found?.instrument.id],
    enabled: !!found,
    queryFn: () => ds.instrument(found!.instrument.id),
    refetchInterval: (q) => (ov.data?.market_open ? (q.state.data?.feed?.live ? 2000 : 15_000) : 60_000),
    placeholderData: (prev) => prev,
  })
  const idx = useQuery({ queryKey: ['index', 'BANKNIFTY'], queryFn: () => get<IndexQuote>('/api/index/BANKNIFTY'), refetchInterval: 2000, retry: false })
  const jq = useQuery({ queryKey: ['tb', ds.key, 'journal'], queryFn: () => ds.journal(300), refetchInterval: 30_000 })
  const snap = snapQ.data
  const refresh = useMutation({ mutationFn: () => post('/api/refresh'), onSuccess: () => { void qc.invalidateQueries({ queryKey: ['tb', ds.key] }) } })

  const setKindSaved = (k: 'line' | 'candle') => { setKind(k); try { localStorage.setItem('dash.ctype', k) } catch { /* ignore */ } }

  if (ov.isError) return <div className="page"><div className="alert">Could not load: {(ov.error as Error).message}</div></div>
  if (ds.readOnly && ov.isSuccess && !found) return <div className="page"><div className="card"><h2>No strategy yet</h2><p className="sub" style={{ marginTop: 6 }}>This user has not opened Top Bottom yet, so no strategy is running for them.</p></div></div>
  if (!snap) return <div className="page"><div className="skeleton" style={{ height: 120 }} /><div className="skeleton" style={{ height: 420 }} />{(add.isError || snapQ.isError) && <div className="alert">Could not load BankNifty: {((add.error ?? snapQ.error) as Error)?.message}. Retrying.</div>}</div>

  return <Content snap={snap} ov={ov.data!} idx={idx.data} journal={jq.data} kind={kind} setKind={setKindSaved} fit={fit} onFit={() => setFit((f) => f + 1)}
    showAll={showAll} onToggleAll={() => setShowAll((s) => !s)} onRefresh={() => refresh.mutate()} refreshing={refresh.isPending} readOnly={ds.readOnly} updated={snapQ.dataUpdatedAt} />
}

interface CP {
  snap: Snapshot; ov: Overview; idx?: IndexQuote; journal?: JournalEvent[]; kind: 'line' | 'candle'; setKind: (k: 'line' | 'candle') => void; fit: number; onFit: () => void
  showAll: boolean; onToggleAll: () => void; onRefresh: () => void; refreshing: boolean; readOnly: boolean; updated: number
}

function Content({ snap: s, ov, idx, journal, kind, setKind, fit, onFit, showAll, onToggleAll, onRefresh, refreshing, readOnly, updated }: CP) {
  const p = s.position, st = s.stats, inst = s.instrument
  const closed = useMemo(() => s.trades.filter((t) => t.status !== 'OPEN'), [s.trades])
  const wins = closed.filter((t) => (t.pnl_amount ?? 0) > 0)
  const pts = closed.reduce((a, t) => a + (t.pnl_points ?? 0), 0)
  const net = closed.reduce((a, t) => a + (t.pnl_amount ?? 0), 0)
  const rows = showAll ? s.trades : s.trades.slice(0, 8)
  const f = s.feed
  const live = ov.market_open ? (f?.live ? `LIVE · ticks (${f.age_s != null ? f.age_s.toFixed(1) : '–'} s)` : 'LIVE · polling (tick feed down)') : 'Market closed · last close'
  const up = (idx?.change ?? 0) >= 0
  const lastRoll = s.rolls?.length ? s.rolls[s.rolls.length - 1] : null

  return (
    <div className="page">
      <div className="titlerow">
        <h1>Top Bottom</h1>
        <span className="chip">BankNifty · daily</span>
        {idx?.price != null && (
          <span className="idxchip" title={`BANKNIFTY index · previous close ${num(idx.prev_close)} · source: ${idx.source}`} style={{ opacity: idx.live || idx.age_s == null ? 1 : 0.7 }}>
            <small>BANKNIFTY</small>
            <b className={`num ${idx.change == null ? '' : up ? 'pos' : 'neg'}`}>{num(idx.price)}</b>
            {idx.change != null && <span className={`num ${up ? 'pos' : 'neg'}`}>{up ? '▲ +' : '▼ −'}{num(Math.abs(idx.change))} ({up ? '+' : '−'}{Math.abs(idx.pct ?? 0).toFixed(2)}%)</span>}
          </span>
        )}
        <span className={'livepill' + (ov.market_open ? ' on' : '')}><i />{live}</span>
        <span className="spacer" />
        <span className="sub">Updated {updated ? new Date(updated).toLocaleTimeString() : '–'}</span>
        {!readOnly && <button className="btn sm" onClick={onRefresh} disabled={refreshing}>{refreshing ? 'Refreshing…' : 'Refresh'}</button>}
      </div>

      <div className="tbkpis">
        <div className="stat"><div className="k">BankNifty fut</div><div className="v">{num(s.ltp)}</div><div className="s">{inst.trading_symbol}</div></div>
        <div className={`stat ${p ? (p.direction === 'BUY' ? 'up' : 'down') : ''}`}><div className="k">Position</div><div className={`v ${p ? (p.direction === 'BUY' ? 'pos' : 'neg') : ''}`}>{p ? p.direction : 'FLAT'}</div><div className="s">{p ? `entry ${num(p.entry_price)}` : s.waiting ? 'waiting for new System Point' : 'no open position'}</div></div>
        <div className="stat"><div className="k">Active stop</div><div className="v">{p ? num(p.active_stop) : '–'}</div><div className="s">{p ? (p.breakeven ? 'breakeven SL (2% done)' : 'structural / trailing') : ''}</div></div>
        <div className={`stat ${p ? (p.pnl_amount >= 0 ? 'up' : 'down') : ''}`}><div className="k">Open P&amp;L</div><div className={`v ${sign(p?.pnl_amount)}`}>{p ? money(p.pnl_amount) : '–'}</div><div className="s">{p ? `${num(p.pnl_points)} pts · ${p.pnl_pct}%` : ''}</div></div>
        <div className="stat"><div className="k">Latest top</div><div className="v">{n2(s.latest_top)}</div><div className="s">confirmed swing</div></div>
        <div className="stat"><div className="k">Latest bottom</div><div className="v">{n2(s.latest_bottom)}</div><div className="s">confirmed swing</div></div>
        <div className="stat"><div className="k">Live trades</div><div className="v">{st.live_trades} · {st.live_win_rate == null ? '–' : `${st.live_win_rate}%`}</div><div className="s">realized <span className={sign(st.live_realized)}>{money(st.live_realized)}</span></div></div>
      </div>

      <div className="tbgrid">
        <div className="card chartcard">
          <div className="card-head">
            <h2>{inst.trading_symbol} · {inst.timeframe} · qty {inst.quantity}</h2>

            <span className="spacer" />
            <span className="seg"><button className={kind === 'line' ? 'on' : ''} onClick={() => setKind('line')}>Line</button><button className={kind === 'candle' ? 'on' : ''} onClick={() => setKind('candle')}>Candles</button></span>
            <button className="btn sm" onClick={onFit}>Fit</button>
          </div>
          <span className="legend"><i style={{ background: 'var(--accent)' }} />Close <i className="dash" style={{ color: 'var(--success)' }} />Entry <i className="dash" style={{ color: 'var(--danger)' }} />Stop-loss ▲ BUY ▼ SELL <b style={{ color: 'var(--warning)' }}>●</b> latest TOP / BOTTOM</span>
          <TradeChart snap={s} kind={kind} fitSignal={fit} />
          {s.engine_note && <p className={`note ${s.engine_warning ? 'neg' : ''}`} style={s.engine_warning ? { fontWeight: 650 } : undefined}>{s.engine_warning ? '⚠ ' : '✔ '}{s.engine_note}</p>}
          <p className="note">Last confirmed bar {s.last_confirmed_bar.slice(0, 10)}. The rightmost point is today&apos;s forming candle (Angel One) and can still change; signals use confirmed closes and the 15-minute gap protocol.</p>
        </div>

        <div className="side">
          <div className="card">
            <div className="card-head"><h2>Strategy summary</h2></div>
            <dl className="kv2">
              <dt>Strategy</dt><dd>Top-Bottom</dd>
              <dt>Instrument</dt><dd>{inst.trading_symbol}</dd>
              <dt>Active contract</dt><dd>{inst.trading_symbol} · exp {inst.expiry}</dd>
              {lastRoll && <><dt>Last roll</dt><dd>{dt(lastRoll.time)}<br /><span className="sub">{lastRoll.from} @ {num(lastRoll.old_price)} → {lastRoll.to} @ {num(lastRoll.new_price)}</span></dd></>}
              <dt>Quantity</dt><dd>{inst.lots || 1} lot · {inst.quantity}</dd>
              <dt>State</dt><dd className={p ? (p.direction === 'BUY' ? 'pos' : 'neg') : ''}>{p ? p.direction : s.waiting ? 'Flat · waiting' : 'Flat'}</dd>
              <dt>Entry</dt><dd>{p ? <>{num(p.entry_price)}<br /><span className="sub">{dt(p.entry_date)}</span></> : '–'}</dd>
              <dt>Initial SL</dt><dd>{p ? num(p.initial_sl) : '–'}</dd>
              <dt>Active stop</dt><dd>{p ? num(p.active_stop) : '–'}</dd>
              <dt>2% breakeven</dt><dd>{p ? (p.breakeven ? 'reached' : 'not yet') : '–'}</dd>
              <dt>Gap protocol</dt><dd>{p?.gap_state ? words(p.gap_state) : '–'}</dd>
              <dt>Protective SL today</dt><dd>{p?.protective_sl ? num(p.protective_sl) : '–'}</dd>
            </dl>
          </div>
          <div className="card">
            <div className="card-head"><h2>Key stats</h2><span className="chip muted">all closed trades</span></div>
            <dl className="kv2">
              <dt>Closed trades</dt><dd>{closed.length}</dd>
              <dt>Win rate</dt><dd>{closed.length ? `${(wins.length / closed.length * 100).toFixed(1)}%` : '–'}</dd>
              <dt>Net points</dt><dd className={sign(pts)}>{num(pts)}</dd>
              <dt>Net P&amp;L (qty {inst.quantity})</dt><dd className={sign(net)}>{money(net)}</dd>
              <dt>Live realized</dt><dd className={sign(st.live_realized)}>{money(st.live_realized)}</dd>
              <dt>Live trades / win</dt><dd>{st.live_trades} / {st.live_win_rate == null ? '–' : `${st.live_win_rate}%`}</dd>
              <dt>History trades</dt><dd>{st.replay_trades}</dd>
              <dt>History realized</dt><dd className={sign(st.replay_realized)}>{money(st.replay_realized)}</dd>
            </dl>
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-head"><h2>Recent trade legs</h2><span className="spacer" /><button className="btn sm" hidden={s.trades.length <= 8} onClick={onToggleAll}>{showAll ? 'Show recent' : `Show all (${s.trades.length})`}</button></div>
        <div className="scroll"><TradesTable rows={rows} /></div>
        <p className="note">Greyed rows are history from before this instrument was added: not counted in live P&amp;L.</p>
      </div>

      <div className="card">
        <div className="card-head"><h2>Live event log</h2></div>
        <p className="note" style={{ marginTop: 0, marginBottom: 8 }}>Every ENTRY, EXIT, stop-to-breakeven and gap-protocol event of the live trades, written the moment the engine first sees it. Rows marked <b>backfill</b> were already in history when the log started.</p>
        <div className="scroll" style={{ maxHeight: 360, overflowY: 'auto' }}><JournalTable events={journal} /></div>
      </div>
    </div>
  )
}

function TradesTable({ rows }: { rows: Trade[] }) {
  return (
    <table>
      <thead><tr><th>#</th><th className="l">Side</th><th className="l">Contract</th><th className="l">Entry time</th><th>Entry</th><th>Stop</th><th className="l">Exit time</th><th>Exit</th><th>Points</th><th>P&amp;L</th><th className="l">Status</th></tr></thead>
      <tbody>
        {rows.map((t) => (
          <tr key={`${t.n}-${t.entry_date}`} className={t.live ? '' : 'replay'}>
            <td className="num">{t.n}</td>
            <td className="l"><span className={`badge ${t.direction}`}>{t.direction}</span></td>
            <td className="l">{(t.contract ?? '').replace(/^BANKNIFTY|FUT$/g, '')}{t.rolled_in && <span className="chip muted" style={{ marginLeft: 6 }} title="Continued from the previous contract at its cutoff">rolled in</span>}</td>
            <td className="l">{dt(t.entry_date)}</td><td className="num">{num(t.entry_price)}</td><td className="num">{num(t.active_stop)}</td>
            <td className="l">{dt(t.exit_date)}</td><td className="num">{num(t.exit_price)}</td>
            <td className={`num ${sign(t.pnl_points)}`}>{num(t.pnl_points)}</td><td className={`num ${sign(t.pnl_amount)}`}>{money(t.pnl_amount)}</td>
            <td className="l">{t.status}{t.breakeven ? ' · 2% → BE' : ''}{t.gap_state ? ` · ${words(t.gap_state)}` : ''}{t.exit_reason ? ` · ${words(t.exit_reason)}` : ''}</td>
          </tr>
        ))}
        {!rows.length && <tr><td colSpan={11} className="empty">No trades yet.</td></tr>}
      </tbody>
    </table>
  )
}

function JournalTable({ events }: { events?: JournalEvent[] }) {
  if (!events) return <div className="skeleton" style={{ height: 120 }} />
  return (
    <table>
      <thead><tr><th className="l">Recorded at</th><th className="l">Event</th><th className="l">Instrument</th><th className="l">Side</th><th>Entry</th><th className="l">Detail</th><th>P&amp;L</th></tr></thead>
      <tbody>
        {events.map((e, i) => (
          <tr key={i}>
            <td className="l">{e.at.replace('T', ' ').slice(0, 19)}{e.backfill && <span className="chip muted" style={{ marginLeft: 6 }}>backfill</span>}</td>
            <td className="l"><span className="chip">{words(e.event)}</span></td>
            <td className="l">{e.trading_symbol ?? e.symbol}</td><td className="l">{e.direction}</td><td className="num">{n2(e.entry_price)}</td>
            <td className="l">{e.event === 'ENTRY' ? `SL ${n2(e.initial_sl)}` : e.event === 'EXIT' ? `${n2(e.exit_price)} · ${words(e.reason)} · ${dt(e.exit_date)}` : e.event === 'GAP' ? `${e.gap_state}${e.protective_sl ? ` · protective SL ${n2(e.protective_sl)}` : ''}` : `stop → ${n2(e.active_stop)}`}</td>
            <td className="num">{e.event === 'EXIT' && e.pnl_amount != null ? <span className={sign(e.pnl_amount)}>₹{n2(e.pnl_amount)}</span> : ''}</td>
          </tr>
        ))}
        {!events.length && <tr><td colSpan={7} className="empty">No live events yet. They appear here as soon as the paper trader enters or exits.</td></tr>}
      </tbody>
    </table>
  )
}
