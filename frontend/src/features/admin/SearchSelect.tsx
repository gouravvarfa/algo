import { useMemo, useRef, useState } from 'react'

export interface Opt { value: string; hint?: string; group?: string }

/** A searchable drop-down that works with a finger: tap the box, the list opens, type to narrow it, tap a row to pick it.
 *  (The browser's own <datalist> does not open reliably on an iPad, so the stock list could not be searched there.)
 *  `value` is always what is in the box; a pick sets it to the option's value. An option may carry a `hint` (shown beside it and searched too)
 *  and a `group` (a heading above the first row of each group). */
export function SearchSelect({ value, onChange, options, placeholder, label, max = 60 }: { value: string; onChange: (v: string) => void; options: (string | Opt)[]; placeholder?: string; label?: string; max?: number }) {
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLInputElement>(null)
  const q = value.trim().toUpperCase()
  const all = useMemo<Opt[]>(() => options.map((o) => (typeof o === 'string' ? { value: o } : o)), [options])
  const shown = useMemo(() => {
    const starts = all.filter((o) => o.value.toUpperCase().startsWith(q))
    const has = all.filter((o) => !o.value.toUpperCase().startsWith(q) && `${o.value} ${o.hint ?? ''}`.toUpperCase().includes(q))
    const found = [...starts, ...has].slice(0, max)
    return q ? found : [...found].sort((a, b) => (a.group ?? '').localeCompare(b.group ?? ''))     // nothing typed yet: the groups sit together
  }, [all, q, max])
  const pick = (o: string) => { onChange(o); setOpen(false); box.current?.blur() }
  return (
    <div className="ss">
      <input ref={box} value={value} onChange={(e) => { onChange(e.target.value.toUpperCase()); setOpen(true) }} onFocus={() => setOpen(true)} onBlur={() => setOpen(false)}
        onKeyDown={(e) => { if (e.key === 'Escape') setOpen(false); if (e.key === 'Enter' && open && shown.length === 1) { e.preventDefault(); pick(shown[0].value) } }}
        placeholder={placeholder} maxLength={30} autoComplete="off" autoCapitalize="characters" spellCheck={false} aria-label={label ?? placeholder} role="combobox" aria-expanded={open} />
      {open && (
        <ul className="sslist" role="listbox">
          {shown.map((o, i) => (
            <li key={o.value} role="option" aria-selected={o.value === value} className={`${o.value === value ? 'on' : ''}${o.group && o.group !== shown[i - 1]?.group ? ' first' : ''}`}
              data-group={o.group && o.group !== shown[i - 1]?.group ? o.group : undefined} onPointerDown={(e) => { e.preventDefault(); pick(o.value) }}>
              {o.value}{o.hint && <span className="sshint"> {o.hint}</span>}
            </li>
          ))}
          {!shown.length && <li className="none">{all.length ? 'No match' : 'Loading the list…'}</li>}
        </ul>
      )}
    </div>
  )
}
