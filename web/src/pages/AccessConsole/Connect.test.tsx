import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, it, vi } from 'vitest'
import {
  issuePersonalToken,
  listPersonalTokens,
  revokePersonalToken,
} from '../../api/accessConsole'
import Connect from './Connect'
vi.mock('../../api/accessConsole', () => ({
  listPersonalTokens: vi.fn(),
  issuePersonalToken: vi.fn(),
  revokePersonalToken: vi.fn(),
}))
vi.mock('./MyResources', () => ({ default: () => <div /> }))
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listPersonalTokens).mockResolvedValue({
    data: { items: [], mcp_url: 'https://pas.test/mcp/personal' },
  } as never)
})
it('uses the personal endpoint and shows an issued token only once', async () => {
  const user = userEvent.setup()
  vi.mocked(issuePersonalToken).mockResolvedValue({
    data: { id: 'token-1', status: 'active', token: 'test-personal-secret' },
  } as never)
  render(
    <MemoryRouter>
      <Connect />
    </MemoryRouter>,
  )
  expect(await screen.findByText('https://pas.test/mcp/personal')).toBeVisible()
  await user.click(screen.getByRole('tab', { name: 'Token connection' }))
  await user.click(
    screen.getByRole('button', { name: 'Generate personal token' }),
  )
  await waitFor(() =>
    expect(screen.getByText('test-personal-secret')).toBeVisible(),
  )
  expect(issuePersonalToken).toHaveBeenCalledWith(90)
  await user.click(screen.getByRole('button', { name: 'I have saved it' }))
  await waitFor(() =>
    expect(screen.queryByText('test-personal-secret')).not.toBeInTheDocument(),
  )
  expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
})
it('requires revocation before another token and reports errors', async () => {
  const user = userEvent.setup()
  vi.mocked(listPersonalTokens).mockResolvedValue({
    data: {
      items: [
        { id: 'token-1', status: 'active', expires_at: '2027-01-01T00:00:00Z' },
      ],
      mcp_url: 'https://pas.test/mcp/personal',
    },
  } as never)
  vi.mocked(revokePersonalToken).mockResolvedValue({} as never)
  render(
    <MemoryRouter>
      <Connect />
    </MemoryRouter>,
  )
  await screen.findByText('https://pas.test/mcp/personal')
  await user.click(screen.getByRole('tab', { name: 'Token connection' }))
  expect(
    screen.getByRole('button', { name: 'Generate personal token' }),
  ).toBeDisabled()
  await user.click(screen.getByRole('button', { name: 'Revoke token' }))
  await user.click(screen.getByRole('button', { name: 'OK' }))
  await waitFor(() =>
    expect(revokePersonalToken).toHaveBeenCalledWith('token-1'),
  )
})

it('expands a local URL and explains when OAuth is not configured', async () => {
  const user = userEvent.setup()
  vi.mocked(listPersonalTokens).mockResolvedValue({
    data: { items: [], mcp_url: '/mcp/personal', oauth_ready: false },
  } as never)
  render(
    <MemoryRouter>
      <Connect />
    </MemoryRouter>,
  )
  expect(
    await screen.findByText(
      new URL('/mcp/personal', window.location.origin).toString(),
    ),
  ).toBeInTheDocument()
  await user.click(
    screen.getByRole('tab', { name: 'OAuth sign-in · recommended' }),
  )
  expect(
    screen.getByText(/OAuth requires an administrator/),
  ).toBeInTheDocument()
})
