import { useId } from 'react'

/** The app's mark: three rising candlesticks in a soft rounded tile. Colours come from CSS variables (light / dark theme);
 * `onDark` is the glass version for the blue login panel. Inline SVG, so it stays sharp at every size. */
export function BrandMark({ size = 36, onDark = false }: { size?: number; onDark?: boolean }) {
  const id = 'bm' + useId().replace(/[^a-zA-Z0-9]/g, '')
  return (
    <svg className={'brandmark' + (onDark ? ' ondark' : '')} width={size} height={size} viewBox="0 0 40 40" aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" style={{ stopColor: 'var(--bm-a)' }} />
          <stop offset="1" style={{ stopColor: 'var(--bm-b)' }} />
        </linearGradient>
      </defs>
      <rect x="0.75" y="0.75" width="38.5" height="38.5" rx="11" style={{ fill: `url(#${id})`, stroke: 'var(--bm-line)', strokeWidth: 1 }} />
      <path className="bm-w" d="M12.8 17v15M20 12v16M27.2 7.5v15.5" />
      <rect className="bm-c" x="10.5" y="20" width="4.6" height="9" rx="1.3" />
      <rect className="bm-c" x="17.7" y="15" width="4.6" height="10" rx="1.3" />
      <rect className="bm-c" x="24.9" y="10" width="4.6" height="10" rx="1.3" />
    </svg>
  )
}
