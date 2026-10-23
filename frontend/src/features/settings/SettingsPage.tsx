import { useQuery } from '@tanstack/react-query'
import { Fragment } from 'react'
import { Link } from 'react-router-dom'
import { get } from '../../lib/api'
import { useSession } from '../../lib/session'
import { useTheme } from '../../lib/theme'

interface Bhav { symbols: { symbol: string; days: number; rows: number; expiries: number; from: string; to: string }[]; days: { date: string; status: string }[] }

export default function SettingsPage() {
  const { dark, set } = useTheme()
  const { mode, me } = useSession()
  const data = useQuery({ queryKey: ['settings', 'bhavcopy'], queryFn: () => get<Bhav>('/api/options/bhavcopy/summary') })
  const missing = data.data?.days.filter((d) => d.status !== 'loaded').map((d) => d.date) ?? []
  const system = !localStorage.getItem('theme')
  return (
    <div className="page" style={{ maxWidth: 860 }}>
      <h1>Settings</h1>
      <div className="card">
        <div className="card-head"><h2>Appearance</h2></div>
        <div className="row" style={{ alignItems: 'center' }}>
          <span className="sub">Theme (saved in this browser)</span><span className="spacer" />
          <span className="seg">
            <button className={!system && !dark ? 'on' : ''} onClick={() => set('light')}>Light</button>
            <button className={!system && dark ? 'on' : ''} onClick={() => set('dark')}>Dark</button>
            <button className={system ? 'on' : ''} onClick={() => { try { localStorage.removeItem('theme') } catch { /* ignore */ } document.documentElement.removeAttribute('data-theme'); window.dispatchEvent(new CustomEvent('themechange')) }}>System</button>
          </span>
        </div>
      </div>
      {mode === 'platform' && me && (
        <div className="card">
          <div className="card-head"><h2>Account</h2></div>
          <p className="sub">Signed in as <b>{me.user.name}</b> ({me.user.role}). Change your password and see who opened your dashboard on the <Link to="/account">Account</Link> page.</p>
        </div>
      )}
      <div className="card">
        <div className="card-head"><h2>Historical options data</h2></div>
        {data.isLoading ? <div className="skeleton" style={{ height: 60 }} /> : data.data?.symbols.length ? (
          <dl className="kv">
            {data.data.symbols.map((s) => (<Fragment key={s.symbol}><dt>{s.symbol}</dt><dd>{s.days} trading days · {s.from} → {s.to} · {s.expiries} expiries</dd></Fragment>))}
            {missing.length > 0 && (
              <><dt>Weekdays with no NSE file</dt>
                <dd><details><summary>{missing.length} days (exchange holidays, or a file NSE did not publish). Nothing is filled in for them.</summary>
                  <div className="sub" style={{ marginTop: 6, lineHeight: 1.7, fontWeight: 400 }}>{missing.join(' · ')}</div></details></dd></>
            )}
          </dl>
        ) : <p className="sub">No Bhavcopy data loaded yet.</p>}
        <p className="note">Load more NSE Bhavcopy days from the <Link to="/options">Option Chain</Link> page.</p>
      </div>
      <div className="card">
        <div className="card-head"><h2>Safety</h2></div>
        <dl className="kv">
          <dt>Broker orders</dt><dd>Disabled. Simulator, Strategy Builder and Backtesting never place orders.</dd>
          <dt>Data</dt><dd>Real NSE / Angel One data only. Missing data is flagged, never filled.</dd>
        </dl>
      </div>
    </div>
  )
}
