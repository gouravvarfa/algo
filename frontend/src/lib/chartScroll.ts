import type { IChartApi } from 'lightweight-charts'

/** Chart interaction that never steals the page scroll until the person asks for it: the mouse wheel and a vertical finger swipe
 * scroll the PAGE (as on every normal website); dragging still moves the chart, dragging an axis or pinching zooms it. */
export const PAGE_FRIENDLY_CHART = {
  handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
  handleScale: { mouseWheel: false, pinch: true, axisPressedMouseMove: true, axisDoubleClickReset: true },
} as const

const ACTIVE = {
  handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true },
  handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true, axisDoubleClickReset: true },
} as const

/** Like a map embedded in a web page: CLICK (or tap) the chart and from then on the wheel zooms / scrolls the chart and the page stays
 * still. Click or tap anywhere outside, or press Escape, and the wheel scrolls the page again. Ctrl + wheel always zooms the chart.
 * Returns a cleanup function. */
export function attachChartFocus(host: HTMLElement, chart: IChartApi, onChange: (active: boolean) => void): () => void {
  let active = false
  const set = (on: boolean) => {
    if (on === active) return
    active = on
    chart.applyOptions(on ? ACTIVE : PAGE_FRIENDLY_CHART)
    host.classList.toggle('chart-active', on)
    onChange(on)
  }
  const onDown = () => set(true)
  const onOutside = (e: PointerEvent) => { if (!host.contains(e.target as Node)) set(false) }
  const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') set(false) }
  // Ctrl + wheel zooms the chart even when it is not selected (and keeps the browser from zooming the whole page)
  const onWheel = (e: WheelEvent) => {
    if (!active && (e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      const ts = chart.timeScale(), r = ts.getVisibleLogicalRange()
      if (!r) return
      const span = r.to - r.from, k = e.deltaY > 0 ? 1.12 : 1 / 1.12, mid = r.to - span * 0.15
      ts.setVisibleLogicalRange({ from: mid - (mid - r.from) * k, to: mid + (r.to - mid) * k })
    }
  }
  host.addEventListener('pointerdown', onDown)
  host.addEventListener('wheel', onWheel, { passive: false })
  document.addEventListener('pointerdown', onOutside)
  document.addEventListener('keydown', onKey)
  return () => {
    host.removeEventListener('pointerdown', onDown)
    host.removeEventListener('wheel', onWheel)
    document.removeEventListener('pointerdown', onOutside)
    document.removeEventListener('keydown', onKey)
  }
}
