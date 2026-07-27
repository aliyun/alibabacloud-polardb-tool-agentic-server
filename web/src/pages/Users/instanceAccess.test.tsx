import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from '../../api/client'
import { listInstanceCredentials } from '../../api/credentials'
import {
  getUserInstanceAccess,
  listInstances,
  updateUserInstanceAccess,
} from '../../api/instanceAccess'
import Users from './index'

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    default: {
      get: vi.fn(),
      put: vi.fn(),
    },
  }
})

vi.mock('../../api/credentials', () => ({
  listInstanceCredentials: vi.fn(),
}))

vi.mock('../../api/instanceAccess', () => ({
  getUserInstanceAccess: vi.fn(),
  listInstances: vi.fn(),
  updateUserInstanceAccess: vi.fn(),
}))

const member = {
  id: 'user-1',
  external_id: 'reporter',
  display_name: 'Production reporter',
  email: 'reporter@example.com',
  role: 'member',
  status: 'active',
  provisioning_mode: 'dedicated',
  departments: [],
}

const autoInstance = {
  id: 'auto-1',
  cluster_id: 'pc-auto',
  name: 'Personal production',
  engine: 'polardb_mysql',
  topology: 'single_tenant',
  allocation_mode: 'auto_provisioned',
  status: 'active',
  region: null,
  host: null,
  port: null,
  owner_user_id: member.id,
  health: null,
  binding_counts: { users: 1, departments: 0, agents: 0 },
}

const registeredInstance = {
  ...autoInstance,
  id: 'registered-1',
  cluster_id: 'pc-registered',
  name: 'Registered production',
  allocation_mode: 'registered',
  owner_user_id: null,
}

