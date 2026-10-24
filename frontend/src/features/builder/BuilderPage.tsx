import { useQuery } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { ApiError, api, get, post } from '../../lib/api'
import { useToast } from '../../lib/toast'
import { HistoryView } from './HistoryView'
import OptionsJournalPage from '../journal/OptionsJournalPage'
import { ResultView, type BtResult } from './ResultView'
import { buildStrategy, defaultForm, fromLegacyState, HINT, L, MODES, PRESET_GROUPS, PRESET_NOTES, PRESETS, strikeText, toLegacyState, type FormState, type LegForm, type LegacyState } from './strategy'
import './builder.css'

const DRAFT = 'bt.draft'
const loadDraft = (): FormState => { try { return fromLegacyState(JSON.parse(localStorage.getItem(DRAFT) ?? 'null')) } catch { return defaultForm() } }
const saveDraft = (f: FormState) => { try { localStorage.setItem(DRAFT, JSON.stringify(toLegacyState(f))) } catch { /* private mode */ } }

interface Saved { id: string; name: string }
export const errorList = (e: unknown): string[] => {
  if (e instanceof ApiError) { const d = e.body.detail as { errors?: string[] } | undefined; return d?.errors ?? [e.message] }
  return [String(e)]
}

/** History: options paper trades (every leg from the Options Simulator) and saved backtests, one tab each. */
function HistoryTabs() {
  const [tab, setTab] = useState<'trades' | 'backtests'>(() => { try { return localStorage.getItem('history.tab') === 'backtests' ? 'backtests' : 'trades' } catch { return 'trades' } })
  const pick = (t: 'trades' | 'backtests') => { setTab(t); try { localStorage.setItem('history.tab', t) } catch { /* ignore */ } }
  return (
    <>
      <div className="histtabs"><span className="seg"><button className={tab === 'trades' ? 'on' : ''} onClick={() => pick('trades')}>Options paper trades</button><button className={tab === 'backtests' ? 'on' : ''} onClick={() => pick('backtests')}>Backtests</button></span></div>
      {tab === 'trades' ? <OptionsJournalPage /> : <HistoryView />}
    </>
  )
}

export default function BuilderPage({ view }: { view: 'builder' | 'backtest' | 'history' }) {
  if (view === 'history') return <HistoryTabs />
  return <Workspace view={view} />
}

