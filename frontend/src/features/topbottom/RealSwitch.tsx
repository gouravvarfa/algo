import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ApiError, get, put } from '../../lib/api'
import { useDataSource } from '../../lib/datasource'
import { useToast } from '../../lib/toast'

interface Item { instrument_id: string; symbol: string; real_on: boolean; real_effective: boolean }
interface Data { account_mode: string; real_allowed: boolean; blockers: string[]; instruments: Item[] }

/** The person's own Real/Paper switch for ONE Top-Bottom instrument. Shown only to accounts an admin allowed for real orders, never in an admin's "view as" screen.
 * Off by default; it can be turned on only while the whole account is in REAL mode, and it turns itself off whenever the account leaves REAL. */
export function RealSwitch({ instrumentId, symbol }: { instrumentId: string; symbol: string }) {
  const ds = useDataSource()
  const qc = useQueryClient()
  const toast = useToast()
  const q = useQuery({ queryKey: ['broker-instruments'], queryFn: () => get<Data>('/api/me/broker/instruments'), enabled: !ds.readOnly, refetchInterval: 30_000, retry: false })
  const set = useMutation({
    mutationFn: (real: boolean) => put<Data>(`/api/me/broker/instruments/${instrumentId}`, { real }),
    onSuccess: (d, real) => { qc.setQueryData(['broker-instruments'], d); toast(real ? `Real orders armed for ${symbol}` : `${symbol} is on paper`) },
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Could not change the switch'),
  })
  const d = q.data
  const it = d?.instruments.find((i) => i.instrument_id === instrumentId)
  if (ds.readOnly || !d || !d.real_allowed || !it) return null
  const accountReal = d.account_mode === 'real'
  const on = it.real_on && accountReal
  return (
    <div className="row" style={{ gap: 10, alignItems: 'center', flexWrap: 'wrap', padding: '6px 0' }}>
      <span className={'chip ' + (on ? 'bad' : 'muted')}>{on ? 'REAL' : 'PAPER'}</span>
      <span className="sub">Real orders for <b>{symbol}</b></span>
      <button className={'btn sm' + (on ? '' : ' danger')} disabled={set.isPending || (!on && !accountReal)}
        title={!accountReal ? d.blockers.join(' ') || 'Switch your account to REAL in Settings first.' : ''}
        onClick={() => {
          if (on) set.mutate(false)
          else if (window.confirm(`Turn REAL orders ON for ${symbol}?\n\nThis instrument will trade real money in your Angel One account, within the limits an admin set for you. You can turn it off here at any time.`)) set.mutate(true)
        }}>{on ? 'Switch to paper' : 'Turn real ON'}</button>
      {!accountReal && <span className="sub">Your account is on paper. <Link to="/settings">Switch to REAL in Settings</Link>.</span>}
    </div>
  )
}
