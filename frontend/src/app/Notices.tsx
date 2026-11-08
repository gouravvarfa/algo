import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { get } from '../lib/api'
import './notices.css'

interface Cal { exchange: string; kind: string; text: string; ahead?: boolean }
interface Ann { message: string; level: string; updated_at?: number }

type Tone = 'info' | 'warn' | 'bad' | 'holiday' | 'special'
const ICON: Record<Tone, string> = { info: 'ℹ', warn: '⚠', bad: '⛔', holiday: '📅', special: '🪔' }
const TITLE: Record<Tone, string> = { info: 'Announcement', warn: 'Notice', bad: 'Urgent', holiday: 'Market holiday', special: 'Special session' }

const KEY = 'notices.dismissed'
function dismissed(): Record<string, string> {
  try { return JSON.parse(localStorage.getItem(KEY) || '{}') } catch { return {} }
}
const today = () => new Date().toISOString().slice(0, 10)
const id = (s: string) => { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return String(h) }

/** The notices at the top of every trading page: today's and the next days' market holidays / special sessions (made from the market calendar,
 * automatically) and the admin's announcement. Each can be closed; a closed notice stays closed for the rest of that day in this browser. */
export function Notices() {
  const q = useQuery({ queryKey: ['announcement'], queryFn: () => get<{ announcement: Ann | null; calendar?: Cal[] }>('/api/announcement'), refetchInterval: 60_000, retry: false })
  const [gone, setGone] = useState(dismissed)
  const items: { key: string; tone: Tone; title: string; text: string }[] = []
  for (const c of q.data?.calendar ?? []) {
    const tone: Tone = c.kind === 'holiday' ? 'holiday' : 'special'
    items.push({ key: id(c.text), tone, title: c.ahead ? (c.kind === 'holiday' ? 'Upcoming market holiday' : 'Upcoming special session') : TITLE[tone], text: c.text })
  }
  const a = q.data?.announcement
  if (a) {
    const tone: Tone = a.level === 'warn' || a.level === 'bad' ? a.level : 'info'
    items.push({ key: id(a.message), tone, title: TITLE[tone], text: a.message })
  }
  const visible = items.filter((x) => gone[x.key] !== today())
  if (!visible.length) return null
  const close = (k: string) => {
    const next = { ...dismissed(), [k]: today() }
    try { localStorage.setItem(KEY, JSON.stringify(next)) } catch { /* private window: closes until reload */ }
    setGone(next)
  }
  return (
    <div className="notices" role="region" aria-label="Notices">
      {visible.map((x) => (
        <div key={x.key} className={`notice ${x.tone}`} role="status">
          <span className="notice-icon" aria-hidden>{ICON[x.tone]}</span>
          <div className="notice-body"><b>{x.title}</b><span>{x.text}</span></div>
          <button className="notice-x" aria-label="Close this notice" title="Hide for today" onClick={() => close(x.key)}>×</button>
        </div>
      ))}
    </div>
  )
}
