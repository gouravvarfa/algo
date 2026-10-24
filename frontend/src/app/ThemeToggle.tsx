import { useMotion, useTheme } from '../lib/theme'
import { Icon } from './Icon'

/** Light / Dark switch for the top-right corner of every page. */
export function ThemeToggle() {
  const { dark, set } = useTheme()
  const motion = useMotion()
  return (
    <div className="themecorner" role="group" aria-label="Theme">
      <button className={!dark ? 'on' : ''} onClick={() => set('light')} title="Light mode" aria-label="Light mode" aria-pressed={!dark}><Icon name="sun" /></button>
      <button className={dark ? 'on' : ''} onClick={() => set('dark')} title="Dark mode" aria-label="Dark mode" aria-pressed={dark}><Icon name="moon" /></button>
      <button className={'motion' + (motion.on ? ' live' : '')} onClick={motion.toggle} title={motion.on ? 'Background animation: ON (click to stop)' : 'Background animation: OFF (click to start)'} aria-label="Background animation" aria-pressed={motion.on}><Icon name="wave" /></button>
    </div>
  )
}