function Workspace({ view }: { view: 'builder' | 'backtest' }) {
  const toast = useToast()
  const nav = useNavigate()
  const loc = useLocation()
  const [f, setF] = useState<FormState>(loadDraft)
  const [result, setResult] = useState<BtResult | null>(null)
  const [errs, setErrs] = useState<string[]>([])
  const [running, setRunning] = useState(false)
  const [note, setNote] = useState('')
  const [presetNote, setPresetNote] = useState('')
  const saved = useQuery({ queryKey: ['builder', 'saved'], queryFn: () => get<Saved[]>('/api/options/strategies'), retry: false })
  const range = useQuery({ queryKey: ['builder', 'dates', f.symbol], queryFn: () => get<{ dates: string[] }>(`/api/options/sim/dates?symbol=${f.symbol}`) })
  const [savedId, setSavedId] = useState('')

  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setF((x) => ({ ...x, [k]: v }))
  useEffect(() => { saveDraft(f) }, [f])
  useEffect(() => {          // first use: fill the date range from the data that is loaded
    const ds = range.data?.dates
    if (ds?.length && (!f.start || !f.end)) setF((x) => ({ ...x, start: x.start || ds[0], end: x.end || ds[ds.length - 1] }))
  }, [range.data]) // eslint-disable-line react-hooks/exhaustive-deps

  // a backtest opened from History
  const openId = (loc.state as { open?: string } | null)?.open
  useEffect(() => {
    if (!openId || view !== 'backtest') return
    get<BtResult>(`/api/options/backtests/${openId}`).then((r) => { setResult(r); setNote(`opened saved backtest ${openId}`) }).catch((e) => setErrs([String(e)]))
  }, [openId, view])

  const applyPreset = (name: string) => {
    setPresetNote(PRESET_NOTES[name] ?? '')
    if (!name) return
    setF((x) => ({ ...x, name, legs: PRESETS[name].map((l) => ({ ...l })) }))
  }
  const loadSaved = async (id: string) => {
    setSavedId(id)
    if (!id) return
    try { const r = await get<{ state: LegacyState }>(`/api/options/strategies/${id}`); setF(fromLegacyState(r.state)) } catch (e) { toast('Could not load: ' + errorList(e).join('; ')) }
  }

  const run = async () => {
    setErrs([]); setRunning(true); setNote('running…')
    try { const r = await post<BtResult>('/api/options/backtest', buildStrategy(f)); setResult(r); setNote(`saved to History as ${r.id}`) }
    catch (e) { setErrs(errorList(e)); setNote('') }
    setRunning(false)
  }
  const save = async () => {
    const name = f.name.trim()
    if (!name) { toast('Give the strategy a name first'); return false }
    setErrs([])
    const body = { name, state: toLegacyState(f), strategy: buildStrategy(f) }
    try {
      let r: { id: string; created: boolean }
      try { r = await post('/api/options/strategies', body) }
      catch (e) {
        if (e instanceof ApiError && errorList(e).join(' ').includes('already exists') && confirm(`"${name}" already exists on the server. Overwrite it?`)) r = await post('/api/options/strategies', { ...body, overwrite: true })
        else throw e
      }
      await saved.refetch(); setSavedId(r.id); setNote(`${r.created ? 'saved' : 'updated'} "${name}" on the server (validated)`)
      return true
    } catch (e) { setErrs(errorList(e)); return false }
  }
  const remove = async () => {
    const x = saved.data?.find((q) => q.id === savedId)
    if (!x || !confirm(`Delete saved strategy "${x.name}" from the server?`)) return
    try { await api(`/api/options/strategies/${savedId}`, { method: 'DELETE' }) } catch (e) { toast(errorList(e).join('; ')) }
    setSavedId(''); void saved.refetch()
  }
  const local = useMemo<Record<string, LegacyState>>(() => { try { return JSON.parse(localStorage.getItem('builder.strategies') ?? '{}') } catch { return {} } }, [])
  const pending = Object.keys(local).filter((n) => !(saved.data ?? []).some((s) => s.name.toLowerCase() === n.toLowerCase()))
  const importLocal = async () => {
    const items = Object.entries(local).map(([name, st]) => { const ff = fromLegacyState(st); return { name, state: st, strategy: { ...buildStrategy(ff), name } } })
    try {
      const r = await post<{ imported: unknown[]; skipped: { name: string; reason: string }[] }>('/api/options/strategies/import', { items })
      setNote(`imported ${r.imported.length}` + (r.skipped.length ? `, skipped ${r.skipped.length}: ${r.skipped.map((x) => `${x.name} (${x.reason})`).join('; ')}` : '') + '. Browser copies were kept.')
    } catch (e) { setNote('import failed: ' + errorList(e).join('; ')) }
    void saved.refetch()
  }

  const legs = f.legs
  const setLeg = useCallback((i: number, patch: Partial<LegForm>) => setF((x) => ({ ...x, legs: x.legs.map((l, j) => (j === i ? { ...l, ...patch } : l)) })), [])
  const s = useMemo(() => buildStrategy(f), [f])
  const isBuilder = view === 'builder'

  return (
    <div className="page">
      <div className="row" style={{ alignItems: 'center' }}>
        <h1>{isBuilder ? 'Strategy Builder' : 'Backtesting'}</h1><span className="chip">NSE Bhavcopy · EOD real data</span><span className="chip muted">Backtest / simulation only · no orders</span>
      </div>
      <div className="alert warn">Backtest results are historical simulations on end-of-day data. They are not a guarantee of future returns.</div>

      {!isBuilder && (
        <div className="card noprint">
          <div className="row" style={{ alignItems: 'center' }}>
            <span className="sub">Preset:</span>
            <PresetSelect onPick={applyPreset} />
            <span className="sub">Saved strategy:</span>
            <select value={savedId} onChange={(e) => void loadSaved(e.target.value)}><option value="">—</option>{saved.data?.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}</select>
            <span className="sub">or the current draft:</span><b>{f.name}</b><span className="spacer" />
            <Link className="btn" to="/builder">✎ Edit in Strategy Builder</Link>
            <button className="btn primary" onClick={() => void run()} disabled={running}>▶ Run Backtest</button><span className="sub">{note}</span>
          </div>
          <div className="sub" style={{ marginTop: 6 }}>{s.symbol} · {s.start} → {s.end} · {s.expiry} expiries · entry {s.entry_days_before_expiry} trading day(s) before expiry at close · exit {s.exit_days_before_expiry ? `${s.exit_days_before_expiry} day(s) before expiry at close` : 'at expiry settlement'} · slippage {s.slippage_pct}%{s.overall_sl ? ` · overall SL ₹${s.overall_sl}` : ''}{s.overall_target ? ` · overall target ₹${s.overall_target}` : ''}</div>
          <div style={{ marginTop: 6, fontSize: 12 }}>
            {s.legs.map((l, i) => (
              <div key={i}><span className={`badge ${l.side}`}>{l.side}</span> {l.lots} lot {l.opt} {strikeText(l.strike as Parameters<typeof strikeText>[0])}{l.expiry !== 'cycle' ? ` · ${l.expiry.replace('_', '-')} expiry` : ''}{l.sl ? ` · SL ${l.sl.value}${l.sl.type === 'pct' ? '%' : ' pts'}` : ''}{l.target ? ` · target ${l.target.value}${l.target.type === 'pct' ? '%' : ' pts'}` : ''}{l.trail_pct ? ` · trail ${l.trail_pct}%` : ''}{l.reentry ? ` · re-entry ×${l.reentry.max}` : ''}</div>
            ))}
            <div className="sub" style={{ marginTop: 4 }}>Rules: {s.on_leg_exit === 'complete' ? 'complete square-off on any leg exit' : 'partial exits'}{s.sl_to_cost !== 'off' ? ` · SL-to-cost (${s.sl_to_cost.replace('_', ' ')})` : ''}{s.lock_profit ? ` · lock profit at ₹${s.lock_profit.trigger} → floor ₹${s.lock_profit.lock}` : ''}{s.vix_filter ? ` · India VIX filter ${(s.vix_filter as { min?: number }).min ?? '–'}..${(s.vix_filter as { max?: number }).max ?? '–'}` : ''}</div>
          </div>
          {errs.length > 0 && <div className="alert" style={{ marginTop: 8 }}>{errs.map((x) => <div key={x}>• {x}</div>)}</div>}
        </div>
      )}

      {isBuilder && (
        <div className="card noprint">
          <div className="row" style={{ alignItems: 'center', marginBottom: 10 }}>
            <span>Preset:</span><PresetSelect onPick={applyPreset} /><span className="sub">{presetNote}</span><span className="spacer" />
            {pending.length > 0 && <span className="warn">{pending.length} strategies saved only in this browser <button className="btn sm" onClick={() => void importLocal()}>Import to server</button></span>}
            <span>Saved (server):</span>
            <select value={savedId} onChange={(e) => void loadSaved(e.target.value)}><option value="">—</option>{saved.data?.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}</select>
            <button className="btn sm" onClick={() => void save()}>Save strategy</button><button className="btn sm" onClick={() => void remove()} title="Delete selected saved strategy" disabled={!savedId}>✕</button>
            <span className="sub" style={{ width: '100%' }}>{range.data?.dates.length ? `${f.symbol} data loaded: ${range.data.dates[0]} → ${range.data.dates[range.data.dates.length - 1]} (${range.data.dates.length} trading days)` : `No ${f.symbol} data loaded`}</span>
          </div>
          <div className="form">
            <label className="field">Name<input value={f.name} onChange={(e) => set('name', e.target.value)} /></label>
            <label className="field">Index<select value={f.symbol} onChange={(e) => set('symbol', e.target.value as FormState['symbol'])}><option>NIFTY</option><option>BANKNIFTY</option></select></label>
            <label className="field">Start<input type="date" value={f.start} onChange={(e) => set('start', e.target.value)} /></label>
            <label className="field">End<input type="date" value={f.end} onChange={(e) => set('end', e.target.value)} /></label>
            <label className="field">Expiries<select value={f.expiry} onChange={(e) => set('expiry', e.target.value as FormState['expiry'])}><option value="weekly">Weekly (every expiry)</option><option value="monthly">Monthly only</option></select></label>
            <label className="field">Entry: trading days before expiry (at close)<input type="number" min={0} max={30} value={f.entry} onChange={(e) => set('entry', e.target.value)} /></label>
            <label className="field">Exit: trading days before expiry (0 = expiry settle)<input type="number" min={0} max={30} value={f.exit} onChange={(e) => set('exit', e.target.value)} /></label>
            <label className="field">Overall SL ₹ (on close MTM)<input type="number" placeholder="none" value={f.osl} onChange={(e) => set('osl', e.target.value)} /></label>
            <label className="field">Overall target ₹<input type="number" placeholder="none" value={f.otg} onChange={(e) => set('otg', e.target.value)} /></label>
            <label className="field">Slippage %<input type="number" step={0.1} min={0} value={f.slip} onChange={(e) => set('slip', e.target.value)} /></label>
            <label className="field">Skip if a leg did not trade on entry day<select value={f.skip} onChange={(e) => set('skip', e.target.value as FormState['skip'])}><option value="1">Yes</option><option value="0">No (flag)</option></select></label>
          </div>
          <h3 style={{ margin: '14px 0 6px' }}>Risk rules <span className="sub" style={{ fontWeight: 400 }}>evaluated on end-of-day data in a fixed, documented order</span></h3>
          <div className="form">
            <label className="field">When a leg hits SL / target<select value={f.onexit} onChange={(e) => set('onexit', e.target.value as FormState['onexit'])}><option value="partial">Partial: exit only that leg</option><option value="complete">Complete: exit all legs at that day&apos;s close</option></select></label>
            <label className="field">After a leg SL, move other legs&apos; SL to cost<select value={f.slcost} onChange={(e) => set('slcost', e.target.value as FormState['slcost'])}><option value="off">Off</option><option value="sl_legs">Legs that have an SL</option><option value="all_legs">All other legs</option></select></label>
            <label className="field">Lock profit: arm at closing MTM ≥ ₹<input type="number" placeholder="off" value={f.lpTrig} onChange={(e) => set('lpTrig', e.target.value)} /></label>
            <label className="field">…then exit if closing MTM ≤ ₹ (floor)<input type="number" placeholder="—" value={f.lpLock} onChange={(e) => set('lpLock', e.target.value)} /></label>
            <label className="field">Trail floor: every further ₹<input type="number" placeholder="optional" value={f.lpEvery} onChange={(e) => set('lpEvery', e.target.value)} /></label>
            <label className="field">…raise the floor by ₹<input type="number" placeholder="optional" value={f.lpBy} onChange={(e) => set('lpBy', e.target.value)} /></label>
            <label className="field">Trade only if India VIX ≥ (entry-day close)<input type="number" step={0.5} placeholder="no filter" value={f.vixMin} onChange={(e) => set('vixMin', e.target.value)} /></label>
            <label className="field">…and India VIX ≤<input type="number" step={0.5} placeholder="no filter" value={f.vixMax} onChange={(e) => set('vixMax', e.target.value)} /></label>
          </div>
          <details style={{ marginTop: 6 }}><summary className="sub">How these rules are evaluated (EOD)</summary>
            <div className="sub" style={{ lineHeight: 1.6, marginTop: 6 }}>Each day: (1) legs entered before today are checked against today&apos;s high/low, SL before target, both inside the range = SL and the trade is flagged AMBIGUOUS; (2) complete square-off closes the other legs at today&apos;s close; (3) trailing SL moves from today&apos;s close; (4) SL-to-cost applies from the NEXT day; (5) a leg with re-entry left re-enters at today&apos;s close with its strike rule re-applied to today&apos;s data, and is checked from the next day (never on the exit day); (6) overall SL/target and then lock-profit use today&apos;s closing MTM; lock-profit arms on one close and can exit from the next close. Intraday order of events is unknown in EOD data, so none of these claim intraday accuracy.</div></details>
          <h3 style={{ margin: '14px 0 6px' }}>Legs</h3>
          <div className="scroll">
            <table className="legs">
              <thead><tr><th className="l">#</th><th className="l">B/S</th><th className="l">Type</th><th>Lots</th><th className="l">Expiry</th><th className="l">Strike rule</th><th>Value</th><th className="l">Leg SL</th><th className="l">Leg target</th><th>Trail SL %</th><th>Re-entries</th><th /></tr></thead>
              <tbody>
                {legs.map((l, i) => (
                  <tr key={i}>
                    <td className="l">{i + 1}</td>
                    <td className="l"><select value={l.side} onChange={(e) => setLeg(i, { side: e.target.value as LegForm['side'] })}><option value="BUY">Buy</option><option value="SELL">Sell</option></select></td>
                    <td className="l"><select value={l.opt} onChange={(e) => setLeg(i, { opt: e.target.value as LegForm['opt'] })}><option>CE</option><option>PE</option></select></td>
                    <td><input type="number" style={{ width: 50 }} value={l.lots} onChange={(e) => setLeg(i, { lots: e.target.value })} /></td>
                    <td className="l"><select value={l.exp} onChange={(e) => setLeg(i, { exp: e.target.value })}><option value="cycle">Cycle expiry</option><option value="next_weekly">Next weekly</option><option value="monthly">Monthly</option><option value="next_monthly">Next monthly</option></select></td>
                    <td className="l"><select value={l.mode} onChange={(e) => setLeg(i, { mode: e.target.value })}>{MODES.map(([v, t]) => <option key={v} value={v}>{t}</option>)}</select></td>
                    <td><input type="number" step="any" style={{ width: 70 }} value={l.val} onChange={(e) => setLeg(i, { val: e.target.value })} />{l.mode === 'PREMIUM_RANGE' && <> – <input type="number" step="any" style={{ width: 70 }} value={l.val2} onChange={(e) => setLeg(i, { val2: e.target.value })} /></>} <span className="sub">{HINT[l.mode] ?? ''}</span></td>
                    <td className="l"><select value={l.sl_t} onChange={(e) => setLeg(i, { sl_t: e.target.value as LegForm['sl_t'] })}><option value="">none</option><option value="pct">%</option><option value="pts">pts</option></select> {l.sl_t && <input type="number" step="any" style={{ width: 60 }} value={l.sl_v} onChange={(e) => setLeg(i, { sl_v: e.target.value })} />}</td>
                    <td className="l"><select value={l.tg_t} onChange={(e) => setLeg(i, { tg_t: e.target.value as LegForm['tg_t'] })}><option value="">none</option><option value="pct">%</option><option value="pts">pts</option></select> {l.tg_t && <input type="number" step="any" style={{ width: 60 }} value={l.tg_v} onChange={(e) => setLeg(i, { tg_v: e.target.value })} />}</td>
                    <td>{l.sl_t ? <input type="number" step="any" style={{ width: 55 }} value={l.trail} onChange={(e) => setLeg(i, { trail: e.target.value })} /> : <span className="sub">needs SL</span>}</td>
                    <td>{l.sl_t ? <select value={l.re} onChange={(e) => setLeg(i, { re: e.target.value })}>{['0', '1', '2', '3', '4', '5'].map((v) => <option key={v} value={v}>{v === '0' ? 'none' : v}</option>)}</select> : <span className="sub">needs SL</span>}</td>
                    <td><button className="btn sm" onClick={() => setF((x) => ({ ...x, legs: x.legs.filter((_, j) => j !== i) }))}>✕</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="row" style={{ marginTop: 8, alignItems: 'center' }}>
            <button className="btn" onClick={() => legs.length < 10 && setF((x) => ({ ...x, legs: [...x.legs, L('SELL', 'CE')] }))}>+ Add leg</button>
            <button className="btn primary" onClick={() => void (async () => { if (await save()) nav('/backtesting') })()}>Save &amp; open in Backtesting →</button>
            <span className="sub">{note}</span>
          </div>
          {errs.length > 0 && <div className="alert" style={{ marginTop: 8 }}>{errs.map((x) => <div key={x}>• {x}</div>)}</div>}
          <details style={{ marginTop: 8 }}><summary className="sub">Charges (editable)</summary>
            <div className="form" style={{ marginTop: 6 }}>{Object.entries(f.charges).map(([k, v]) => <label className="field" key={k}>{k}<input type="number" step="any" value={v} onChange={(e) => setF((x) => ({ ...x, charges: { ...x.charges, [k]: e.target.value } }))} /></label>)}</div></details>
        </div>
      )}

      {!isBuilder && result && <ResultView r={result} />}
    </div>
  )
}

function PresetSelect({ onPick }: { onPick: (name: string) => void }) {
  return (
    <select value="" onChange={(e) => onPick(e.target.value)}>
      <option value="">—</option>
      {PRESET_GROUPS.map(([g, names]) => <optgroup key={g} label={g}>{names.map((n) => <option key={n}>{n}</option>)}</optgroup>)}
    </select>
  )
}
