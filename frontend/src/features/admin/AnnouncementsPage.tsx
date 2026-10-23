import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { ApiError, del, get, put } from '../../lib/api'
import { useToast } from '../../lib/toast'
import './admin.css'

interface A { message: string; level: string; updated_at: number }

/** One banner shown across the top of the trading app for everybody who is signed in (e.g. planned maintenance). */
export default function AnnouncementsPage() {
  const qc = useQueryClient()
  const toast = useToast()
  const cur = useQuery({ queryKey: ['announcement'], queryFn: () => get<{ announcement: A | null }>('/api/announcement') })
  const [message, setMessage] = useState('')
  const [level, setLevel] = useState('info')
  const done = () => { void qc.invalidateQueries({ queryKey: ['announcement'] }) }
  const save = useMutation({ mutationFn: () => put('/api/admin/announcement', { message, level }), onSuccess: () => { toast('Banner is live'); setMessage(''); done() }, onError: (e) => toast(e instanceof ApiError ? e.message : 'Could not publish') })
  const clear = useMutation({ mutationFn: () => del('/api/admin/announcement'), onSuccess: () => { toast('Banner removed'); done() } })
  const a = cur.data?.announcement
  return (
    <div className="page">
      <h1>Announcements</h1>
      <p className="sub">A single banner at the top of every trading page. Use it for maintenance notices or market-day reminders.</p>
      <div className="card">
        <div className="card-head"><h2>Showing now</h2></div>
        {a ? (
          <div className={`banner ${a.level}`}><span>{a.message}</span><button className="btn sm" onClick={() => clear.mutate()}>Remove</button></div>
        ) : <p className="empty">No banner is showing.</p>}
      </div>
      <form className="card stack" onSubmit={(e) => { e.preventDefault(); if (message.trim()) save.mutate() }}>
        <div className="card-head"><h2>Publish a new banner</h2></div>
        <label className="field">Message<input value={message} onChange={(e) => setMessage(e.target.value)} maxLength={300} placeholder="Planned maintenance tonight 10 pm to 10:30 pm" /></label>
        <label className="field">Style<select value={level} onChange={(e) => setLevel(e.target.value)}><option value="info">Information</option><option value="warn">Warning</option><option value="bad">Urgent</option></select></label>
        <div><button className="btn primary" type="submit" disabled={!message.trim() || save.isPending}>Publish</button></div>
      </form>
    </div>
  )
}
