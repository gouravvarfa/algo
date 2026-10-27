import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { get, post, put } from '../../lib/api'
import type { DataSource } from '../../lib/datasource'
import { dmy, n0, num } from '../../lib/format'
import { useToast } from '../../lib/toast'
import { PRESET_RULES, planPreset, type Chain, type Margin, type Mark, type SimPos, type Sym, type Valued, type WhatIf, type Opt, type Side } from './sim'

const SYMS: Sym[] = ['NIFTY', 'BANKNIFTY']
const ls = {
  get: (k: string): string | null => { try { return localStorage.getItem(k) } catch { return null } },
  set: (k: string, v: string) => { try { localStorage.setItem(k, v) } catch { /* private mode */ } },
}
const legacyKey = (s: Sym) => 'sim.positions.v1' + (s === 'NIFTY' ? '' : '.' + s)       // positions used to live in the browser

const legBody = (p: SimPos, mult: number) => ({ side: p.side, opt: p.opt, strike: p.strike, expiry: p.expiry, lots: p.lots * mult, lot_size: p.lot_size, entry: p.entry, entry_date: p.entry_date })

interface FetchArgs { sym: Sym; date: string; tm: string | null; intra: boolean; live: boolean; expiry: string | null }
async function fetchChain(a: FetchArgs): Promise<Chain> {
  const ex = a.expiry ? `&expiry=${a.expiry}` : ''
  const url = a.live ? `/api/options/live/chain?symbol=${a.sym}${ex}`
    : a.intra ? `/api/options/intraday/chain?symbol=${a.sym}&date=${a.date}&time=${a.tm}${ex}`
      : `/api/options/sim/chain?symbol=${a.sym}&date=${a.date}${ex}`
  const c = await get<Chain>(url)
  return { ...c, mode: c.mode ?? 'eod' }
}

/** Everything the Options Simulator does, in one place: the time machine (dates, minutes, playback), the chain (EOD, 1-minute or LIVE ticks),
 * positions (stored on the server per user), valuation, stop-losses, margin and the Black-Scholes what-if. Pages only draw this state. */
