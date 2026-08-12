import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {
  MemoryRouter,
  Route,
  Routes,
  useNavigate,
} from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  getAgent,
  listAgentPolarRAGBindings,
  regenerateAgentToken,
  revealAgentToken,
  revokeAgentToken,
  updateAgent,
} from '../../api/agents'
import { executeConfig } from '../../api/configuration'
import { listInstanceCredentials } from '../../api/credentials'
import { listDedicatedPools } from '../../api/dedicatedPools'
import {
  createAgentInstanceAccess,
  deleteAgentInstanceAccess,
  listAgentResources,
  listAgentInstanceAccess,
  listInstances,
  listProvisioningBindings,
  updateAgentInstanceAccess,
} from '../../api/instanceAccess'
import { listProvisioningBackends } from '../../api/provisioningBackends'
import AgentDetail from './index'

vi.mock('../../api/agents', () => ({
  getAgent: vi.fn(),
  listAgentPolarRAGBindings: vi.fn(),
  regenerateAgentToken: vi.fn(),
  revealAgentToken: vi.fn(),
  revokeAgentToken: vi.fn(),
  updateAgent: vi.fn(),
}))

vi.mock('../../api/credentials', () => ({
  listInstanceCredentials: vi.fn(),
}))

vi.mock('../../api/dedicatedPools', () => ({
  listDedicatedPools: vi.fn(),
}))

vi.mock('../../api/configuration', () => ({
  executeConfig: vi.fn(),
}))

vi.mock('../../api/instanceAccess', () => ({
  createAgentInstanceAccess: vi.fn(),
  createProvisioningBinding: vi.fn(),
  deleteAgentInstanceAccess: vi.fn(),
  deleteProvisioningBinding: vi.fn(),
  listAgentResources: vi.fn(),
  listAgentInstanceAccess: vi.fn(),
  listInstances: vi.fn(),
  listProvisioningBindings: vi.fn(),
  reorderDedicatedProvisioningBindings: vi.fn(),
  updateAgentInstanceAccess: vi.fn(),
  updateProvisioningBinding: vi.fn(),
}))

vi.mock('../../api/provisioningBackends', () => ({
  listProvisioningBackends: vi.fn(),
}))

vi.mock('./PolarRAGAccessPanel', () => ({
  default: () => <div>PolarRAG access panel</div>,
}))

const agent = {
  id: 'agent-1',
  name: 'production-reader',
  description: 'Reads production inventory',
  status: 'active' as const,
  max_active_resources: 4,
  created_by: 'admin-1',
  created_at: '2026-07-26T00:00:00Z',
  updated_at: null,
  token_summary: {
    id: 'token-1',
    token_prefix: 'pas_agent_active',
    status: 'active' as const,
    expires_at: null,
    revoked_at: null,
    last_used_at: null,
    created_at: '2026-07-26T00:00:00Z',
    updated_at: null,
  },
}

const instance = {
  id: 'instance-1',
  cluster_id: 'pc-production',
  name: 'Production',
  engine: 'polardb_mysql' as const,
  topology: 'single_tenant' as const,
  allocation_mode: 'registered' as const,
  region: 'cn-hangzhou',
  host: null,
  port: null,
  status: 'active',
  owner_user_id: null,
  health: null,
  binding_counts: { users: 0, departments: 0, agents: 0 },
}

const credential = {
  id: 'credential-1',
  instance_id: instance.id,
  resource_id: null,
  name: 'Reader',
  purpose: 'direct_access' as const,
  capability: 'readonly' as const,
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
  admin_credential_id: 'admin-credential-1',
  status: 'active' as const,
  priority: 10,
  max_active_resources: 20,
  resource_min_cpu: 1,
  resource_max_cpu: 4,
  ddl_concurrency: 2,
  config_revision: 1,
  healthy: true,
  health_checked_at: '2026-07-27T00:00:00Z',
  available_for_create: true,
  created_at: '2026-07-26T00:00:00Z',
  updated_at: null,
}

