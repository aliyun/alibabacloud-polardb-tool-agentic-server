import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { listDedicatedPools } from '../../api/dedicatedPools'
import {
  deleteProvisioningBinding,
  listProvisioningBindings,
  reorderDedicatedProvisioningBindings,
  updateProvisioningBinding,
} from '../../api/instanceAccess'
import DedicatedPoolRoutes from './DedicatedPoolRoutes'

vi.mock('../../api/dedicatedPools', async () => {
  const actual = await vi.importActual('../../api/dedicatedPools')
  return { ...actual, listDedicatedPools: vi.fn() }
})

vi.mock('../../api/instanceAccess', async () => {
  const actual = await vi.importActual('../../api/instanceAccess')
  return {
    ...actual,
    listProvisioningBindings: vi.fn(),
    reorderDedicatedProvisioningBindings: vi.fn(),
    updateProvisioningBinding: vi.fn(),
    deleteProvisioningBinding: vi.fn(),
  }
})

const pools = [
  {
    id: 'pool-primary', name: 'Primary pool', region_id: 'cn-hangzhou',
    supply_state: 'partially_ready', supply_current: 1, supply_target: 2,
  },
  {
    id: 'pool-fallback', name: 'Fallback pool', region_id: 'cn-beijing',
    supply_state: 'ready', supply_current: 2, supply_target: 2,
  },
]

const bindings = [
  {
    id: 'binding-primary', agent_id: 'agent-1', backend_id: 'backend-primary',
    enabled: true, routing_order: 0, backend_type: 'dedicated_pool',
    dedicated_pool_id: 'pool-primary', backend_status: 'active', allow_create: true,
    created_by_user_id: 'admin-1', created_at: '2026-08-11T00:00:00Z', updated_at: null,
  },
  {
    id: 'binding-fallback', agent_id: 'agent-1', backend_id: 'backend-fallback',
    enabled: true, routing_order: 1, backend_type: 'dedicated_pool',
    dedicated_pool_id: 'pool-fallback', backend_status: 'active', allow_create: true,
    created_by_user_id: 'admin-1', created_at: '2026-08-11T00:00:00Z', updated_at: null,
  },
]

describe('DedicatedPoolRoutes', () => {
  beforeEach(() => {
    vi.mocked(listDedicatedPools).mockResolvedValue({ data: pools } as never)
    vi.mocked(listProvisioningBindings).mockResolvedValue({ data: bindings } as never)
    vi.mocked(reorderDedicatedProvisioningBindings).mockResolvedValue({
      data: [bindings[1], bindings[0]],
    } as never)
    vi.mocked(updateProvisioningBinding).mockResolvedValue({ data: bindings[1] } as never)
    vi.mocked(deleteProvisioningBinding).mockResolvedValue({} as never)
  })

  it('shows primary and fallback capacity and sends a complete reorder', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter>
        <DedicatedPoolRoutes agentId="agent-1" resources={[]} backends={[]} />
      </MemoryRouter>,
    )

    expect(await screen.findByText('Primary pool')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /auto-provisioning pool routing/i }))
      .toBeInTheDocument()
    expect(screen.getByText('Primary')).toBeInTheDocument()
    expect(screen.getByText('Fallback 1')).toBeInTheDocument()
    expect(screen.getByText('1 / 2 ready')).toBeInTheDocument()
    expect(screen.getByText('2 / 2 ready')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /set Fallback pool as primary/i }))
    expect(screen.getByText(/future create requests only/i)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /confirm set primary/i }))
    await waitFor(() =>
      expect(reorderDedicatedProvisioningBindings).toHaveBeenCalledWith(
        'agent-1',
        ['binding-fallback', 'binding-primary'],
      ),
    )
  })

  it('blocks unlink while a nonterminal resource remains on the route', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter>
        <DedicatedPoolRoutes
          agentId="agent-1"
          resources={[
            {
              id: 'resource-1', backend_id: 'backend-primary', client_token: 'token-1',
              name: null, engine: 'polardb_mysql', status: 'ready',
              created_at: '2026-08-11T00:00:00Z', updated_at: null,
            },
          ]}
          backends={[]}
        />
      </MemoryRouter>,
    )

    await user.click(await screen.findByRole('button', { name: /unlink Primary pool/i }))
    expect(screen.getByRole('dialog')).toHaveTextContent(
      /cannot be unlinked while 1 nonterminal resource remains/i,
    )
    expect(deleteProvisioningBinding).not.toHaveBeenCalled()
  })
})
