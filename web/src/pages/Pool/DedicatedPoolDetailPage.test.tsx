import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  getDedicatedPool,
  getDedicatedPurchaseProfile,
  getDedicatedReadiness,
  updateDedicatedPool,
  upgradeDedicatedPurchaseProfile,
} from '../../api/dedicatedPools'
import DedicatedPoolDetailPage from './DedicatedPoolDetailPage'

vi.mock('../../api/dedicatedPools', async () => {
  const actual = await vi.importActual('../../api/dedicatedPools')
  return {
    ...actual,
    getDedicatedPool: vi.fn(),
    getDedicatedPurchaseProfile: vi.fn(),
    getDedicatedReadiness: vi.fn(),
    updateDedicatedPool: vi.fn(),
    upgradeDedicatedPurchaseProfile: vi.fn(),
  }
})

const pool = {
  id: 'pool-1',
  name: 'Production agents',
  status: 'active',
  supply_state: 'capacity_limited',
  supply_current: 1,
  supply_target: 3,
  blocking_reasons: ['POOL_CAPACITY_LIMIT_REACHED', 'ALIYUN_ACCESS_NOT_CONFIGURED'],
  target_size: 3,
  max_total_members: 3,
  max_member_purchases_per_hour: 2,
  max_create_requests_per_agent_per_hour: 10,
  max_delete_requests_per_agent_per_hour: 10,
  purchase_profile_id: 'agentic-dedicated-mysql',
  purchase_profile_revision: 1,
  purchase_profile_status: 'valid',
  storage_type: 'essdpl1',
  region_id: 'cn-hangzhou',
  zone_id: 'cn-hangzhou-k',
  vpc_id: 'vpc-1',
  vswitch_id: 'vsw-1',
  reclaim_policy: 'destroy',
  lifecycle_admin_policy: 'pas_managed',
  permission_template_revision_id: 'revision-1',
  delete_cooldown_duration_hours: 24,
  effective_delete_cooldown_duration_hours: 24,
  available_health_check_interval_seconds: 300,
  available_health_stale_after_seconds: 600,
  config_revision: 2,
  allocatable: 1,
  planning: 2,
  billable_total: 3,
  surplus: 0,
  route_usage: [],
  members: [
    {
      id: 'member-1',
      instance_id: 'instance-1',
      allocated_resource_id: null,
      status: 'replenishing',
      readiness_status: 'checking',
      preparation_step: 'purchase_requested',
      cloud_request_id: 'request-safe-1',
      failure_detail: 'Account pas_lifecycle_demo is not exist!',
      failure_occurred_at: '2026-08-11T14:19:19Z',
      failure_operation: 'CreateDatabase',
      last_ready_verified_at: null,
      readiness_evidence_age_seconds: null,
      delete_cooldown_duration_hours: 24,
      delete_cooldown_source: 'pool',
      failure_reason: 'DEDICATED_PREWARM_RUNTIMEERROR',
      actions: { retry: false, quarantine: true, destroy: true },
    },
  ],
}

