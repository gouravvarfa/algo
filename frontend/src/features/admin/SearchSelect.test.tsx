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

describe('SearchSelect with hints and groups', () => {
  const OPTS = [
    { value: 'GOLD', hint: 'Gold (1 kg lot) · MCX', group: 'Gold · MCX' },
    { value: 'GOLDM', hint: 'Gold Mini (100 g lot) · MCX', group: 'Gold · MCX' },
    { value: 'SILVERMIC', hint: 'Silver Micro (1 kg lot) · MCX', group: 'Silver · MCX' },
    { value: 'RELIANCE', group: 'F&O stocks' },
  ]
  function H() {
    const [v, setV] = useState('')
    return <div><SearchSelect value={v} onChange={setV} options={OPTS} placeholder="pick" /><span data-testid="v">{v}</span></div>
  }
  it('finds a contract by its description as well as its symbol, and shows the hint', () => {
    render(<H />)
    const box = screen.getByRole('combobox')
    fireEvent.change(box, { target: { value: 'MICRO' } })
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(['SILVERMIC Silver Micro (1 kg lot) · MCX'])
    fireEvent.change(box, { target: { value: 'GOLD' } })
    expect(screen.getAllByRole('option').map((o) => o.textContent?.split(' ')[0])).toEqual(['GOLD', 'GOLDM'])
    fireEvent.pointerDown(screen.getByText('GOLDM'))
    expect(screen.getByTestId('v').textContent).toBe('GOLDM')
  })
  it('puts a heading above the first row of each group when nothing is typed', () => {
    render(<H />)
    fireEvent.focus(screen.getByRole('combobox'))
    const heads = screen.getAllByRole('option').filter((o) => o.getAttribute('data-group')).map((o) => o.getAttribute('data-group'))
    expect(heads).toEqual(['F&O stocks', 'Gold · MCX', 'Silver · MCX'])
  })
})
