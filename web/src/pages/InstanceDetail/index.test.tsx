import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from '../../api/client'
import {
  createInstanceCredential,
  listInstanceCredentials,
  revealCredential,
  revokeCredential,
  testInstanceCredentialConnection,
  updateCredential,
} from '../../api/credentials'
import {
  createProvisioningBackend,
  disableProvisioningBackend,
  drainProvisioningBackend,
  listProvisioningBackends,
} from '../../api/provisioningBackends'
import InstanceDetail from './index'

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    default: {
      get: vi.fn(),
      post: vi.fn(),
      put: vi.fn(),
    },
  }
})

vi.mock('../../api/credentials', () => ({
  createInstanceCredential: vi.fn(),
  listInstanceCredentials: vi.fn(),
  revealCredential: vi.fn(),
  revokeCredential: vi.fn(),
  testInstanceCredentialConnection: vi.fn(),
  updateCredential: vi.fn(),
}))

vi.mock('../../api/provisioningBackends', () => ({
  createProvisioningBackend: vi.fn(),
  disableProvisioningBackend: vi.fn(),
  drainProvisioningBackend: vi.fn(),
  listProvisioningBackends: vi.fn(),
  updateProvisioningBackend: vi.fn(),
}))

const instance = {
  id: 'instance-1',
  cluster_id: 'pc-production',
  name: 'Production tenant host',
  usage: 'Shared tenant provisioning',
  engine: 'polardb_mysql',
  topology: 'multitenant',
  allocation_mode: 'registered',
  status: 'active',
  region: 'cn-hangzhou',
  host: 'production.example.invalid',
  port: 3306,
  owner_user_id: null,
  health: {
    healthy: true,
    checked_at: '2026-07-26T00:00:00Z',
    consecutive_failures: 0,
    error_code: null,
  },
  binding_counts: { users: 1, departments: 0, agents: 2 },
}

const credential = {
  id: 'credential-1',
  instance_id: instance.id,
  resource_id: null,
  name: 'Provisioning administrator',
  purpose: 'provisioning_admin' as const,
  capability: 'admin' as const,
  database_name: null,
  status: 'active' as const,
  version: 1,
  created_by_user_id: 'admin-1',
  created_at: '2026-07-26T00:00:00Z',
  updated_at: null,
}

