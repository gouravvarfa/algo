/** Chart interaction that never steals the page scroll: the mouse wheel and a vertical finger swipe scroll the PAGE (as on every
 * normal website); the chart is still moved by dragging, zoomed by dragging an axis or pinching, and reset with the Fit button. */
export const PAGE_FRIENDLY_CHART = {
  handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
  handleScale: { mouseWheel: false, pinch: true, axisPressedMouseMove: true, axisDoubleClickReset: true },
} as const
