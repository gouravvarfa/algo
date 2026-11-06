import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import { SearchSelect } from './SearchSelect'

const SYMS = ['RELIANCE', 'RECLTD', 'TCS', 'INFY', 'HDFCBANK', 'AXISBANK']
function Host() {
  const [v, setV] = useState('')
  return <div><SearchSelect value={v} onChange={setV} options={SYMS} placeholder="pick" /><span data-testid="v">{v}</span></div>
}

describe('SearchSelect', () => {
  it('opens on tap, narrows while typing (starts-with first), and a tap on a row picks it', () => {
    render(<Host />)
    const box = screen.getByRole('combobox')
    fireEvent.focus(box)
    expect(screen.getAllByRole('option')).toHaveLength(SYMS.length)
    fireEvent.change(box, { target: { value: 're' } })
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(['RELIANCE', 'RECLTD'])
    fireEvent.change(box, { target: { value: 'bank' } })
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(['HDFCBANK', 'AXISBANK'])      // found by "contains" when nothing starts with it
    fireEvent.pointerDown(screen.getByText('AXISBANK'))
    expect(screen.getByTestId('v').textContent).toBe('AXISBANK')
    expect(screen.queryByRole('listbox')).toBeNull()
  })
  it('says so when nothing matches', () => {
    render(<Host />)
    const box = screen.getByRole('combobox')
    fireEvent.change(box, { target: { value: 'zzz' } })
    expect(screen.getByText('No match')).toBeTruthy()
  })
})