const credential = {
  id: 'credential-1',
  instance_id: registeredInstance.id,
  resource_id: null,
  name: 'Read-only application',
  purpose: 'direct_access' as const,
  capability: 'readonly' as const,
  database_name: 'analytics',
  status: 'active' as const,
  version: 1,
  created_by_user_id: 'admin-1',
  created_at: '2026-07-26T00:00:00Z',
  updated_at: null,
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

describe('User instance access editor', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/api/users') {
        return Promise.resolve({
          data: { items: [member], total: 1, offset: 0, limit: 20 },
        } as never)
      }
      if (url === '/auth/mode') {
        return Promise.resolve({ data: { mode: 'builtin' } } as never)
      }
      return Promise.resolve({ data: [] } as never)
    })
    vi.mocked(listInstances).mockResolvedValue({
      items: [autoInstance, registeredInstance],
      total: 2,
      offset: 0,
      limit: 200,
    } as never)
    vi.mocked(getUserInstanceAccess).mockImplementation((_userId, instanceId) => {
      if (instanceId === autoInstance.id) {
        return Promise.resolve({
          data: {
            id: 'binding-system',
            user_id: member.id,
            instance_id: autoInstance.id,
            credential_id: 'credential-auto',
            permission: 'readwrite',
            capabilities: ['sql:read', 'sql:write'],
            enabled: true,
            origin: 'system',
            created_at: '2026-07-26T00:00:00Z',
            updated_at: null,
          },
        } as never)
      }
      return Promise.reject({ response: { status: 404 } })
    })
    vi.mocked(listInstanceCredentials).mockImplementation((instanceId) =>
      Promise.resolve({
        data: instanceId === registeredInstance.id ? [credential] : [],
      } as never),
    )
  })

  it('lists the user owned and registered instances and keeps SQL grants read-only', async () => {
    const user = userEvent.setup()
    render(<Users />)

    await user.click(
      await screen.findByRole('button', {
        name: /instance access for production reporter/i,
      }),
    )
    expect(
      (await screen.findAllByText('Personal production')).length,
    ).toBeGreaterThan(0)
    expect(screen.getByText('Registered production')).toBeInTheDocument()
    expect(getUserInstanceAccess).toHaveBeenCalledTimes(1)
    expect(getUserInstanceAccess).toHaveBeenCalledWith(member.id, autoInstance.id)
    expect(listInstanceCredentials).toHaveBeenCalledTimes(1)
    expect(screen.getByText('SQL read')).toBeInTheDocument()
    expect(screen.getByText('SQL write')).toBeInTheDocument()
    expect(
      screen.getByRole('combobox', {
        name: /requested sql permission ceiling/i,
      }),
    ).toBeDisabled()
    expect(screen.queryByLabelText(/grant sql write/i)).not.toBeInTheDocument()

    await user.click(
      screen.getByRole('button', { name: /registered production/i }),
    )
    await waitFor(() =>
      expect(getUserInstanceAccess).toHaveBeenCalledWith(
        member.id,
        registeredInstance.id,
      ),
    )
    expect(getUserInstanceAccess).toHaveBeenCalledTimes(2)
  })

  it('grants describe and credentials independently with dependency expansion', async () => {
    const user = userEvent.setup()
    vi.mocked(updateUserInstanceAccess).mockResolvedValue({
      data: {
        id: 'binding-admin',
        user_id: member.id,
        instance_id: registeredInstance.id,
        credential_id: credential.id,
        permission: 'readonly',
        capabilities: [
          'db_instance:list',
          'db_instance:describe',
          'db_instance:credentials:read',
        ],
        enabled: true,
        origin: 'admin',
        created_at: '2026-07-26T00:00:00Z',
        updated_at: null,
      },
    } as never)
    render(<Users />)

    await user.click(
      await screen.findByRole('button', {
        name: /instance access for production reporter/i,
      }),
    )
    await user.click(
      await screen.findByRole('button', { name: /registered production/i }),
    )
    expect(screen.getByLabelText(/view instance metadata/i)).not.toBeChecked()
    expect(screen.getByLabelText(/reveal credentials/i)).not.toBeChecked()

    await user.click(screen.getByLabelText(/reveal credentials/i))
    expect(screen.getByLabelText(/list bound instances/i)).toBeChecked()
    expect(screen.getByLabelText(/view instance metadata/i)).toBeChecked()
    await user.click(
      screen.getByRole('combobox', { name: /direct-access credential/i }),
    )
    await user.click(screen.getByText(/read-only application · readonly/i))
    await user.click(screen.getByRole('button', { name: /save instance access/i }))

    await waitFor(() =>
      expect(updateUserInstanceAccess).toHaveBeenCalledWith(
        member.id,
        registeredInstance.id,
        {
          credential_id: credential.id,
          permission: 'readonly',
          capabilities: [
            'db_instance:list',
            'db_instance:describe',
            'db_instance:credentials:read',
          ],
          enabled: true,
        },
      ),
    )
  })

  it('does not grant credential retrieval when only metadata is selected', async () => {
    const user = userEvent.setup()
    vi.mocked(updateUserInstanceAccess).mockResolvedValue({ data: {} } as never)
    render(<Users />)

    await user.click(
      await screen.findByRole('button', {
        name: /instance access for production reporter/i,
      }),
    )
    await user.click(
      await screen.findByRole('button', { name: /registered production/i }),
    )
    await user.click(screen.getByLabelText(/view instance metadata/i))
    await user.click(
      screen.getByRole('combobox', { name: /direct-access credential/i }),
    )
    await user.click(screen.getByText(/read-only application · readonly/i))
    await user.click(screen.getByRole('button', { name: /save instance access/i }))

    await waitFor(() =>
      expect(updateUserInstanceAccess).toHaveBeenCalled(),
    )
    const payload = vi.mocked(updateUserInstanceAccess).mock.calls[0][2]
    expect(payload.capabilities).toEqual([
      'db_instance:list',
      'db_instance:describe',
    ])
    expect(payload.capabilities).not.toContain(
      'db_instance:credentials:read',
    )
  })

  it('isolates a selected instance load failure and keeps cached access usable', async () => {
    const user = userEvent.setup()
    vi.mocked(getUserInstanceAccess).mockImplementation((_userId, instanceId) => {
      if (instanceId === autoInstance.id) {
        return Promise.resolve({
          data: {
            id: 'binding-system',
            user_id: member.id,
            instance_id: autoInstance.id,
            credential_id: 'credential-auto',
            permission: 'readwrite',
            capabilities: ['sql:read', 'sql:write'],
            enabled: true,
            origin: 'system',
            created_at: '2026-07-26T00:00:00Z',
            updated_at: null,
          },
        } as never)
      }
      return Promise.reject(new Error('target unavailable'))
    })
    render(<Users />)

    await user.click(
      await screen.findByRole('button', {
        name: /instance access for production reporter/i,
      }),
    )
    expect(await screen.findByText('SQL write')).toBeInTheDocument()
    await user.click(
      screen.getByRole('button', { name: /registered production/i }),
    )
    expect(
      await screen.findByText(/could not load access for this instance/i),
    ).toBeInTheDocument()
    await user.click(
      screen.getByRole('button', { name: /personal production/i }),
    )
    expect(screen.getByText('SQL write')).toBeInTheDocument()
    expect(getUserInstanceAccess).toHaveBeenCalledTimes(2)
  })

  it('ignores a save response after the administrator switches instances', async () => {
    const user = userEvent.setup()
    const pending = deferred<{
      data: {
        id: string
        user_id: string
        instance_id: string
        credential_id: string
        permission: 'readonly'
        capabilities: ['db_instance:list']
        enabled: true
        origin: 'admin'
        created_at: string
        updated_at: null
      }
    }>()
    vi.mocked(updateUserInstanceAccess).mockReturnValue(
      pending.promise as never,
    )
    render(<Users />)

    await user.click(
      await screen.findByRole('button', {
        name: /instance access for production reporter/i,
      }),
    )
    await user.click(
      await screen.findByRole('button', { name: /registered production/i }),
    )
    await user.click(screen.getByLabelText(/list bound instances/i))
    await user.click(
      screen.getByRole('combobox', { name: /direct-access credential/i }),
    )
    await user.click(screen.getByText(/read-only application · readonly/i))
    await user.click(screen.getByRole('button', { name: /save instance access/i }))
    await user.click(
      screen.getByRole('button', { name: /personal production/i }),
    )

    pending.resolve({
      data: {
        id: 'binding-admin',
        user_id: member.id,
        instance_id: registeredInstance.id,
        credential_id: credential.id,
        permission: 'readonly',
        capabilities: ['db_instance:list'],
        enabled: true,
        origin: 'admin',
        created_at: '2026-07-26T00:00:00Z',
        updated_at: null,
      },
    })

    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: /personal production/i }),
      ).toHaveAttribute('aria-pressed', 'true'),
    )
    expect(
      screen.getByLabelText(/list bound instances/i),
    ).not.toBeDisabled()
    expect(
      screen.queryByText(/user may need to reconnect/i),
    ).not.toBeInTheDocument()
  })
})
