import { useMemo, useRef, useState } from 'react'

/** A searchable drop-down that works with a finger: tap the box, the list opens, type to narrow it, tap a row to pick it.
 *  (The browser's own <datalist> does not open reliably on an iPad, so the stock list could not be searched there.)
 *  `value` is always what is in the box; a pick sets it to the option's value. */
export function SearchSelect({ value, onChange, options, placeholder, label, max = 60 }: { value: string; onChange: (v: string) => void; options: string[]; placeholder?: string; label?: string; max?: number }) {
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLInputElement>(null)
  const q = value.trim().toUpperCase()
  const shown = useMemo(() => {
    const starts = options.filter((o) => o.toUpperCase().startsWith(q))
    const has = options.filter((o) => !o.toUpperCase().startsWith(q) && o.toUpperCase().includes(q))
    return [...starts, ...has].slice(0, max)
  }, [options, q, max])
  const pick = (o: string) => { onChange(o); setOpen(false); box.current?.blur() }
  return (
    <div className="ss">
      <input ref={box} value={value} onChange={(e) => { onChange(e.target.value.toUpperCase()); setOpen(true) }} onFocus={() => setOpen(true)} onBlur={() => setOpen(false)}
        onKeyDown={(e) => { if (e.key === 'Escape') setOpen(false); if (e.key === 'Enter' && open && shown.length === 1) { e.preventDefault(); pick(shown[0]) } }}
        placeholder={placeholder} maxLength={30} autoComplete="off" autoCapitalize="characters" spellCheck={false} aria-label={label ?? placeholder} role="combobox" aria-expanded={open} />
      {open && (
        <ul className="sslist" role="listbox">
          {shown.map((o) => <li key={o} role="option" aria-selected={o === value} className={o === value ? 'on' : ''} onPointerDown={(e) => { e.preventDefault(); pick(o) }}>{o}</li>)}
          {!shown.length && <li className="none">{options.length ? 'No match' : 'Loading the list…'}</li>}
        </ul>
      )}
    </div>
  )
}
