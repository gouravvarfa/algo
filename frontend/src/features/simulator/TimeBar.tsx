import { dmy } from '../../lib/format'
import type { Sim } from './useSimulator'

/** The fixed bar at the bottom: minute-by-minute time travel on days with real 1-minute data, day-by-day timeline, and playback. */
export function TimeBar({ s }: { s: Sim }) {
  const hasMin = s.idx >= 0 && s.intraSet.has(s.dates[s.idx]) && s.mins.length > 0
  const mi = s.tm ? s.mins.indexOf(s.tm) : -1
  // the bar always works, also while LIVE is on: any time-travel button first switches LIVE off, then does its job (owner, 23 Nov 2026: since LIVE
  // is remembered the whole bar stayed greyed out and the 1-minute steps could not be used)
  const act = (f: () => void) => () => { if (s.live) s.setLive(false); f() }
  const step = (j: number) => s.goMinute(mi + j)
  const dis = false
  return (
    <div className="actbar">
      {s.idx >= 0 ? (
        <div className="tbar">
          <span className="seg">
            <button className={s.useIntra ? 'on' : ''} onClick={act(() => s.setMode('1m'))} disabled={dis || !hasMin}>1-min</button>
            <button className={!s.useIntra ? 'on' : ''} onClick={act(() => s.setMode('eod'))} disabled={dis || !s.eodSet.has(s.date)}>EOD</button>
          </span>
          <button className="btn sm" disabled={dis || !hasMin || !s.useIntra} onClick={act(() => s.goMinute(0))}>SOD</button>
          {[-15, -5, -1].map((j) => <button key={j} className="btn sm" disabled={dis || !hasMin || !s.useIntra} onClick={act(() => step(j))}>{j}</button>)}
          <span className="tmv num">{s.useIntra ? s.tm : '15:30'}</span>
          {[1, 5, 15].map((j) => <button key={j} className="btn sm" disabled={dis || !hasMin || !s.useIntra} onClick={act(() => step(j))}>+{j}</button>)}
          <button className="btn sm" disabled={dis || !hasMin || !s.useIntra} onClick={act(() => s.goMinute(s.mins.length - 1))}>END</button>
          <input type="range" min={0} max={Math.max(0, s.mins.length - 1)} value={Math.max(0, mi)} disabled={dis || !hasMin || !s.useIntra} onChange={(e) => { const v = Number(e.target.value); act(() => s.goMinute(v))() }} aria-label="Minute of the day" />
          <span className="sub">{s.useIntra && hasMin ? `${s.mins[0]}–${s.mins[s.mins.length - 1]} · ${s.mins.length} index minutes` : hasMin ? 'end-of-day view (NSE Bhavcopy close)' : s.intraSet.has(s.date) ? 'loading the minutes of this day…' : 'end-of-day data only for this date (no 1-minute candles)'}</span>
        </div>
      ) : null}
      <div className="tbar">
        <button className="txtb" disabled={s.idx <= 0} onClick={act(() => s.goIndex(s.idx - 1))}>‹ Previous day</button>
        <span className="when">📅 {s.date ? dmy(s.date) : '–'}</span>
        <button className="txtb" disabled={s.idx >= s.dates.length - 1} onClick={act(() => s.goIndex(s.idx + 1))}>Next day ›</button>
        <div className="tl"><span>{s.dates.length ? dmy(s.dates[0]) : ''}</span>
          <input type="range" min={0} max={Math.max(0, s.dates.length - 1)} value={Math.max(0, s.idx)} onChange={(e) => { const v = Number(e.target.value); act(() => s.goIndex(v))() }} aria-label="Timeline" />
          <span>{s.dates.length ? dmy(s.dates[s.dates.length - 1]) : ''}</span><span className="sub">Day {s.idx + 1} / {s.dates.length}</span></div>
        <button className={'ib round' + (s.playing ? ' on' : '')} title="Play / pause" onClick={act(() => s.setPlaying(!s.playing))}>{s.playing ? '❚❚' : '▶'}</button>
        <select value={s.speed} onChange={(e) => s.setSpeed(Number(e.target.value))} title="Playback speed">{[1, 2, 5, 10].map((v) => <option key={v} value={v}>{v}x</option>)}</select>
      </div>
    </div>
  )
}