const instanceAccess = {
  agent_id: agent.id,
  instance_id: instance.id,
  credential_id: credential.id,
  permission: 'readonly' as const,
  direct_enabled: true,
  capabilities: ['db_instance:list' as const, 'sql:read' as const],
  direct_binding_id: 'direct-1',
  provisioning_binding_id: null,
  provisioning_backend_id: null,
  create_availability: 'instance_ineligible' as const,
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function NavigationControls() {
  const navigate = useNavigate()
  return (
    <>
      <button type="button" onClick={() => navigate('/agents/agent-1')}>
        Agent A
      </button>
      <button type="button" onClick={() => navigate('/agents/agent-2')}>
        Agent B
      </button>
    </>
  )
}

function renderPage(withNavigation = false) {
  return render(
    <MemoryRouter initialEntries={['/agents/agent-1']}>
      {withNavigation && <NavigationControls />}
      <Routes>
        <Route path="/agents/:id" element={<AgentDetail />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('Agent detail page', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(getAgent).mockResolvedValue({ data: agent } as never)
    vi.mocked(listAgentPolarRAGBindings).mockResolvedValue({ data: [] } as never)
    vi.mocked(listInstances).mockResolvedValue({
      items: [instance], total: 1, offset: 0, limit: 200,
    } as never)
    vi.mocked(listInstanceCredentials).mockResolvedValue({
      data: [credential],
    } as never)
    vi.mocked(listAgentInstanceAccess).mockResolvedValue({
      data: [],
    } as never)
    vi.mocked(listProvisioningBackends).mockResolvedValue({
      data: [],
    } as never)
    vi.mocked(listAgentResources).mockResolvedValue({ data: [] } as never)
    vi.mocked(listDedicatedPools).mockResolvedValue({ data: [] } as never)
    vi.mocked(listProvisioningBindings).mockResolvedValue({ data: [] } as never)
    vi.mocked(updateAgent).mockResolvedValue({ data: agent } as never)
    vi.mocked(revealAgentToken).mockResolvedValue({
      data: { token: 'pas_agent_default_plaintext' },
    } as never)
    vi.mocked(revokeAgentToken).mockResolvedValue({ data: {} } as never)
    vi.mocked(executeConfig).mockResolvedValue({
      config_version: 1,
      system_state: 'READY',
      module: {
        name: 'runtime_policy',
        revision: 1,
        workflow_state: 'ACTIVE',
        effective: {
          revision: 1,
          state: 'ACTIVE',
          config: {},
        },
        schema: {},
        dependencies: [],
        dependents: [],
      },
    })
  })

  it('regenerates a token only after destructive confirmation without exposing plaintext', async () => {
    const user = userEvent.setup()
    vi.mocked(regenerateAgentToken).mockResolvedValue({
      data: { token: null, token_prefix: 'pas_agent_new' },
    } as never)
    renderPage()

    expect(
      await screen.findByText('pas_agent_••••••••'),
    ).toBeInTheDocument()
    expect(revealAgentToken).not.toHaveBeenCalled()
    await user.click(
      screen.getByRole('button', { name: /regenerate token/i }),
    )
    expect(regenerateAgentToken).not.toHaveBeenCalled()
    expect(
      screen.getByText(/old token becomes invalid immediately/i),
    ).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /^confirm regenerate$/i }))
    expect(await screen.findByText('pas_agent_••••••••')).toBeInTheDocument()
    expect(await screen.findByRole('status')).toHaveTextContent(
      /reconnect the MCP client/i,
    )
    expect(screen.queryByText('pas_agent_regenerated_plaintext')).not.toBeInTheDocument()
    expect(
      screen.queryByRole('dialog', { name: /new agent token/i }),
    ).not.toBeInTheDocument()
    expect(localStorage).toHaveLength(0)
    expect(sessionStorage).toHaveLength(0)
  })

  it('uses the configured external base URL for MCP and REST API access', async () => {
    vi.mocked(revealAgentToken).mockResolvedValue({
      data: { token: 'pas_agent_revealed_plaintext' },
    } as never)
    vi.mocked(executeConfig).mockResolvedValue({
      config_version: 1,
      system_state: 'READY',
      module: {
        name: 'runtime_policy',
        revision: 5,
        workflow_state: 'ACTIVE',
        effective: {
          revision: 5,
          state: 'ACTIVE',
          config: {
            external_base_url: 'http://10.0.0.8:18760/',
          },
        },
        schema: {},
        dependencies: [],
        dependents: [],
      },
    })
    renderPage()

    expect(await screen.findByText('pas_agent_••••••••')).toBeInTheDocument()
    expect(revealAgentToken).not.toHaveBeenCalled()
    expect(
      await screen.findByText('http://10.0.0.8:18760/mcp'),
    ).toBeInTheDocument()
    expect(
      screen.getByText('http://10.0.0.8:18760/mcp/rest/db-instances'),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: /open live api docs/i }),
    ).toHaveAttribute('href', 'http://10.0.0.8:18760/mcp/rest/docs')
    expect(
      screen.getByRole('link', { name: /open openapi schema/i }),
    ).toHaveAttribute(
      'href',
      'http://10.0.0.8:18760/mcp/rest/openapi.json',
    )
    expect(
      screen.getByRole('link', { name: /view github guide/i }),
    ).toHaveAttribute(
      'href',
      'https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server/blob/main/docs/en/database-instances/agent-rest-provisioning.md',
    )
    expect(
      screen.getAllByText(/Authorization: Bearer <agent-token>/),
    ).toHaveLength(2)
    expect(
      screen.queryByRole('button', { name: /reveal credential/i }),
    ).not.toBeInTheDocument()
  })

  it('falls back to the browser origin when runtime policy cannot be loaded', async () => {
    vi.mocked(executeConfig).mockRejectedValue(new Error('unavailable'))
    renderPage()

    expect(
      await screen.findByText(`${window.location.origin}/mcp`),
    ).toBeInTheDocument()
  })

  it('does not reveal a Token while loading the page', async () => {
    vi.mocked(revealAgentToken).mockRejectedValue(new Error('unavailable'))
    renderPage()

    expect(await screen.findByText('pas_agent_••••••••')).toBeInTheDocument()
    expect(revealAgentToken).not.toHaveBeenCalled()
  })

  it('uses the real Token state independently from disabled Agent status', async () => {
    vi.mocked(getAgent).mockResolvedValue({
      data: { ...agent, status: 'disabled' },
    } as never)
    renderPage()

    expect(
      await screen.findByText('pas_agent_••••••••'),
    ).toBeInTheDocument()
    expect(screen.getByText(/^Active$/)).toBeInTheDocument()
  })

  it.each(['revoked', 'expired'] as const)(
    'shows a %s Token without reveal or revoke actions',
    async (status) => {
      vi.mocked(getAgent).mockResolvedValue({
        data: {
          ...agent,
          token_summary: {
            ...agent.token_summary,
            status,
            revoked_at:
              status === 'revoked' ? '2026-07-26T01:00:00Z' : null,
            expires_at:
              status === 'expired' ? '2026-07-25T23:00:00Z' : null,
          },
        },
      } as never)
      renderPage()

      expect(await screen.findByText(new RegExp(`^${status}$`, 'i'))).toBeInTheDocument()
      expect(
        screen.getByRole('button', { name: /copy token/i }),
      ).toBeDisabled()
      expect(
        screen.queryByRole('button', { name: /revoke token/i }),
      ).not.toBeInTheDocument()
      expect(
        screen.getByRole('button', { name: /regenerate token/i }),
      ).toBeEnabled()
    },
  )

  it('shows a missing Token as regeneratable', async () => {
    vi.mocked(getAgent).mockResolvedValue({
      data: { ...agent, token_summary: null },
    } as never)
    renderPage()

    expect(await screen.findByText(/^Missing$/)).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /regenerate token/i }),
    ).toBeEnabled()
    expect(
      screen.getByRole('button', { name: /copy token/i }),
    ).toBeDisabled()
  })

  it('does not let a late Agent A load overwrite route B', async () => {
    const user = userEvent.setup()
    const pendingA = deferred<{ data: typeof agent }>()
    const agentB = {
      ...agent,
      id: 'agent-2',
      name: 'billing-agent',
      token_summary: {
        ...agent.token_summary,
        id: 'token-2',
        token_prefix: 'pas_agent_billing',
      },
    }
    vi.mocked(getAgent).mockImplementation((agentId) =>
      agentId === 'agent-1'
        ? pendingA.promise as never
        : Promise.resolve({ data: agentB }) as never,
    )
    renderPage(true)

    await user.click(screen.getByRole('button', { name: /agent b/i }))
    expect(
      await screen.findByRole('heading', {
        name: 'billing-agent',
        level: 3,
      }),
    ).toBeInTheDocument()
    pendingA.resolve({ data: agent })

    await waitFor(() =>
      expect(
        screen.queryByRole('heading', {
          name: 'production-reader',
          level: 3,
        }),
      ).not.toBeInTheDocument(),
    )
    expect(
      screen.getByRole('heading', { name: 'billing-agent', level: 3 }),
    ).toBeInTheDocument()
  })

  it('clears route A Token summary immediately while route B is still loading', async () => {
    const user = userEvent.setup()
    const pendingB = deferred<{ data: typeof agent }>()
    vi.mocked(getAgent).mockImplementation((agentId) =>
      agentId === 'agent-1'
        ? Promise.resolve({ data: agent }) as never
        : pendingB.promise as never,
    )
    renderPage(true)

    expect(await screen.findByText('pas_agent_••••••••')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /agent b/i }))

    await waitFor(() =>
      expect(screen.queryByText('pas_agent_••••••••')).not.toBeInTheDocument(),
    )
    expect(screen.getByText(/loading Agent access settings/i)).toBeInTheDocument()
  })

  it('does not reveal either Agent Token while navigating between routes', async () => {
    const user = userEvent.setup()
    const agentB = {
      ...agent,
      id: 'agent-2',
      name: 'billing-agent',
      token_summary: {
        ...agent.token_summary,
        id: 'token-2',
        token_prefix: 'pas_agent_billing',
      },
    }
    vi.mocked(getAgent).mockImplementation((agentId) =>
      Promise.resolve({ data: agentId === 'agent-1' ? agent : agentB }) as never,
    )
    renderPage(true)

    expect(await screen.findByText('pas_agent_••••••••')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /agent b/i }))
    expect(
      await screen.findByRole('heading', {
        name: 'billing-agent',
        level: 3,
      }),
    ).toBeInTheDocument()

    expect(screen.getByText('pas_agent_••••••••')).toBeInTheDocument()
    expect(revealAgentToken).not.toHaveBeenCalled()
  })

  it('ignores a pending route A regeneration after navigating to B', async () => {
    const user = userEvent.setup()
    const pendingRegeneration = deferred<{
      data: { token: string }
    }>()
    const agentB = {
      ...agent,
      id: 'agent-2',
      name: 'billing-agent',
      token_summary: {
        ...agent.token_summary,
        id: 'token-2',
        token_prefix: 'pas_agent_billing',
      },
    }
    vi.mocked(getAgent).mockImplementation((agentId) =>
      Promise.resolve({ data: agentId === 'agent-1' ? agent : agentB }) as never,
    )
    vi.mocked(regenerateAgentToken).mockReturnValue(
      pendingRegeneration.promise as never,
    )
    renderPage(true)

    await user.click(
      await screen.findByRole('button', { name: /regenerate token/i }),
    )
    await user.click(screen.getByRole('button', { name: /^confirm regenerate$/i }))
    await user.click(screen.getByRole('button', { name: /agent b/i }))
    expect(
      await screen.findByRole('heading', {
        name: 'billing-agent',
        level: 3,
      }),
    ).toBeInTheDocument()

    pendingRegeneration.resolve({
      data: { token: 'pas_agent_stale_regeneration' },
    })

    await waitFor(() =>
      expect(
        screen.queryByText('pas_agent_stale_regeneration'),
      ).not.toBeInTheDocument(),
    )
    expect(
      screen.getByRole('heading', { name: 'billing-agent', level: 3 }),
    ).toBeInTheDocument()
  })

  it('ignores route A mutation rejection and finally state after navigating to B', async () => {
    const user = userEvent.setup()
    const pendingStatus = deferred<{ data: typeof agent }>()
    const agentB = {
      ...agent,
      id: 'agent-2',
      name: 'billing-agent',
      token_summary: {
        ...agent.token_summary,
        id: 'token-2',
        token_prefix: 'pas_agent_billing',
      },
    }
    vi.mocked(getAgent).mockImplementation((agentId) =>
      Promise.resolve({ data: agentId === 'agent-1' ? agent : agentB }) as never,
    )
    vi.mocked(updateAgent).mockReturnValue(pendingStatus.promise as never)
    renderPage(true)

    await user.click(
      await screen.findByRole('button', { name: /disable agent/i }),
    )
    await user.click(screen.getByRole('button', { name: /^confirm disable$/i }))
    await user.click(screen.getByRole('button', { name: /agent b/i }))
    expect(
      await screen.findByRole('heading', {
        name: 'billing-agent',
        level: 3,
      }),
    ).toBeInTheDocument()

    pendingStatus.reject(new Error('late route A failure'))
    await waitFor(() =>
      expect(
        screen.queryByText(/requested change could not be saved/i),
      ).not.toBeInTheDocument(),
    )
    expect(
      screen.getByRole('button', { name: /disable agent/i }),
    ).toBeEnabled()
  })

  it('keeps the Token masked when it is regenerated', async () => {
    const user = userEvent.setup()
    vi.mocked(regenerateAgentToken).mockResolvedValue({
      data: { token: null, token_prefix: 'pas_agent_new' },
    } as never)
    renderPage()

    expect(await screen.findByText('pas_agent_••••••••')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /regenerate token/i }))
    await user.click(screen.getByRole('button', { name: /^confirm regenerate$/i }))

    expect(await screen.findByText('pas_agent_••••••••')).toBeInTheDocument()
    expect(screen.queryByText('pas_agent_new_plaintext')).not.toBeInTheDocument()
  })

  it('keeps the independently active Token after an Agent status change', async () => {
    const user = userEvent.setup()
    vi.mocked(updateAgent).mockResolvedValue({
      data: { ...agent, status: 'disabled' },
    } as never)
    renderPage()

    expect(
      await screen.findByText('pas_agent_••••••••'),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /disable agent/i }))
    await user.click(screen.getByRole('button', { name: /^confirm disable$/i }))
    expect(
      await screen.findByText('pas_agent_••••••••'),
    ).toBeInTheDocument()
  })

  it('preserves the current Token when regeneration fails', async () => {
    const user = userEvent.setup()
    vi.mocked(regenerateAgentToken).mockRejectedValue(
      new Error('regeneration failed'),
    )
    renderPage()

    expect(await screen.findByText('pas_agent_••••••••')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /regenerate token/i }))
    await user.click(screen.getByRole('button', { name: /^confirm regenerate$/i }))

    expect(await screen.findByText('pas_agent_••••••••')).toBeInTheDocument()
    expect(
      await screen.findByText(/requested change could not be saved/i),
    ).toBeInTheDocument()
  })

  it('clears masked Token actions after revocation', async () => {
    const user = userEvent.setup()
    vi.mocked(revokeAgentToken).mockResolvedValue({
      data: {
        ...agent.token_summary,
        agent_id: agent.id,
        revoked_at: '2026-07-26T02:00:00Z',
        token: null,
      },
    } as never)
    renderPage()

    expect(await screen.findByText('pas_agent_••••••••')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /revoke token/i }))
    await user.click(screen.getByRole('button', { name: /^confirm revoke$/i }))

    expect(await screen.findByText('pas_agent_••••••••')).toBeInTheDocument()
    expect(screen.getByText(/^Revoked$/)).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /copy token/i }),
    ).toBeDisabled()
    expect(
      screen.queryByRole('button', { name: /revoke token/i }),
    ).not.toBeInTheDocument()
  })

  it('shows one instance access section and no separate provisioning section', async () => {
    renderPage()

    expect(
      await screen.findByRole('heading', { name: /identity & status/i }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { name: /^instance access$/i }),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('heading', { name: /provisioning backends/i }),
    ).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /^resources$/i })).toBeInTheDocument()
    expect(listInstanceCredentials).not.toHaveBeenCalled()
  })

  it('shows database and PolarRAG bindings with their instance types', async () => {
    vi.mocked(listAgentInstanceAccess).mockResolvedValue({
      data: [instanceAccess],
    } as never)
    vi.mocked(listAgentPolarRAGBindings).mockResolvedValue({
      data: [
        {
          id: 'rag-binding-1',
          polarrag_instance_id: 'rag-1',
          instance_name: 'Primary RAG',
          created_at: '2026-08-05T00:00:00Z',
        },
      ],
    } as never)
    renderPage()

    const heading = await screen.findByRole('heading', {
      name: /^instance access$/i,
    })
    const section = heading.closest('section')
    expect(section).not.toBeNull()
    const accessTable = within(section as HTMLElement)

    expect(accessTable.getByRole('columnheader', {
      name: /instance type/i,
    })).toBeInTheDocument()
    expect(accessTable.getByText('Production')).toBeInTheDocument()
    expect(accessTable.getByText('PolarDB for MySQL')).toBeInTheDocument()
    expect(accessTable.getByText('Primary RAG')).toBeInTheDocument()
    expect(accessTable.getByText('PolarRAG')).toBeInTheDocument()
  })

  it('prevents saving new access before an instance and capability are selected', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.click(
      await screen.findByRole('button', { name: /add instance access/i }),
    )

    expect(
      screen.getByRole('button', { name: /save instance access/i }),
    ).toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent(
      /select a database instance/i,
    )
  })

  it('excludes instances that already have aggregate access', async () => {
    const user = userEvent.setup()
    const secondInstance = {
      ...instance,
      id: 'instance-2',
      cluster_id: 'pc-secondary',
      name: 'Secondary',
    }
    vi.mocked(listInstances).mockResolvedValue({
      items: [instance, secondInstance],
      total: 2,
      offset: 0,
      limit: 200,
    } as never)
    vi.mocked(listAgentInstanceAccess).mockResolvedValue({
      data: [instanceAccess],
    } as never)
    renderPage()

    await user.click(
      await screen.findByRole('button', { name: /add instance access/i }),
    )
    await user.click(
      screen.getByRole('combobox', { name: /database instance/i }),
    )
    const listbox = await screen.findByRole('listbox')
    expect(
      within(listbox).queryByRole('option', { name: /Production/i }),
    ).not.toBeInTheDocument()
    expect(
      within(listbox).getByRole('option', { name: /Secondary/i }),
    ).toBeInTheDocument()
  })

  it('creates provisioning-only access through the aggregate API', async () => {
    const user = userEvent.setup()
    const multitenant = {
      ...instance,
      topology: 'multitenant' as const,
    }
    const created = {
      ...instanceAccess,
      credential_id: null,
      permission: null,
      direct_enabled: null,
      capabilities: ['db_instance:create' as const],
      direct_binding_id: null,
      provisioning_binding_id: 'provisioning-1',
      provisioning_backend_id: backend.id,
      create_availability: 'available' as const,
    }
    vi.mocked(listInstances).mockResolvedValue({
      items: [multitenant], total: 1, offset: 0, limit: 200,
    } as never)
    vi.mocked(listProvisioningBackends).mockResolvedValue({
      data: [backend],
    } as never)
    vi.mocked(createAgentInstanceAccess).mockResolvedValue({
      data: created,
    } as never)
    renderPage()

    await user.click(
      await screen.findByRole('button', { name: /add instance access/i }),
    )
    await user.click(
      screen.getByRole('combobox', { name: /database instance/i }),
    )
    await user.click(await screen.findByText(/Production.*pc-production/i))
    await user.click(
      screen.getByRole('checkbox', {
        name: /create managed databases/i,
      }),
    )
    await user.click(
      screen.getByRole('button', { name: /save instance access/i }),
    )

    expect(createAgentInstanceAccess).toHaveBeenCalledWith('agent-1', {
      instance_id: 'instance-1',
      credential_id: null,
      permission: null,
      direct_enabled: null,
      capabilities: ['db_instance:create'],
    })
  })

  it('updates aggregate access by instance id', async () => {
    const user = userEvent.setup()
    vi.mocked(listAgentInstanceAccess).mockResolvedValue({
      data: [instanceAccess],
    } as never)
    vi.mocked(updateAgentInstanceAccess).mockResolvedValue({
      data: {
        ...instanceAccess,
        capabilities: ['db_instance:list'],
      },
    } as never)
    renderPage()

    await user.click(await screen.findByRole('button', { name: /^edit$/i }))
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: /save instance access/i }),
      ).toBeEnabled(),
    )
    await user.click(
      screen.getByRole('checkbox', {
        name: /enable sql over http proxy/i,
      }),
    )
    await user.click(
      screen.getByRole('button', { name: /save instance access/i }),
    )

    expect(updateAgentInstanceAccess).toHaveBeenCalledWith(
      'agent-1',
      'instance-1',
      {
        credential_id: 'credential-1',
        permission: 'readonly',
        direct_enabled: true,
        capabilities: ['db_instance:list'],
      },
    )
  })

  it('keeps a blocked aggregate delete actionable by disabling new creation', async () => {
    const user = userEvent.setup()
    const combined = {
      ...instanceAccess,
      capabilities: [
        'db_instance:list' as const,
        'sql:read' as const,
        'db_instance:create' as const,
      ],
      provisioning_binding_id: 'provisioning-1',
      provisioning_backend_id: backend.id,
      create_availability: 'available' as const,
    }
    vi.mocked(listAgentInstanceAccess).mockResolvedValue({
      data: [combined],
    } as never)
    vi.mocked(listAgentResources).mockResolvedValue({
      data: [
        {
          id: 'resource-1',
          backend_id: backend.id,
          client_token: 'resource-token',
          name: 'orders',
          engine: 'polardb_mysql',
          status: 'ready',
          created_at: '2026-07-26T00:00:00Z',
          updated_at: null,
        },
      ],
    } as never)
    vi.mocked(deleteAgentInstanceAccess).mockRejectedValue({
      response: {
        status: 409,
        data: {
          detail: {
            code: 'BINDING_HAS_RESOURCES',
            message: 'Agent instance access has non-deleted resources',
          },
        },
      },
    })
    vi.mocked(updateAgentInstanceAccess).mockResolvedValue({
      data: instanceAccess,
    } as never)
    renderPage()

    await user.click(await screen.findByRole('button', { name: /^remove$/i }))
    await user.click(screen.getByRole('button', { name: /confirm remove/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      /contains 1 non-deleted resource/i,
    )

    await user.click(
      screen.getByRole('button', {
        name: /disable managed database creation/i,
      }),
    )
    expect(updateAgentInstanceAccess).toHaveBeenCalledWith(
      'agent-1',
      'instance-1',
      {
        credential_id: 'credential-1',
        permission: 'readonly',
        direct_enabled: true,
        capabilities: ['db_instance:list', 'sql:read'],
      },
    )
  })

  it('omits terminal resources even if a stale API response contains them', async () => {
    vi.mocked(listAgentResources).mockResolvedValue({
      data: [
        {
          id: 'resource-ready',
          backend_id: 'backend-1',
          client_token: 'ready-token',
          name: 'orders',
          engine: 'polardb_mysql',
          status: 'ready',
          created_at: '2026-07-26T00:00:00Z',
          updated_at: null,
        },
        {
          id: 'resource-deleted',
          backend_id: 'backend-1',
          client_token: 'deleted-token',
          name: 'removed',
          engine: 'polardb_mysql',
          status: 'deleted',
          created_at: '2026-07-26T00:00:00Z',
          updated_at: null,
        },
      ],
    } as never)
    renderPage()

    expect(await screen.findByText('orders')).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.queryByText('removed')).not.toBeInTheDocument(),
    )
  })
})
