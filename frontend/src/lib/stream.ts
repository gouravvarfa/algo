import { useEffect, useMemo, useRef, useState } from 'react'

/** Live ticks pushed by the server (Server-Sent Events, /api/stream). ONE connection per browser tab, shared by every screen that wants prices:
 * each screen asks for the tokens it needs, the connection is reopened (after a short pause) when the set of tokens changes, and the browser
 * reconnects by itself when the line drops. If the server says the stream is switched off (or the connection keeps failing), `status` becomes
 * 'off' / 'error' and the screens keep using their normal polling: nothing is ever shown that did not come from a tick. */

export type StreamStatus = 'idle' | 'connecting' | 'live' | 'off' | 'error'
export interface Tick { ltp: number; ts: number; at: number }        // price, exchange time (ms), browser time received (ms)

const REOPEN_DELAY_MS = 250      // tokens asked for by several screens at once share one reopen
const STALE_AFTER_MS = 20_000    // no tick (and no heartbeat) for this long: the line is treated as dead and reopened

class StreamManager {
  private want = new Map<string, number>()          // "NFO:61466" -> number of screens asking for it
  private ticks = new Map<string, Tick>()           // token -> latest tick
  private subscribers = new Set<() => void>()
  private es: EventSource | null = null
  private key = ''
  private timer: number | undefined
  private watchdog: number | undefined
  private lastSeen = 0
  private failures = 0
  status: StreamStatus = 'idle'

  subscribe(specs: string[], onChange: () => void): () => void {
    for (const s of specs) this.want.set(s, (this.want.get(s) ?? 0) + 1)
    this.subscribers.add(onChange)
    this.schedule()
    return () => {
      for (const s of specs) {
        const n = (this.want.get(s) ?? 1) - 1
        if (n <= 0) this.want.delete(s); else this.want.set(s, n)
      }
      this.subscribers.delete(onChange)
      this.schedule()
    }
  }

  tick(token: string): Tick | undefined { return this.ticks.get(token) }

  private emit() { this.subscribers.forEach((f) => f()) }
  private setStatus(s: StreamStatus) { if (s !== this.status) { this.status = s; this.emit() } }

  private schedule() {
    window.clearTimeout(this.timer)
    this.timer = window.setTimeout(() => this.reopen(), REOPEN_DELAY_MS)
  }

  private close() {
    this.es?.close(); this.es = null
    window.clearInterval(this.watchdog)
  }

  private reopen() {
    const specs = [...this.want.keys()].sort()
    const key = specs.join(',')
    if (!specs.length) { this.close(); this.key = ''; this.setStatus('idle'); return }
    if (key === this.key && this.es) return
    this.close()
    this.key = key
    this.setStatus('connecting')
    const es = new EventSource(`/api/stream?tokens=${encodeURIComponent(key)}`)
    this.es = es
    this.lastSeen = Date.now()
    const take = (p: Record<string, [number, number]>) => {
      const at = Date.now()
      for (const [tok, v] of Object.entries(p)) this.ticks.set(tok, { ltp: v[0], ts: v[1], at })
    }
    es.addEventListener('snap', (e) => { this.lastSeen = Date.now(); this.failures = 0; take(JSON.parse((e as MessageEvent).data).p); this.setStatus('live'); this.emit() })
    es.addEventListener('ticks', (e) => { this.lastSeen = Date.now(); take(JSON.parse((e as MessageEvent).data).p); this.emit() })
    es.addEventListener('bye', () => { this.lastSeen = Date.now() })       // the server ended it on purpose (lifetime / signed out): onerror below reconnects
    es.onerror = () => {
      // EventSource retries by itself; if the very first connect is refused (switched off, not signed in) the browser closes it: readyState CLOSED
      this.failures += 1
      if (es.readyState === EventSource.CLOSED) {
        this.setStatus(this.failures >= 2 ? 'off' : 'error')
        if (this.failures < 4) window.setTimeout(() => { if (this.es === es) { this.key = ''; this.reopen() } }, 2000 * this.failures)
      } else {
        this.setStatus('connecting')
      }
    }
    this.watchdog = window.setInterval(() => {
      if (document.hidden) return
      if (Date.now() - this.lastSeen > STALE_AFTER_MS && this.es === es) { this.key = ''; this.reopen() }      // heartbeats are every 15 s: silence means a dead line
    }, 5000)
  }
}

export const stream = new StreamManager()

/** Spec of a token for the stream: exchange + token, e.g. streamSpec('NFO', '61466') -> 'NFO:61466'. */
export const streamSpec = (exch: string | undefined, token: string | number | undefined | null): string | null =>
  exch && token != null && String(token) !== '' ? `${exch.toUpperCase()}:${token}` : null

/** The latest pushed price of each token, re-rendered at most about every 120 ms (a busy market must not re-render a page hundreds of times a second).
 * Returns { ticks: token -> Tick, status }. `enabled = false` asks for nothing. */
export function useLiveTicks(specs: (string | null | undefined)[], enabled = true) {
  const list = useMemo(() => [...new Set(specs.filter((s): s is string => !!s))].sort(), [specs.join('|')]) // eslint-disable-line react-hooks/exhaustive-deps
  const [, bump] = useState(0)
  const pending = useRef(false)
  useEffect(() => {
    if (!enabled || !list.length) return
    const on = () => {
      if (pending.current) return
      pending.current = true
      window.setTimeout(() => { pending.current = false; bump((n) => n + 1) }, 120)
    }
    return stream.subscribe(list, on)
  }, [list, enabled])
  useEffect(() => {
    if (!enabled || !list.length) return
    const id = window.setInterval(() => bump((n) => n + 1), 5000)         // lets `fresh()` notice a price that stopped arriving
    return () => window.clearInterval(id)
  }, [list, enabled])
  const out: Record<string, Tick> = {}
  for (const s of list) {
    const tok = s.split(':')[1]
    const t = stream.tick(tok)
    if (t) out[tok] = t
  }
  return { ticks: out, status: enabled && list.length ? stream.status : ('idle' as StreamStatus) }
}

/** A tick counts as live only if it is recent: a price from before the line dropped must not look current. */
export const fresh = (t: Tick | undefined, maxAgeMs = 30_000): t is Tick => !!t && Date.now() - t.at < maxAgeMs