export function useSimulator(ds: DataSource) {
  const toast = useToast()
  const readOnly = ds.readOnly

  const [sym, setSymState] = useState<Sym>(() => { const s = ls.get('sim.sym') as Sym | null; return s && SYMS.includes(s) ? s : 'NIFTY' })
  const [dates, setDates] = useState<string[]>([])
  const [eodSet, setEodSet] = useState<Set<string>>(new Set())
  const [intraSet, setIntraSet] = useState<Set<string>>(new Set())
  const [idx, setIdx] = useState(-1)
  const [tm, setTm] = useState<string | null>(null)
  const [mins, setMins] = useState<string[]>([])
  const [mode, setModeState] = useState<'1m' | 'eod'>('1m')
  const [initError, setInitError] = useState('')
  const minCache = useRef(new Map<string, string[]>())

  const [chain, setChain] = useState<Chain | null>(null)
  const [chainError, setChainError] = useState('')
  const [loading, setLoading] = useState(false)
  const [curExp, setCurExp] = useState<string | null>(null)
  const [expPinned, setExpPinned] = useState(false)
  const [live, setLiveState] = useState(false)
  const [prevSpot, setPrevSpot] = useState<number | null>(null)
  const spotCache = useRef(new Map<string, number | null>())

  const [positions, setPositions] = useState<SimPos[]>([])
  const [posLoaded, setPosLoaded] = useState(false)
  const [valued, setValued] = useState<Record<string, Valued>>({})
  const [mark, setMark] = useState<Mark | null>(null)
  const [margin, setMargin] = useState<Margin | null>(null)
  const marginKey = useRef('')
  const [wi, setWi] = useState<WhatIf | null>(null)
  const [wiDate, setWiDate] = useState('')
  const [wiIv, setWiIv] = useState(0)
  const [wiSp, setWiSp] = useState(0)

  const [mult, setMultState] = useState(() => Math.max(1, Math.min(100, parseInt(ls.get('sim.mult') ?? '1', 10) || 1)))
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(1)

  const useIntra = idx >= 0 && intraSet.has(dates[idx]) && (mode === '1m' || !eodSet.has(dates[idx]))
  const date = idx >= 0 ? dates[idx] : ''

  // ---------- the time machine
  const goTo = useCallback(async (i: number, atMinute?: string | 'END' | null) => {
    if (i < 0 || i >= dates.length) return
    const d = dates[i]
    let m: string[] = []
    if (intraSet.has(d)) {
      if (!minCache.current.has(d)) {
        try { minCache.current.set(d, (await get<{ minutes: string[] }>(`/api/options/intraday/minutes?symbol=${sym}&date=${d}`)).minutes) } catch { minCache.current.set(d, []) }
      }
      m = minCache.current.get(d) ?? []
    }
    setMins(m)
    setTm(m.length ? (atMinute === 'END' ? m[m.length - 1] : atMinute ?? (tm && m.includes(tm) ? tm : m[0])) : null)
    setIdx(i)
  }, [dates, intraSet, sym, tm])

  useEffect(() => {          // load the list of days whenever the index changes
    let alive = true
    setInitError(''); setChain(null); setIdx(-1); setDates([]); setPosLoaded(false)
    minCache.current.clear(); spotCache.current.clear(); setCurExp(null); setExpPinned(false); setTm(null)
    ;(async () => {
      try {
        const eod = (await get<{ dates: string[] }>(`/api/options/sim/dates?symbol=${sym}`)).dates
        let intra: string[] = []
        try { intra = (await get<{ days: { date: string }[] }>(`/api/options/intraday/days?symbol=${sym}`)).days.map((d) => d.date) } catch { /* no 1-minute data */ }
        if (!alive) return
        const all = [...new Set([...eod, ...intra])].sort()
        setEodSet(new Set(eod)); setIntraSet(new Set(intra)); setDates(all)
        if (!all.length) { setInitError(`No ${sym} data loaded. Load data from the Option Chain page.`); return }
        const last = all.length - 1
        let m: string[] = []
        if (intra.includes(all[last])) {
          try { m = (await get<{ minutes: string[] }>(`/api/options/intraday/minutes?symbol=${sym}&date=${all[last]}`)).minutes } catch { m = [] }
          minCache.current.set(all[last], m)
        }
        setMins(m); setTm(m.length ? m[0] : null); setIdx(last)
      } catch (e) { if (alive) setInitError(`Could not load dates: ${(e as Error).message}`) }
    })()
    return () => { alive = false }
  }, [sym])

  // ---------- positions: stored on the server per user (an admin's view-as reads the user's, read-only)
  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const r = await ds.simPositions(sym)
        let list = (r.positions ?? []) as SimPos[]
        if (!readOnly && !list.length) {                 // one-time import of positions saved in this browser before they lived on the server
          try {
            const old = JSON.parse(ls.get(legacyKey(sym)) ?? '[]') as SimPos[]
            if (old.length) { list = old; await put(`/api/me/sim-positions?symbol=${sym}`, { positions: old }); ls.set(legacyKey(sym), '[]') }
          } catch { /* ignore a corrupt old value */ }
        }
        if (alive) { setPositions(list); setPosLoaded(true) }
      } catch { if (alive) setPosLoaded(true) }
    }
    void load()
    const id = readOnly ? window.setInterval(() => { void load() }, 5000) : undefined
    return () => { alive = false; if (id) window.clearInterval(id) }
  }, [sym, ds, readOnly])

  const savedOnce = useRef(false)
  useEffect(() => {
    if (readOnly || !posLoaded) return
    if (!savedOnce.current) { savedOnce.current = true; return }       // the load itself is not a change
    const t = window.setTimeout(() => { put(`/api/me/sim-positions?symbol=${sym}`, { positions }).catch(() => toast('Could not save positions on the server')) }, 400)
    return () => window.clearTimeout(t)
  }, [positions]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { savedOnce.current = false }, [sym])

  // ---------- LIVE preference (kept per user on the server)
  useEffect(() => {
    if (readOnly) return
    get<{ settings: Record<string, unknown> }>('/api/me/settings').then((r) => setLiveState(r.settings['sim.live'] === true)).catch(() => { /* default off */ })
  }, [readOnly])
  const setLive = useCallback((on: boolean) => {
    setLiveState(on); setPlaying(false); setExpPinned(false)
    if (!readOnly) put('/api/me/settings/sim.live', { value: on }).catch(() => undefined)
    if (on) toast('LIVE: chain from Angel One ticks, updates every second. Paper only.')
  }, [readOnly, toast])

  // ---------- chain
  const loadTok = useRef(0)
  const loadChain = async (silent: boolean) => {
    if (idx < 0) return
    const tok = ++loadTok.current
    if (!silent) setLoading(true)
    try {
      const c = await fetchChain({ sym, date, tm, intra: useIntra, live, expiry: expPinned ? curExp : null })
      if (tok !== loadTok.current) return
      if (expPinned && c.expiry !== curExp) { setExpPinned(false); toast(`Expiry ${dmy(curExp!)} is not listed on ${dmy(date)}; showing the nearest expiry`) }
      if (c.mode !== 'intraday' && c.mode !== 'live') spotCache.current.set(date, c.underlying)
      setChain(c); setCurExp(c.expiry); setChainError('')
    } catch (e) {
      if (tok !== loadTok.current) return
      if (silent) return                                   // keep the last live chain on a missed refresh
      setChain(null); setChainError(`${live ? 'Live chain unavailable' : `No data for ${dmy(date)}`}: ${(e as Error).message}`)
    } finally { if (tok === loadTok.current) setLoading(false) }
  }
  const loadRef = useRef(loadChain)
  loadRef.current = loadChain
  useEffect(() => { void loadChain(false) }, [idx, tm, mode, live, curExp && expPinned ? curExp : '', expPinned, sym, useIntra]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!live) return
    let busy = false
    const id = window.setInterval(async () => {
      if (document.hidden || busy) return
      busy = true
      try { await loadRef.current(true) } finally { busy = false }
    }, 1000)
    return () => window.clearInterval(id)
  }, [live])

  useEffect(() => {          // previous close, for the "vs prev. close" line
    setPrevSpot(null)
    if (idx <= 0 || live || !chain) return
    const p = dates[idx - 1]
    if (spotCache.current.has(p)) { setPrevSpot(spotCache.current.get(p) ?? null); return }
    let alive = true
    get<Chain>(`/api/options/sim/chain?symbol=${sym}&date=${p}`).then((c) => { spotCache.current.set(p, c.underlying); if (alive) setPrevSpot(c.underlying) }).catch(() => { spotCache.current.set(p, null) })
    return () => { alive = false }
  }, [idx, sym, live, chain?.underlying]) // eslint-disable-line react-hooks/exhaustive-deps

  // ---------- valuation (marks, stop-losses, margin)
  const openKey = useMemo(() => JSON.stringify(positions.map((p) => [p.id, p.closed ? 1 : 0, p.lots])), [positions])
  const valTok = useRef(0)
  useEffect(() => {
    if (!chain || idx < 0) return
    const tok = ++valTok.current
    const alive = () => tok === valTok.current
    ;(async () => {
      // The simulator never closes a leg by itself: only the person's own Exit does. (Stop-losses belong to the Strategy Builder.)
      const open = positions.filter((p) => !p.closed)
      if (!open.length) { setMark(null); setValued({}); setMargin(null); marginKey.current = ''; return }
      let m: Mark | null = null
      const vals: Record<string, Valued> = {}
      try {
        const body = { symbol: sym, positions: open.map((p) => legBody(p, mult)) }
        m = live ? await post<Mark>('/api/options/live/mark', body)
          : await post<Mark>(useIntra ? '/api/options/intraday/mark' : '/api/options/sim/mark', { ...body, date, ...(useIntra ? { time: tm } : {}) })
        if (!alive()) return
        open.forEach((p, i) => { vals[String(p.id)] = m!.positions[i] })
      } catch (e) {
        if (!alive()) return
        open.forEach((p) => { vals[String(p.id)] = { ltp: null, pnl: null, note: 'valuation failed: ' + (e as Error).message } })
      }
      if (!alive()) return
      setMark(m); setValued(vals)
      // margin only when the set of open legs changes (never estimated: the server says "n/a" when it cannot know)
      const legs = open.map((p) => legBody(p, mult))
      const key = JSON.stringify(legs.map((l) => [l.side, l.opt, l.strike, l.expiry, l.lots, l.lot_size, l.entry]))
      if (key !== marginKey.current) {
        try { const r = await post<Margin>('/api/options/sim/margin', { symbol: sym, positions: legs }); if (alive()) { setMargin(r); marginKey.current = key } }
        catch (e) { if (alive()) { setMargin({ margin: null, has_sell: false, premium_received: 0, hedge_benefit: null, note: 'margin request failed: ' + (e as Error).message, unlisted: [] }); marginKey.current = '' } }
      }
    })()
  }, [chain, openKey, mult, live]) // eslint-disable-line react-hooks/exhaustive-deps

  // ---------- what-if (theoretical Black-Scholes; never used for the P&L numbers)
  const minOpenExpiry = useMemo(() => { const e = positions.filter((p) => !p.closed && p.expiry >= date).map((p) => p.expiry).sort(); return e[0] ?? null }, [positions, date])
  useEffect(() => {
    if (!date) return
    if (!wiDate || wiDate < date || (minOpenExpiry && wiDate > minOpenExpiry) || (!minOpenExpiry && wiDate !== date)) setWiDate(date)
  }, [date, minOpenExpiry]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const open = positions.filter((p) => !p.closed)
    if (!open.length || !chain) { setWi(null); return }
    if (useIntra || live) { setWi({ error: live ? 'the what-if model uses end-of-day closes; switch LIVE off to use it' : 'the what-if model uses end-of-day closes; switch the time bar to EOD to use it' }); return }
    let alive = true
    const t = window.setTimeout(() => {
      post<WhatIf>('/api/options/sim/whatif', { date, symbol: sym, positions: open.map((p) => legBody(p, mult)), target_date: wiDate || date, iv_shift: wiIv, spot_shift_pct: wiSp })
        .then((r) => { if (alive) setWi(r) }).catch((e: Error) => { if (alive) setWi({ error: e.message }) })
    }, 220)
    return () => { alive = false; window.clearTimeout(t) }
  }, [chain, openKey, mult, wiDate, wiIv, wiSp, live]) // eslint-disable-line react-hooks/exhaustive-deps

  // ---------- playback
  const stateRef = useRef({ idx, tm, mins, dates, intraSet, useIntra, speed })
  stateRef.current = { idx, tm, mins, dates, intraSet, useIntra, speed }
  useEffect(() => {
    if (!playing) return
    let alive = true
    let timer: number | undefined
    const step = async () => {
      if (!alive) return
      const s = stateRef.current
      if (s.useIntra) {
        const i = s.mins.indexOf(s.tm ?? '')
        if (i < s.mins.length - 1) { setTm(s.mins[Math.min(s.mins.length - 1, i + s.speed)]); timer = window.setTimeout(step, 650); return }
        if (s.idx < s.dates.length - 1 && s.intraSet.has(s.dates[s.idx + 1])) { await goTo(s.idx + 1, null); timer = window.setTimeout(step, 650); return }
        setPlaying(false); toast('End of the intraday data for this day'); return
      }
      if (s.idx >= s.dates.length - 1) { setPlaying(false); toast('Reached the last available trading date'); return }
      await goTo(s.idx + 1)
      timer = window.setTimeout(step, 1500 / s.speed)
    }
    timer = window.setTimeout(step, 400)
    return () => { alive = false; window.clearTimeout(timer) }
  }, [playing, speed]) // eslint-disable-line react-hooks/exhaustive-deps

  // ---------- actions
  const setSym = (s: Sym) => { setPlaying(false); setSymState(s); ls.set('sim.sym', s) }
  const goIndex = (i: number) => { setPlaying(false); void goTo(i) }
  const goMinute = (j: number) => { setPlaying(false); if (mins.length) setTm(mins[Math.max(0, Math.min(mins.length - 1, j))]) }
  const setMode = (m: '1m' | 'eod') => { setModeState(m); setPlaying(false) }
  const pickExpiry = (e: string) => { setCurExp(e); setExpPinned(true) }
  const setMult = (v: number) => { const m = Math.max(1, Math.min(100, Math.round(v) || 1)); setMultState(m); ls.set('sim.mult', String(m)) }

  const makeLeg = (c: Chain, side: Side, opt: Opt, k: number): SimPos | string => {
    const l = c.rows.find((r) => r.strike === k)?.[opt]
    if (!l || !(l.ltp != null && l.ltp > 0)) return 'No valid price for that contract on this date'
    if (!c.lot_size) return 'Lot size unknown for this expiry/date, so no position can be valued'
    const intra = c.mode === 'intraday' || c.mode === 'live'
    return { id: Date.now() + Math.random(), side, opt, strike: k, expiry: c.expiry, lots: 1, lot_size: c.lot_size, entry: l.ltp, entry_date: c.date, sl_pct: null, sl: null,
      entry_time: intra ? (c.time ?? '').slice(0, 5) : null, live: c.mode === 'live' || undefined, untraded_entry: intra ? false : !l.volume, stale_entry: intra && (l.age_min ?? 0) > 5 ? l.age_min : null }
  }
  const addPos = (side: Side, opt: Opt, k: number) => {
    if (readOnly || !chain) return
    const leg = makeLeg(chain, side, opt, k)
    if (typeof leg === 'string') { toast(leg); return }
    setPositions((p) => [...p, leg])
    const l = chain.rows.find((r) => r.strike === k)![opt]!
    if (chain.mode === 'intraday' && (l.age_min ?? 0) > 5) toast(`Note: last trade in this contract was ${l.age_min} min before ${chain.time}`)
    else toast(`Simulated ${side} ${n0(k)} ${opt} @ ₹${num(l.ltp)} (${chain.mode === 'live' ? 'live ' + chain.time : dmy(chain.date) + (chain.mode === 'intraday' ? ' ' + chain.time : ' close')})`)
  }
  const placePreset = async (name: string) => {
    if (readOnly || !chain) return
    const rule = PRESET_RULES[name]
    let c = chain
    if (rule) {
      // pick an expiry whose settlement day is in the loaded data, then go to its entry day (T-N trading days) at the close
      const inData = (e: string) => dates.includes(e) && dates.indexOf(e) - rule.entryDaysBefore >= 0
      const E = inData(chain.expiry) ? chain.expiry : chain.expiries.find(inData)
      if (!E) { toast(`${name}: the expiry day of ${dmy(chain.expiry)} is not in the loaded data yet. Pick an earlier date.`); return }
      const di = dates.indexOf(E) - rule.entryDaysBefore
      setPlaying(false)
      const useEod = eodSet.has(dates[di])
      try { c = await fetchChain({ sym, date: dates[di], tm: null, intra: !useEod, live: false, expiry: E }) } catch (e) { toast(`${name}: ${(e as Error).message}`); return }
      if (c.expiry !== E) { toast(`${name}: expiry ${dmy(E)} not listed on ${dmy(dates[di])}`); return }
      const { plan, missing } = planPreset(c, name)
      if (missing.length) { toast(`${name}: no real price for ${missing.join(', ')}; nothing added`); return }
      const legs = plan.map(([s, o, k]) => makeLeg(c, s, o, k))
      const bad = legs.find((l) => typeof l === 'string')
      if (bad) { toast(String(bad)); return }
      setModeState(useEod ? 'eod' : '1m'); setCurExp(E); setExpPinned(true)
      await goTo(di, 'END')
      setPositions((p) => [...p, ...(legs as SimPos[])])
      toast(`${name}: placed on ${dmy(dates[di])} close (T−${rule.entryDaysBefore}) for expiry ${dmy(E)}: ${plan.map(([s, o, k]) => s[0] + ' ' + n0(k) + o).join(', ')} · no automatic exit, close the legs yourself. Press Next day › to hold to expiry.`)
      return
    }
    const { plan, missing } = planPreset(c, name)
    if (missing.length) { toast(`${name}: no real price for ${missing.join(', ')} at this ${c.mode === 'intraday' ? 'minute' : 'date'}; nothing added`); return }
    const legs = plan.map(([s, o, k]) => makeLeg(c, s, o, k))
    const bad = legs.find((l) => typeof l === 'string')
    if (bad) { toast(String(bad)); return }
    setPositions((p) => [...p, ...(legs as SimPos[])])
    toast(`${name} added: ${plan.map(([s, o, k]) => s[0] + ' ' + n0(k) + o).join(', ')} @ ${c.mode === 'intraday' ? c.time : c.mode === 'live' ? 'live' : 'close'} prices`)
  }
  const changeLots = (id: SimPos['id'], d: number) => !readOnly && setPositions((p) => p.map((x) => (x.id === id ? { ...x, lots: Math.max(1, x.lots + d) } : x)))
  const removePos = (id: SimPos['id']) => !readOnly && setPositions((p) => p.filter((x) => x.id !== id))
  const exitPos = (id: SimPos['id']) => {
    if (readOnly) return
    const v = valued[String(id)]
    if (!v || v.ltp == null) { toast('No price for this contract on the selected date, so it cannot be exited here'); return }
    setPositions((p) => p.map((x) => (x.id === id ? { ...x, closed: true, exit: v.ltp!, exit_date: date, exit_time: useIntra ? tm : live ? (chain?.time ?? '').slice(0, 5) : null, realized: v.pnl } : x)))
  }
  const exitAll = () => { if (readOnly) return; let n = 0; positions.forEach((p) => { if (!p.closed && valued[String(p.id)]?.ltp != null) { exitPos(p.id); n++ } }); if (!n) toast('No open legs with a price on this date') }
  const clearAll = () => { if (readOnly) return; if (positions.length && !confirm('Remove all simulated positions?')) return; setPositions([]) }
  const exportCsv = () => {
    if (!positions.length) { toast('No positions to export'); return }
    const H = ['side', 'opt', 'strike', 'expiry', 'lots', 'lot_size', 'entry_date', 'entry', 'status', 'valued_on', 'value_or_exit', 'pnl']
    const rows = positions.map((p) => { const v = valued[String(p.id)]; return [p.side, p.opt, p.strike, p.expiry, p.lots, p.lot_size, p.entry_date, p.entry, p.closed ? 'closed' : 'open', p.closed ? p.exit_date : date, p.closed ? p.exit : v?.ltp, p.closed ? p.realized : v?.pnl] })
    const csv = [H, ...rows].map((r) => r.map((x) => `"${String(x ?? '').replace(/"/g, '""')}"`).join(',')).join('\n')
    const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' })); a.download = `simulation_${date}.csv`; a.click()
  }

  return {
    readOnly, sym, setSym, dates, eodSet, intraSet, idx, date, tm, mins, mode, setMode, useIntra, initError, goIndex, goMinute, goTo,
    chain, chainError, loading, curExp, pickExpiry, live, setLive, prevSpot,
    positions, valued, mark, margin, mult, setMult, addPos, placePreset, changeLots, removePos, exitPos, exitAll, clearAll, exportCsv,
    wi, wiDate, setWiDate, wiIv, setWiIv, wiSp, setWiSp, minOpenExpiry,
    playing, setPlaying, speed, setSpeed,
  }
}
export type Sim = ReturnType<typeof useSimulator>
