import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { expect, it, vi } from 'vitest'
import { listAccounts, listGrants, saveSQLGrant } from '../../api/accessConsole'
import { listInstanceCredentials } from '../../api/credentials'
import GrantPanel from './GrantPanel'
vi.mock('../../api/accessConsole', () => ({
  listAccounts: vi.fn(),
  listGrants: vi.fn(),
  listResources: vi.fn(),
  saveSQLGrant: vi.fn(),
}))
vi.mock('../../api/credentials', () => ({ listInstanceCredentials: vi.fn() }))
it('sends only a SQL preset to the shared grant endpoint', async () => {
  const user = userEvent.setup()
  vi.mocked(listGrants).mockResolvedValue({
    data: {
      items: [
        {
          id: 'grant',
          account_kind: 'personal',
          account_id: 'alice',
          name: 'Alice',
          resource_id: 'db',
          resource_name: 'Reports',
          source: 'admin',
          enabled: true,
          permission: 'readonly',
          credential_id: 'credential',
        },
      ],
      total: 1,
    },
  } as never)
  vi.mocked(listAccounts).mockResolvedValue({ data: { items: [] } } as never)
  vi.mocked(listInstanceCredentials).mockResolvedValue({
    data: {
      items: [
        {
          id: 'credential',
          name: 'Read account',
          status: 'active',
          purpose: 'direct_access',
          capability: 'readonly',
        },
      ],
      total: 1,
    },
  } as never)
  vi.mocked(saveSQLGrant).mockResolvedValue({} as never)
  render(
    <MemoryRouter>
      <GrantPanel resourceId="db" />
    </MemoryRouter>,
  )
  await user.click(await screen.findByRole('button', { name: 'Edit' }))
  await screen.findByText('Read account · Query data')
  await user.click(screen.getByRole('button', { name: 'OK' }))
  await waitFor(() => expect(saveSQLGrant).toHaveBeenCalled())
  expect(saveSQLGrant).toHaveBeenCalledWith(
    'personal',
    'alice',
    'db',
    'credential',
    'readonly',
  )
})
