import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// jsdom has no EventSource: a stand-in that only records the URLs it was opened with
const opened: string[] = []
class FakeES {
  static CLOSED = 2
  readyState = 0
  onerror: (() => void) | null = null
  constructor(url: string) { opened.push(url) }
  addEventListener() { /* no events in this test */ }
  close() { this.readyState = 2 }
}

describe('live stream: one connection per tab', () => {
  beforeEach(() => { vi.useFakeTimers(); opened.length = 0; vi.stubGlobal('EventSource', FakeES) })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  it('never asks the server for more than 400 tokens, and keeps the ones asked for first (the market bar)', async () => {
    const { stream, MAX_STREAM_TOKENS } = await import('./stream')
    const bar = ['NSE:99926000', 'NSE:99926009', 'MCX:574842']
    const chain = Array.from({ length: 494 }, (_, i) => `MCX:${400000 + i}`)       // SILVERM 20 Nov 2026: refused with 400, the bar lost its prices too
    const offBar = stream.subscribe(bar, () => undefined)
    const offChain = stream.subscribe(chain, () => undefined)
    vi.advanceTimersByTime(300)
    const url = opened[opened.length - 1]
    const asked = decodeURIComponent(url.split('tokens=')[1]).split(',')
    expect(MAX_STREAM_TOKENS).toBe(400)
    expect(asked.length).toBe(400)
    for (const b of bar) expect(asked).toContain(b)
    offChain(); offBar()
  })
})
