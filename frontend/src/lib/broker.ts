import { useQuery } from '@tanstack/react-query'
import { get } from './api'

/** The name of the market-data source the server runs on ("Angel One" or "Upstox"), so a label never says Angel One while the data comes from Upstox. */
export function useBrokerName(): string {
  const q = useQuery({ queryKey: ['broker-name'], queryFn: () => get<{ data_source?: string }>('/api/overview'), staleTime: 5 * 60_000, retry: false })
  return q.data?.data_source || 'Angel One'
}
