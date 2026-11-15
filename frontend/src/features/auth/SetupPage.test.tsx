import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

const session = { refresh: vi.fn().mockResolvedValue(undefined) }
vi.mock('../../lib/session', () => ({ useSession: () => session }))

import { SetupPage } from './SetupPage'

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status })
const mount = () => render(<MemoryRouter initialEntries={['/setup/tok']}><Routes><Route path="/setup/:token" element={<SetupPage />} /></Routes></MemoryRouter>)
const type = (label: RegExp, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } })

describe('SetupPage after an authenticator reset', () => {
  afterEach(() => { vi.restoreAllMocks(); session.refresh.mockClear() })

  it('tells the person to delete the old authenticator entry', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve(json(200, { name: 'Yogesh', username: 'yogesh', qr_svg: '<svg/>', secret: 'ABCD EFGH' })))
    mount()
    expect(await screen.findByText(/delete it: only this new one works now/i)).toBeInTheDocument()
  })

  it('finishing signs the person in: no trip to the login page and no second code', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((_u, init) => Promise.resolve(init?.method === 'POST'
      ? json(200, { ok: true, username: 'yogesh', backup_codes: ['AAAA-BBBB'], signed_in: true })
      : json(200, { name: 'Yogesh', username: 'yogesh', qr_svg: '<svg/>', secret: 'ABCD EFGH' })))
    mount()
    await screen.findByText(/Welcome, Yogesh/)
    type(/^password/i, 'a brand new long password')
    type(/repeat password/i, 'a brand new long password')
    type(/6-digit code/i, '123456')
    fireEvent.click(screen.getByRole('button', { name: /finish setup/i }))
    expect(await screen.findByRole('link', { name: /open the app/i })).toHaveAttribute('href', '/')
    expect(screen.queryByRole('link', { name: /continue to sign in/i })).toBeNull()
    expect(session.refresh).toHaveBeenCalled()
  })
})