const readiness = {
  worker: { configured: true, active_worker_count: 1, last_heartbeat_at: '2026-08-11T00:00:00Z' },
  aliyun_access: { configured: true, validated: true, credential_mode: 'direct_ak' },
  purchase_profile: {
    valid: true,
    profile_id: 'agentic-dedicated-mysql',
    revision: 1,
    default_storage_type: 'essdpl1',
    supported_storage_types: ['essdpl1'],
  },
  permission_template: { valid: true, default_revision_id: 'revision-1' },
  simulation_mode: false,
  preparation_mode: 'full' as const,
  blocking_reasons: [],
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/pool/dedicated/pool-1']}>
      <Routes>
        <Route path="/pool/dedicated/:poolId" element={<DedicatedPoolDetailPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('DedicatedPoolDetailPage', () => {
  beforeEach(() => {
    vi.mocked(getDedicatedPool).mockResolvedValue({ data: pool } as never)
    vi.mocked(getDedicatedReadiness).mockResolvedValue({ data: readiness } as never)
    vi.mocked(getDedicatedPurchaseProfile).mockResolvedValue({
      data: {
        profile_id: 'agentic-dedicated-mysql',
        revision: 1,
        default_storage_type: 'essdpl1',
        supported_storage_types: ['essdpl1'],
        fixed_parameters: { db_minor_version: '8.0.2', storage_space: '20' },
      },
    } as never)
    vi.mocked(upgradeDedicatedPurchaseProfile).mockResolvedValue({
      data: { ...pool, purchase_profile_status: 'valid', config_revision: 3 },
    } as never)
    vi.mocked(updateDedicatedPool).mockResolvedValue({
      data: { ...pool, target_size: 2, config_revision: 3 },
    } as never)
  })

  it('separates configuration from supply and explains every blocker', async () => {
    renderPage()

    expect(await screen.findByRole('heading', { name: 'Production agents' }))
      .toBeInTheDocument()
    expect(screen.getByText(/review this auto-provisioning pool's configured intent/i))
      .toBeInTheDocument()
    expect(screen.getByText(/configuration: active/i)).toBeInTheDocument()
    expect(screen.getByText(/supply: capacity limited/i)).toBeInTheDocument()
    expect(screen.getByText('1 / 3 ready')).toBeInTheDocument()
    expect(screen.getByText(/maximum billable member limit has been reached/i))
      .toBeInTheDocument()
    expect(screen.getByText(/Alibaba Cloud access is not configured/i))
      .toBeInTheDocument()
    expect(screen.queryByText(/simulation mode is enabled/i))
      .not.toBeInTheDocument()
    expect(screen.getByText(/no Agent can consume it yet/i)).toBeInTheDocument()
    expect(screen.getByText(/purchase requested/i)).toBeInTheDocument()
    expect(screen.getByText('request-safe-1')).toBeInTheDocument()
    expect(screen.getByText('DEDICATED_PREWARM_RUNTIMEERROR')).toBeInTheDocument()
  })

  it('shows the simulation warning only for effective simulation', async () => {
    vi.mocked(getDedicatedReadiness).mockResolvedValue({
      data: {
        ...readiness,
        aliyun_access: {
          configured: false,
          validated: false,
          credential_mode: 'direct_ak',
        },
        simulation_mode: true,
      },
    } as never)

    renderPage()

    expect(await screen.findByText(/simulation mode is enabled/i))
      .toBeInTheDocument()
  })

  it('shows cloud failure diagnostics when hovering the request ID', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.hover(await screen.findByText('request-safe-1'))

    expect(await screen.findByText(/CreateDatabase/)).toBeInTheDocument()
    expect(screen.getByText(/2026-08-11T14:19:19Z/)).toBeInTheDocument()
    expect(screen.getByText(/Account pas_lifecycle_demo is not exist!/))
      .toBeInTheDocument()
  })

  it('warns that OpenAPI-only members remain unavailable', async () => {
    vi.mocked(getDedicatedReadiness).mockResolvedValue({
      data: {
        ...readiness,
        simulation_mode: false,
        preparation_mode: 'openapi_only',
      },
    } as never)

    renderPage()

    expect(await screen.findByText(/OpenAPI-only preparation is enabled/i))
      .toBeInTheDocument()
    expect(screen.getByText(/real billable PolarDB resources/i))
      .toBeInTheDocument()
    expect(screen.getByRole('link', {
      name: /configure preparation mode/i,
    })).toHaveAttribute(
      'href',
      '/settings/configuration?module=runtime_policy&field=dedicated_pool_preparation_mode',
    )
  })

  it('guides a local full-mode pool paused before private data-plane access', async () => {
    vi.mocked(getDedicatedPool).mockResolvedValue({
      data: {
        ...pool,
        members: [{
          ...pool.members[0],
          preparation_step: 'openapi_ready',
        }],
      },
    } as never)
    vi.mocked(getDedicatedReadiness).mockResolvedValue({
      data: {
        ...readiness,
        simulation_mode: false,
        preparation_mode: 'full',
      },
    } as never)

    renderPage()

    expect(await screen.findByText(/private data-plane preparation is pending/i))
      .toBeInTheDocument()
    expect(screen.getByText(/OpenAPI only.*local development/i))
      .toBeInTheDocument()
    expect(screen.getByRole('link', {
      name: /configure preparation mode/i,
    })).toHaveAttribute(
      'href',
      '/settings/configuration?module=runtime_policy&field=dedicated_pool_preparation_mode',
    )
  })

  it('shows the typed upgrade diff and requires confirmation', async () => {
    const user = userEvent.setup()
    vi.mocked(getDedicatedPool).mockResolvedValue({
      data: {
        ...pool,
        purchase_profile_id: null,
        purchase_profile_revision: null,
        purchase_profile_status: 'upgrade_required',
        storage_type: null,
      },
    } as never)
    renderPage()

    await user.click(await screen.findByRole('button', { name: /review upgrade/i }))
    expect(screen.getByRole('dialog')).toHaveTextContent('DBMinorVersion')
    expect(screen.getByRole('dialog')).toHaveTextContent('8.0.2')
    expect(screen.getByRole('dialog')).toHaveTextContent('essdpl1')
    expect(upgradeDedicatedPurchaseProfile).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: /confirm upgrade/i }))
    await waitFor(() =>
      expect(upgradeDedicatedPurchaseProfile).toHaveBeenCalledWith('pool-1', 2),
    )
  })

  it('edits pool configuration with advanced network fields collapsed', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.click(
      await screen.findByRole('button', { name: /edit pool configuration/i }),
    )
    expect(screen.getByRole('button', { name: /advanced network configuration/i }))
      .toBeInTheDocument()
    expect(screen.queryByLabelText(/VPC ID/i)).not.toBeInTheDocument()

    const target = screen.getByLabelText(/prewarmed instance/i)
    await user.clear(target)
    await user.type(target, '2')
    await user.click(screen.getByRole('button', { name: /save changes/i }))

    await waitFor(() =>
      expect(updateDedicatedPool).toHaveBeenCalledWith('pool-1', expect.objectContaining({
        expected_config_revision: 2,
        target_size: 2,
        max_total_members: 3,
      })),
    )
  })

})
