import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const session = { mode: 'platform', me: null, loading: false, role: null, can: () => false, endReason: null, refresh: vi.fn().mockResolvedValue(undefined), logout: vi.fn() }
vi.mock('../../lib/session', () => ({ useSession: () => session }))

import { LoginPage } from './LoginPage'

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status })
const type = (label: RegExp | string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } })
const fill = () => { type(/username/i, 'rahul'); type(/^password/i, 'a long password!'); type(/authenticator code/i, '123456') }
const mountAt = (url = '/login') => render(<MemoryRouter initialEntries={[url]}><LoginPage /></MemoryRouter>)

describe('LoginPage', () => {
  beforeEach(() => { session.mode = 'platform'; session.me = null })
  afterEach(() => vi.restoreAllMocks())

  it('tells people what to do when they forgot the password or lost the phone', () => {
    mountAt()
    expect(screen.getByText(/Ask your admin to reset it/i)).toBeInTheDocument()
    expect(screen.getByText(/Need access\? Ask your admin for an invite/i)).toBeInTheDocument()
  })

  it('asks for all three things before it calls the server', () => {
    const f = vi.spyOn(globalThis, 'fetch')
    mountAt()
    fireEvent.click(screen.getByRole('button', { name: /^sign in$/i }))
    expect(screen.getByRole('alert')).toHaveTextContent(/Enter your username, password and authenticator code/i)
    expect(f).not.toHaveBeenCalled()
  })

  it('says only "invalid" for a wrong login and clears the code', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve(json(401, { error: 'INVALID_CREDENTIALS', message: 'Invalid credentials' })))
    mountAt()
    fill()
    fireEvent.click(screen.getByRole('button', { name: /^sign in$/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid username, password or code.')
    expect(screen.getByLabelText(/authenticator code/i)).toHaveValue('')
  })

  it('refuses a second device, offers the takeover, and sends replace_existing only when asked', async () => {
    const calls: Record<string, unknown>[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation((_url, init) => {
      const body = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>
      calls.push(body)
      return Promise.resolve(body.replace_existing
        ? json(200, { ok: true })
        : json(409, { error: 'ALREADY_LOGGED_IN', message: 'This account is already logged in on another device. Log out there first.', device: 'Chrome · Windows', last_active: '2026-10-22T10:49:10+05:30', can_take_over: true }))
    })
    mountAt()
    fill()
    fireEvent.click(screen.getByRole('button', { name: /^sign in$/i }))
    const takeover = await screen.findByRole('button', { name: /Sign out the other device and sign in here/i })
    expect(screen.getByRole('alert')).toHaveTextContent(/already logged in on another device.*Chrome · Windows.*2026-10-22 10:49/)
    expect(calls[0].replace_existing).toBe(false)
    fireEvent.click(takeover)
    await waitFor(() => expect(calls).toHaveLength(2))
    expect(calls[1]).toMatchObject({ username: 'rahul', replace_existing: true })      // the credentials are sent again: the server checks them again
    await waitFor(() => expect(session.refresh).toHaveBeenCalled())
  })

  it('shows no takeover button when the deployment switched it off', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve(json(409, { error: 'ALREADY_LOGGED_IN', message: 'Log out there first.', device: 'Chrome', last_active: '', can_take_over: false })))
    mountAt()
    fill()
    fireEvent.click(screen.getByRole('button', { name: /^sign in$/i }))
    await screen.findByRole('alert')
    expect(screen.queryByRole('button', { name: /Sign out the other device/i })).toBeNull()
  })

  it('explains why a session ended', () => {
    mountAt('/login?why=replaced')
    expect(screen.getByText(/signed in on another device/i)).toBeInTheDocument()
  })

  it('shows the old single-user form when the server still runs the old login', () => {
    session.mode = 'legacy'
    mountAt('/login?e=1')
    expect(screen.getByText('Wrong username or password.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /sign in/i }).closest('form')).toHaveAttribute('action', '/login')
  })
})
