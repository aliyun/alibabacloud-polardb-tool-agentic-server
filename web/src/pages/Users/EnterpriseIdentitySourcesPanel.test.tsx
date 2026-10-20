import { render, screen, waitFor, within } from '@testing-library/react'
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
  it('shows the public URL configuration error and leaves DingTalk verification retryable', async () => {
    const detail = 'Configure runtime_policy.external_base_url as an absolute HTTP(S) URL before DingTalk authorization.'
    vi.mocked(api.get).mockResolvedValue({ data: { items: [{
      id: 'dingtalk-1', name: 'DingTalk directory', provider: 'dingtalk', tenant_id: 'corp-1',
      status: 'pending_tenant_verification', last_synced_at: null, last_error: null,
      sync_supported: true, space_bindings: [],
    }], total: 1 } } as never)
    vi.mocked(api.post).mockRejectedValue({ isAxiosError: true, response: { data: { detail }, status: 409 } })
    const user = userEvent.setup()
    render(<EnterpriseIdentitySourcesPanel />)
    await user.click(await screen.findByRole('button', { name: 'Verify DingTalk enterprise' }))
    expect(await screen.findByText(detail)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Verify DingTalk enterprise' })).toBeEnabled()
    expect(api.post).toHaveBeenCalledWith('/api/identity-sources/dingtalk-1/dingtalk-verification')
  })

  it('edits the ETL database without revealing or resubmitting its saved password', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: { items: [{
      id: 'source-1', name: 'Feishu directory', provider: 'feishu', tenant_id: 'tenant-1', status: 'active',
      last_synced_at: null, last_error: null, sync_supported: true, space_bindings: [],
      acl_membership_snapshot_configured: true,
      acl_membership_snapshot: { dialect: 'postgresql', host: 'meta.example.test', port: 5432,
        database: 'polar_rag_meta', username: 'readonly' },
    }], total: 1 } } as never)
    vi.mocked(api.put).mockResolvedValue({ data: {} } as never)
    const user = userEvent.setup()
    render(<EnterpriseIdentitySourcesPanel />)
    await user.click(await screen.findByRole('button', { name: 'Permission data source' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByLabelText('Password')).toHaveValue('')
    expect(within(dialog).getByLabelText('Port')).toHaveValue('5432')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.put).toHaveBeenCalledWith('/api/identity-sources/source-1/acl-membership-snapshot', {
      dialect: 'postgresql', host: 'meta.example.test', port: 5432, database: 'polar_rag_meta', username: 'readonly',
    }))
  })
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
            total: 2,
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
    await user.click(within(await screen.findByRole('row', { name: /Product product-domain/ })).getByRole('checkbox'))
    await user.click(within(await screen.findByRole('row', { name: /Research research-domain/ })).getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: 'Bind selected' }))

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
    vi.mocked(api.get).mockImplementation((url: string, config) => {
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
        const params = config?.params as { binding_status?: string } | undefined
        const items = isBound && params?.binding_status === 'bound'
          ? [{ knowledge_space_id: 'space-1', name: 'Product', identity_domain: 'product-domain' }]
          : []
        return Promise.resolve({
          data: { items, total: items.length },
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
    await user.click(screen.getByRole('tab', { name: 'Bound Spaces' }))
    await user.click(within(await screen.findByRole('row', { name: /Product product-domain/ })).getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: 'Unbind selected' }))
    await user.click(
      (await screen.findAllByRole('button', { name: 'OK' })).find((button) => !button.hasAttribute('disabled'))!,
    )

    await waitFor(() => {
      expect(api.delete).toHaveBeenCalledWith('/api/identity-sources/source-1/spaces/space-1')
      expect(screen.queryByRole('row', { name: /Product product-domain/ })).not.toBeInTheDocument()
    })
  })

  it('lets an administrator synchronize an identity source immediately', async () => {
    const user = userEvent.setup()
    render(<EnterpriseIdentitySourcesPanel />)

    await user.click(await screen.findByRole('button', { name: 'Sync users and groups' }))

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith('/api/identity-sources/source-1/sync')
    })
  })

  it('shows the persisted partial membership warning separately from status', async () => {
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/api/identity-sources') {
        return Promise.resolve({
          data: {
            items: [{
              id: 'source-warning', name: 'Feishu warning', provider: 'feishu', tenant_id: 'tenant-1',
              status: 'active', last_synced_at: null, last_error: null, sync_supported: true,
              sync_warning: {
                code: 'MEMBERSHIPS_PARTIAL',
                skipped_count: 2,
                provider_errors: { '99991672': 2 },
              },
              acl_membership_snapshot_configured: true, space_bindings: [],
            }],
          },
        } as never)
      }
      if (url === '/api/identity-sources/spaces') {
        return Promise.resolve({ data: { items: [] } } as never)
      }
      return Promise.resolve({ data: {} } as never)
    })

    render(<EnterpriseIdentitySourcesPanel />)

    expect(await screen.findByText(
      '2 user groups failed membership sync; previous access was preserved.',
    )).toBeInTheDocument()
    expect(screen.getByText('active')).toBeInTheDocument()
  })

  it('shows Feishu long-connection failure and the conditional fallback', async () => {
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/api/identity-sources') {
        return Promise.resolve({
          data: {
            items: [{
              id: 'source-event-error', name: 'Feishu events', provider: 'feishu', tenant_id: 'tenant-1',
              status: 'active', last_synced_at: '2026-09-21T01:00:00Z', last_error: null, sync_supported: true,
              event_subscription: {
                status: 'error',
                fallback_after_seconds: 86400,
              },
              acl_membership_snapshot_configured: false, space_bindings: [],
            }],
          },
        } as never)
      }
      if (url === '/api/identity-sources/spaces') {
        return Promise.resolve({ data: { items: [] } } as never)
      }
      return Promise.resolve({ data: {} } as never)
    })

    render(<EnterpriseIdentitySourcesPanel />)

    expect(await screen.findByText('Subscription error')).toBeInTheDocument()
    expect(screen.getByText(
      'Groups and knowledge permissions still reconcile periodically. Start a full reconciliation manually, or wait for automatic full reconciliation.',
    )).toBeInTheDocument()
    expect(screen.getByText(/Last full sync:/)).toBeInTheDocument()
  })

  it('shows asynchronous full reconciliation after Sync now', async () => {
    const user = userEvent.setup()
    vi.mocked(api.post).mockResolvedValue({ status: 202, data: {} } as never)
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/api/identity-sources') {
        return Promise.resolve({ data: { items: [{
          id: 'source-1', name: 'Feishu directory', provider: 'feishu', tenant_id: 'tenant-1',
          status: 'active', last_synced_at: null, last_error: null, sync_supported: true,
          full_sync_in_progress: true, space_bindings: [],
        }] } } as never)
      }
      return Promise.resolve({ data: { items: [] } } as never)
    })

    render(<EnterpriseIdentitySourcesPanel />)
    expect(await screen.findByText('Full sync in progress')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Sync users and groups' }))
    expect(await screen.findByText(
      'Full sync in progress. Reconciliation will start after received incremental events finish processing.',
    )).toBeInTheDocument()
  })

  it.each([
    ['connecting', 'Connecting'],
    ['connected_unconfirmed', 'Subscription healthy'],
    ['active', 'Subscription healthy'],
  ] as const)('shows the last full sync for %s subscription status', async (status, label) => {
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/api/identity-sources') {
        return Promise.resolve({
          data: {
            items: [{
              id: 'source-event-waiting', name: 'Feishu events', provider: 'feishu', tenant_id: 'tenant-1',
              status: 'active', last_synced_at: null, last_error: null, sync_supported: true,
              event_subscription: {
                status,
                fallback_after_seconds: 86400,
              },
              acl_membership_snapshot_configured: false, space_bindings: [],
            }],
          },
        } as never)
      }
      if (url === '/api/identity-sources/spaces') {
        return Promise.resolve({ data: { items: [] } } as never)
      }
      return Promise.resolve({ data: {} } as never)
    })

    render(<EnterpriseIdentitySourcesPanel />)

    expect(await screen.findByText(label)).toBeInTheDocument()
    expect(screen.getByText('Last full sync: Never')).toBeInTheDocument()
  })

  it('shows and copies an identity source ID below its name', async () => {
    const user = userEvent.setup()
    const writeText = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue()
    render(<EnterpriseIdentitySourcesPanel />)

    expect(await screen.findByText('ID: source-1')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Copy identity source ID' }))

    expect(writeText).toHaveBeenCalledWith('source-1')
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
