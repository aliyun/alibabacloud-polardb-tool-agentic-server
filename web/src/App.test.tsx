import { act, render, screen, waitFor } from '@testing-library/react'
import { Outlet } from 'react-router-dom'
import { beforeEach, expect, it, vi } from 'vitest'

import App from './App'
import { discoverSystemState } from './api/configuration'
import { useAuth } from './hooks/useAuth'

vi.mock('./api/configuration', () => ({
  discoverSystemState: vi.fn(),
}))

vi.mock('./hooks/useAuth', () => ({
  useAuth: vi.fn(),
}))

vi.mock('./components/Layout', () => ({
  default: () => <Outlet />,
}))

vi.mock('./pages/AgentDetail', () => ({
  default: () => <h1>Agent detail destination</h1>,
}))

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

const admin = {
  id: 'admin-1',
  external_id: 'admin',
  display_name: 'Administrator',
  email: null,
  role: 'admin',
  status: 'active',
}

beforeEach(() => {
  vi.clearAllMocks()
  window.history.replaceState(
    {},
    '',
    '/agents/a0e55263-3e7f-41e5-a5fb-fdc6d821577b',
  )
})

it('preserves an admin detail route while authentication loads', async () => {
  const discovery = deferred<'READY'>()
  vi.mocked(discoverSystemState).mockReturnValue(discovery.promise)
  vi.mocked(useAuth).mockReturnValue({
    user: null,
    loading: true,
    login: vi.fn(),
    logout: vi.fn(),
    isAdmin: false,
    authMode: 'builtin',
  })
  const rendered = render(<App />)

  await act(async () => {
    discovery.resolve('READY')
    await discovery.promise
  })

  expect(window.location.pathname).toBe(
    '/agents/a0e55263-3e7f-41e5-a5fb-fdc6d821577b',
  )

  vi.mocked(useAuth).mockReturnValue({
    user: admin,
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
    isAdmin: true,
    authMode: 'builtin',
  })
  rendered.rerender(<App />)

  expect(
    await screen.findByRole('heading', {
      name: 'Agent detail destination',
    }),
  ).toBeInTheDocument()
  expect(window.location.pathname).toBe(
    '/agents/a0e55263-3e7f-41e5-a5fb-fdc6d821577b',
  )
})

it('redirects a non-admin away from an admin detail route', async () => {
  vi.mocked(discoverSystemState).mockResolvedValue('READY')
  vi.mocked(useAuth).mockReturnValue({
    user: {
      ...admin,
      id: 'member-1',
      external_id: 'member',
      display_name: 'Member',
      role: 'member',
    },
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
    isAdmin: false,
    authMode: 'builtin',
  })

  render(<App />)

  await waitFor(() =>
    expect(window.location.pathname).toBe('/dashboard'),
  )
  expect(
    screen.queryByRole('heading', {
      name: 'Agent detail destination',
    }),
  ).not.toBeInTheDocument()
})
