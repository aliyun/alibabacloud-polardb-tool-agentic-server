import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  listDedicatedPools,
  runDedicatedMemberAction,
} from '../../api/dedicatedPools'
import DedicatedPoolPanel from './DedicatedPoolPanel'

vi.mock('../../api/dedicatedPools', async () => {
  const actual = await vi.importActual('../../api/dedicatedPools')
  return {
    ...actual,
    listDedicatedPools: vi.fn(),
    runDedicatedMemberAction: vi.fn(),
    createDedicatedPool: vi.fn(),
    drainDedicatedPool: vi.fn(),
  }
})

vi.mock('../../api/dbInstanceResources', () => ({
  listDBInstanceResources: vi.fn().mockResolvedValue({ data: [] }),
  restoreDBInstanceResource: vi.fn(),
}))

const basePool = {
  id: 'pool-1',
  name: 'Production agents',
  status: 'active' as const,
  target_size: 2,
  max_total_members: 5,
  max_member_purchases_per_hour: 2,
  max_create_requests_per_agent_per_hour: 10,
  max_delete_requests_per_agent_per_hour: 10,
  purchase_profile_id: 'agentic-dedicated-mysql',
  purchase_profile_revision: 1,
  purchase_profile_status: 'valid' as const,
  storage_type: 'essdpl1',
  region_id: 'cn-hangzhou',
  vpc_id: 'vpc-1',
  vswitch_id: 'vsw-1',
  zone_id: null,
  reclaim_policy: 'sanitize_and_reuse' as const,
  lifecycle_admin_policy: 'pas_managed' as const,
  permission_template_revision_id: 'revision-1',
  delete_cooldown_duration_hours: 24,
  effective_delete_cooldown_duration_hours: 24,
  available_health_check_interval_seconds: 300,
  available_health_stale_after_seconds: 600,
  config_revision: 1,
  allocatable: 2,
  planning: 2,
  billable_total: 2,
  surplus: 0,
  supply_state: 'ready' as const,
  supply_current: 2,
  supply_target: 2,
  blocking_reasons: [],
  members: [],
}

function renderPanel() {
  return render(
    <MemoryRouter>
      <DedicatedPoolPanel />
    </MemoryRouter>,
  )
}

describe('DedicatedPoolPanel', () => {
  beforeEach(() => {
    vi.mocked(listDedicatedPools).mockResolvedValue({
      data: [basePool],
    } as never)
    vi.mocked(runDedicatedMemberAction).mockResolvedValue({ data: {} } as never)
  })

  it('explains billable cooldown without showing a surplus warning normally', async () => {
    renderPanel()
    expect(await screen.findByText('Production agents')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /create auto-provisioning pool/i }))
      .toBeInTheDocument()
    expect(
      screen.getByText(/cooling members do not count toward target capacity/i),
    ).toBeInTheDocument()
    expect(screen.queryByText(/planning capacity exceeds target/i)).not.toBeInTheDocument()
  })

  it('shows the surplus explanation only after planning exceeds target', async () => {
    vi.mocked(listDedicatedPools).mockResolvedValue({
      data: [{ ...basePool, planning: 4, billable_total: 4, surplus: 2 }],
    } as never)
    renderPanel()
    expect(
      await screen.findByText(/planning capacity exceeds target by 2/i),
    ).toBeInTheDocument()
    expect(screen.getByText(/recent deletes or configuration changes/i)).toBeInTheDocument()
  })

  it('distinguishes stale evidence from a failed quarantined member', async () => {
    vi.mocked(listDedicatedPools).mockResolvedValue({
      data: [
        {
          ...basePool,
          members: [
            {
              id: 'member-replenishing',
              instance_id: 'instance-replenishing',
              allocated_resource_id: null,
              status: 'replenishing',
              readiness_status: 'stale',
              preparation_step: 'purchase_requested',
              last_ready_verified_at: null,
              readiness_evidence_age_seconds: null,
              delete_cooldown_duration_hours: 24,
              delete_cooldown_source: 'pool',
              failure_reason: 'DEDICATED_PREWARM_OPERATIONALERROR',
              actions: { retry: true, quarantine: true, destroy: true },
            },
            {
              id: 'member-stale',
              instance_id: 'instance-stale',
              allocated_resource_id: null,
              status: 'available',
              readiness_status: 'stale',
              preparation_step: 'verified',
              last_ready_verified_at: '2026-08-10T00:00:00Z',
              readiness_evidence_age_seconds: 700,
              delete_cooldown_duration_hours: 12,
              delete_cooldown_source: 'member',
              failure_reason: null,
              actions: { retry: false, quarantine: true, destroy: true },
            },
            {
              id: 'member-failed',
              instance_id: 'instance-failed',
              allocated_resource_id: null,
              status: 'quarantined',
              readiness_status: 'stale',
              preparation_step: 'verified',
              last_ready_verified_at: null,
              readiness_evidence_age_seconds: null,
              delete_cooldown_duration_hours: 24,
              delete_cooldown_source: 'pool',
              failure_reason: 'READINESS_CHECK_FAILED',
              actions: { retry: true, quarantine: false, destroy: true },
            },
          ],
        },
      ],
    } as never)
    const user = userEvent.setup()
    renderPanel()
    expect(
      await screen.findAllByText(/health evidence expired; allocation is paused pending recheck/i),
    ).not.toHaveLength(0)
    expect(
      screen.getByText(/health check failed; member is quarantined/i),
    ).toBeInTheDocument()
    expect(screen.getByText(/12 hours · member override/i)).toBeInTheDocument()

    await user.click(screen.getByRole('button', {
      name: /retry instance-replenishing/i,
    }))
    await waitFor(() =>
      expect(runDedicatedMemberAction).toHaveBeenCalledWith(
        'pool-1',
        'member-replenishing',
        'retry',
      ),
    )

    await user.click(screen.getByRole('button', { name: /retry instance-failed/i }))
    await waitFor(() =>
      expect(runDedicatedMemberAction).toHaveBeenCalledWith(
        'pool-1',
        'member-failed',
        'retry',
      ),
    )
  })
})
