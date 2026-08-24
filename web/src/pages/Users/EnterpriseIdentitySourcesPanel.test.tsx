import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from '../../api/client'
import EnterpriseIdentitySourcesPanel from './EnterpriseIdentitySourcesPanel'

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    default: {
      delete: vi.fn(),
      get: vi.fn(),
      post: vi.fn(),
      put: vi.fn(),
    },
  }
})

describe('Enterprise identity sources', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/api/identity-sources') {
        return Promise.resolve({
          data: {
            items: [
              {
                id: 'source-1',
                name: 'Feishu directory',
                provider: 'feishu',
                tenant_id: 'tenant-1',
                status: 'active',
                last_synced_at: null,
                last_error: null,
                sync_supported: true,
                acl_membership_snapshot_configured: true,
                space_bindings: [],
              },
            ],
          },
        } as never)
      }
      if (url === '/api/identity-sources/spaces') {
        return Promise.resolve({
          data: {
            items: [
              {
                knowledge_space_id: 'space-1',
                name: 'Product',
                identity_domain: 'product-domain',
              },
              {
                knowledge_space_id: 'space-2',
                name: 'Research',
                identity_domain: 'research-domain',
              },
            ],
          },
        } as never)
      }
      return Promise.resolve({ data: {} } as never)
    })
    vi.mocked(api.post).mockResolvedValue({ data: {} } as never)
  })

  it('binds all selected Spaces to one identity source', async () => {
    const user = userEvent.setup()
    render(<EnterpriseIdentitySourcesPanel />)

    await user.click(await screen.findByRole('button', { name: 'Bind Space' }))
    await user.click(screen.getByRole('combobox'))
    await user.click(await screen.findByText('Product · product-domain'))
    await user.click(await screen.findByText('Research · research-domain'))
    await user.click(screen.getByRole('button', { name: 'OK' }))

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        '/api/identity-sources/source-1/spaces/space-1',
      )
      expect(api.post).toHaveBeenCalledWith(
        '/api/identity-sources/source-1/spaces/space-2',
      )
    })
  })

  it('unbinds an already bound Space', async () => {
    let isBound = true
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/api/identity-sources') {
        return Promise.resolve({
          data: {
            items: [{
              id: 'source-1', name: 'Feishu directory', provider: 'feishu', tenant_id: 'tenant-1',
              status: 'active', last_synced_at: null, last_error: null, sync_supported: true,
              acl_membership_snapshot_configured: true, space_bindings: isBound ? ['space-1'] : [],
            }],
          },
        } as never)
      }
      if (url === '/api/identity-sources/spaces') {
        return Promise.resolve({
          data: { items: [{ knowledge_space_id: 'space-1', name: 'Product', identity_domain: 'product-domain' }] },
        } as never)
      }
      return Promise.resolve({ data: {} } as never)
    })
    vi.mocked(api.delete).mockImplementation(async () => {
      isBound = false
      return { data: {} } as never
    })
    const user = userEvent.setup()
    render(<EnterpriseIdentitySourcesPanel />)

    await user.click(await screen.findByRole('button', { name: 'Bind Space' }))
    await user.click(await screen.findByRole('button', { name: 'Unbind' }))
    await user.click(
      (await screen.findAllByRole('button', { name: 'OK' })).find((button) => !button.hasAttribute('disabled'))!,
    )

    await waitFor(() => {
      expect(api.delete).toHaveBeenCalledWith('/api/identity-sources/source-1/spaces/space-1')
      expect(screen.queryByText('Product · product-domain')).not.toBeInTheDocument()
    })
  })

  it('lets an administrator synchronize an identity source immediately', async () => {
    const user = userEvent.setup()
    render(<EnterpriseIdentitySourcesPanel />)

    await user.click(await screen.findByRole('button', { name: 'Sync now' }))

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith('/api/identity-sources/source-1/sync')
    })
  })

  it('loads each synced directory entry type through server pagination', async () => {
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/api/identity-sources') {
        return Promise.resolve({
          data: {
            items: [{
              id: 'source-1', name: 'Feishu directory', provider: 'feishu', tenant_id: 'tenant-1',
              status: 'active', last_synced_at: null, last_error: null, sync_supported: true,
              acl_membership_snapshot_configured: true, space_bindings: [],
            }],
          },
        } as never)
      }
      if (url === '/api/identity-sources/spaces') {
        return Promise.resolve({ data: { items: [] } } as never)
      }
      if (url.includes('/directory?entry_type=users')) {
        return Promise.resolve({
          data: { users: [{ external_user_id: 'user-001', display_name: 'User 001', email: null }], groups: [], total: 205 },
        } as never)
      }
      if (url.includes('/directory?entry_type=groups')) {
        return Promise.resolve({
          data: { users: [], groups: [{ external_group_id: 'group-001', display_name: 'Group 001', principal_type: 'group' }], total: 205 },
        } as never)
      }
      return Promise.resolve({ data: {} } as never)
    })
    const user = userEvent.setup()
    render(<EnterpriseIdentitySourcesPanel />)

    await user.click(await screen.findByRole('button', { name: 'Synced identities' }))

    await waitFor(() => {
      expect(api.get).toHaveBeenCalledWith(
        '/api/identity-sources/source-1/directory?entry_type=users&offset=0&limit=20',
      )
      expect(api.get).toHaveBeenCalledWith(
        '/api/identity-sources/source-1/directory?entry_type=groups&offset=0&limit=20',
      )
    })
  })
})