const backend = {
  id: 'backend-1',
  instance_id: instance.id,
  admin_credential_id: credential.id,
  status: 'active' as const,
  priority: 10,
  max_active_resources: 20,
  resource_min_cpu: 1,
  resource_max_cpu: 4,
  ddl_concurrency: 2,
  config_revision: 1,
  created_at: '2026-07-26T00:00:00Z',
  updated_at: null,
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/instances/instance-1']}>
      <Routes>
        <Route path="/instances/:id" element={<InstanceDetail />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('Instance detail administration', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.get).mockResolvedValue({ data: instance } as never)
    vi.mocked(listInstanceCredentials).mockResolvedValue({
      data: [credential],
    } as never)
    vi.mocked(listProvisioningBackends).mockResolvedValue({
      data: [backend],
    } as never)
  })

  it('separates credential and provisioning backend administration', async () => {
    renderPage()

    expect(
      await screen.findByRole('heading', { name: /credentials/i }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { name: /provisioning backend/i }),
    ).toBeInTheDocument()
    expect(screen.getByText('polardb_mysql')).toBeInTheDocument()
    expect(screen.getByText('Shared tenant provisioning')).toBeInTheDocument()
    expect(screen.queryByText(/^shared$/i)).not.toBeInTheDocument()
  })

  it('edits registered instance metadata and endpoint from the detail page', async () => {
    const user = userEvent.setup()
    vi.mocked(api.put).mockResolvedValue({
      data: {
        ...instance,
        name: 'Production reporting',
        usage: 'Read-only reporting workloads',
        region: 'cn-beijing',
        host: 'reporting.example.invalid',
        port: 3307,
      },
    } as never)
    vi.mocked(api.post).mockResolvedValue({ data: { ok: true } } as never)
    renderPage()

    await user.click(
      await screen.findByRole('button', { name: /edit instance/i }),
    )
    expect(screen.getByLabelText(/^cluster id$/i)).toHaveValue(
      'pc-production',
    )
    expect(screen.getByLabelText(/^cluster id$/i)).toBeDisabled()

    const name = screen.getByLabelText(/^instance name$/i)
    await user.clear(name)
    await user.type(name, 'Production reporting')
    const usage = screen.getByLabelText(/^usage$/i)
    await user.clear(usage)
    await user.type(usage, 'Read-only reporting workloads')
    const region = screen.getByLabelText(/^region$/i)
    await user.clear(region)
    await user.type(region, 'cn-beijing')
    const host = screen.getByLabelText(/^host$/i)
    await user.clear(host)
    await user.type(host, 'reporting.example.invalid')
    const port = screen.getByLabelText(/^port$/i)
    await user.clear(port)
    await user.type(port, '3307')
    await user.click(
      screen.getByRole('button', { name: /test connection/i }),
    )
    expect(
      await screen.findByText(/connection succeeded/i),
    ).toBeInTheDocument()
    await user.click(
      screen.getByRole('button', { name: /save instance/i }),
    )

    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith('/api/instances/instance-1', {
        name: 'Production reporting',
        usage: 'Read-only reporting workloads',
        region: 'cn-beijing',
        host: 'reporting.example.invalid',
        port: 3307,
        test_credential_id: credential.id,
      }),
    )
    expect(
      await screen.findByRole('heading', {
        name: 'Production reporting',
      }),
    ).toBeInTheDocument()
    expect(screen.getByText('Read-only reporting workloads')).toBeInTheDocument()
    expect(
      screen.getByText('reporting.example.invalid:3307'),
    ).toBeInTheDocument()
  })

  it('creates credentials without retaining plaintext and reveals only after confirmation', async () => {
    const user = userEvent.setup()
    vi.mocked(createInstanceCredential).mockResolvedValue({
      data: {
        ...credential,
        id: 'credential-2',
        name: 'Application reader',
        purpose: 'direct_access',
        capability: 'readonly',
      },
    } as never)
    vi.mocked(revealCredential).mockResolvedValue({
      data: {
        username: 'reporter',
        password: 'temporary-secret',
        database_name: 'analytics',
      },
    } as never)
    renderPage()

    await user.click(
      await screen.findByRole('button', { name: /add credential/i }),
    )
    await user.type(screen.getByLabelText(/^credential name$/i), 'Application reader')
    expect(screen.getByText('Direct access')).toBeInTheDocument()
    expect(screen.getByText('Read only')).toBeInTheDocument()
    await user.type(screen.getByLabelText(/^username$/i), 'reporter')
    await user.type(screen.getByLabelText(/^password$/i), 'temporary-secret')
    await user.click(screen.getByRole('button', { name: /save credential/i }))

    await waitFor(() =>
      expect(createInstanceCredential).toHaveBeenCalledWith('instance-1', {
        name: 'Application reader',
        purpose: 'direct_access',
        capability: 'readonly',
        username: 'reporter',
        password: 'temporary-secret',
        database_name: null,
      }),
    )
    expect(screen.queryByDisplayValue('temporary-secret')).not.toBeInTheDocument()
    expect(localStorage).toHaveLength(0)
    expect(sessionStorage).toHaveLength(0)

    const revealButtons = screen.getAllByRole('button', {
      name: /reveal credential/i,
    })
    await user.click(revealButtons[revealButtons.length - 1])
    expect(revealCredential).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: /^confirm$/i }))
    expect(await screen.findByText('temporary-secret')).toBeInTheDocument()
    await user.click(
      screen.getByRole('button', { name: /close revealed credential/i }),
    )
    expect(screen.queryByText('temporary-secret')).not.toBeInTheDocument()
  })

  it('keeps credential connection results inline and offers edit', async () => {
    const user = userEvent.setup()
    vi.mocked(testInstanceCredentialConnection).mockResolvedValue({
      data: { ok: true },
    } as never)
    vi.mocked(updateCredential).mockResolvedValue({
      data: { ...credential, version: 2 },
    } as never)
    renderPage()

    await user.click(
      await screen.findByRole('button', { name: /add credential/i }),
    )
    await user.type(screen.getByLabelText(/^credential name$/i), 'Reader')
    await user.type(screen.getByLabelText(/^username$/i), 'reader')
    await user.type(screen.getByLabelText(/^password$/i), 'secret')
    await user.click(
      screen.getByRole('button', { name: /test connection/i }),
    )

    expect(
      await screen.findByText(/connection succeeded/i),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', {
        name: /edit provisioning administrator/i,
      }),
    ).toBeInTheDocument()
  })

  it('revokes a credential only after explicit confirmation', async () => {
    const user = userEvent.setup()
    vi.mocked(revokeCredential).mockResolvedValue({
      data: { ...credential, status: 'revoked' },
    } as never)
    renderPage()

    await user.click(
      await screen.findByRole('button', {
        name: /revoke provisioning administrator/i,
      }),
    )
    expect(revokeCredential).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: /confirm revoke/i }))
    await waitFor(() =>
      expect(revokeCredential).toHaveBeenCalledWith('credential-1'),
    )
  })

  it('switches provisioning credentials to the required admin capability', async () => {
    const user = userEvent.setup()
    vi.mocked(createInstanceCredential).mockResolvedValue({
      data: { ...credential, id: 'credential-2' },
    } as never)
    renderPage()

    await user.click(
      await screen.findByRole('button', { name: /add credential/i }),
    )
    await user.type(screen.getByLabelText(/^credential name$/i), 'DDL owner')
    await user.click(screen.getByLabelText(/scope to a database/i))
    await user.type(screen.getByLabelText(/database name/i), 'stale_database')
    await user.click(screen.getByLabelText(/^purpose$/i))
    const purposeOptions = screen.getAllByText('Provisioning administrator')
    await user.click(purposeOptions[purposeOptions.length - 1])
    expect(
      await screen.findByText(/^administrator$/i),
    ).toBeInTheDocument()
    expect(
      screen.queryByLabelText(/scope to a database/i),
    ).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/database name/i)).not.toBeInTheDocument()
    await user.type(screen.getByLabelText(/^username$/i), 'ddl_owner')
    await user.type(screen.getByLabelText(/^password$/i), 'secret-value')
    await user.click(screen.getByRole('button', { name: /save credential/i }))

    await waitFor(() =>
      expect(createInstanceCredential).toHaveBeenCalledWith('instance-1', {
        name: 'DDL owner',
        purpose: 'provisioning_admin',
        capability: 'admin',
        username: 'ddl_owner',
        password: 'secret-value',
        database_name: null,
      }),
    )
  })

  it('creates a backend with bounded capacity settings', async () => {
    const user = userEvent.setup()
    vi.mocked(listProvisioningBackends).mockResolvedValue({
      data: [],
    } as never)
    vi.mocked(createProvisioningBackend).mockResolvedValue({
      data: backend,
    } as never)
    renderPage()

    await user.click(
      await screen.findByRole('button', { name: /create backend/i }),
    )
    await user.click(
      screen.getByRole('combobox', { name: /provisioning credential/i }),
    )
    const credentialOptions = screen.getAllByText(
      'Provisioning administrator',
    )
    await user.click(credentialOptions[credentialOptions.length - 1])
    expect(screen.getByLabelText(/^priority$/i)).toHaveValue('0')
    expect(screen.getByLabelText(/maximum active resources/i)).toHaveValue(
      '20',
    )
    expect(screen.getByLabelText(/minimum cpu/i)).toHaveValue('1')
    expect(screen.getByLabelText(/maximum cpu/i)).toHaveValue('4')
    expect(screen.getByLabelText(/ddl concurrency/i)).toHaveValue('2')
    await user.click(screen.getByRole('button', { name: /save backend/i }))

    await waitFor(() =>
      expect(createProvisioningBackend).toHaveBeenCalledWith({
        instance_id: 'instance-1',
        admin_credential_id: credential.id,
        priority: 0,
        max_active_resources: 20,
        resource_min_cpu: 1,
        resource_max_cpu: 4,
        ddl_concurrency: 2,
      }),
    )
  })

  it('drains and disables a backend with status-specific confirmation', async () => {
    const user = userEvent.setup()
    vi.mocked(drainProvisioningBackend).mockResolvedValue({
      data: { ...backend, status: 'draining' },
    } as never)
    vi.mocked(disableProvisioningBackend).mockResolvedValue({
      data: { ...backend, status: 'disabled' },
    } as never)
    renderPage()

    await user.click(
      await screen.findByRole('button', { name: /drain backend/i }),
    )
    expect(drainProvisioningBackend).not.toHaveBeenCalled()
    expect(screen.getByText(/stops new resource placement/i)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /confirm drain/i }))
    await waitFor(() =>
      expect(drainProvisioningBackend).toHaveBeenCalledWith('backend-1'),
    )

    await user.click(screen.getByRole('button', { name: /disable backend/i }))
    expect(screen.getByText(/cleanup and recovery only/i)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /confirm disable/i }))
    await waitFor(() =>
      expect(disableProvisioningBackend).toHaveBeenCalledWith('backend-1'),
    )
  })
})
