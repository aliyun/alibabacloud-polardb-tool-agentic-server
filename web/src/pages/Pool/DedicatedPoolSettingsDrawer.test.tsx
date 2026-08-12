import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  updateDedicatedPool,
  type DedicatedPool,
  type DedicatedPurchaseProfile,
} from '../../api/dedicatedPools'
import DedicatedPoolSettingsDrawer from './DedicatedPoolSettingsDrawer'

vi.mock('../../api/dedicatedPools', async () => {
  const actual = await vi.importActual('../../api/dedicatedPools')
  return { ...actual, updateDedicatedPool: vi.fn() }
})

const pool: DedicatedPool = {
  id: 'pool-1',
  name: 'Production agents',
  status: 'active',
  target_size: 1,
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
  security_ip_list: null,
  vpc_id: 'vpc-1',
  vswitch_id: 'vsw-1',
  reclaim_policy: 'destroy',
  lifecycle_admin_policy: 'pas_managed',
  permission_template_revision_id: 'revision-1',
  delete_cooldown_duration_hours: 24,
  effective_delete_cooldown_duration_hours: 24,
  available_health_check_interval_seconds: 300,
  available_health_stale_after_seconds: 600,
  config_revision: 7,
  allocatable: 0,
  planning: 1,
  billable_total: 1,
  surplus: 0,
  supply_state: 'prewarming',
  supply_current: 0,
  supply_target: 1,
  blocking_reasons: [],
  route_usage: [],
  members: [],
}

const profile: DedicatedPurchaseProfile = {
  profile_id: 'agentic-dedicated-mysql',
  revision: 1,
  default_storage_type: 'essdpl1',
  supported_storage_types: ['essdpl1'],
  fixed_parameters: {},
}

describe('DedicatedPoolSettingsDrawer', () => {
  beforeEach(() => {
    vi.mocked(updateDedicatedPool).mockResolvedValue({ data: pool } as never)
  })

  it('requires confirmation only for a normalized network change', async () => {
    const user = userEvent.setup()
    render(
      <DedicatedPoolSettingsDrawer
        open
        pool={pool}
        profile={profile}
        onClose={vi.fn()}
        onUpdated={vi.fn()}
      />,
    )

    expect(screen.queryByLabelText('VPC ID')).not.toBeInTheDocument()
    await user.click(
      screen.getByRole('button', { name: /advanced network configuration/i }),
    )
    const vpc = screen.getByLabelText('VPC ID')

    await user.clear(vpc)
    await user.type(vpc, ' vpc-1 ')
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()

    await user.clear(vpc)
    await user.type(vpc, ' vpc-2 ')
    expect(screen.getByText(/does not migrate existing polarDB clusters/i))
      .toBeInTheDocument()
    const confirmation = screen.getByRole('checkbox', {
      name: /confirmed connectivity and availability/i,
    })

    await user.click(screen.getByRole('button', { name: /save changes/i }))
    expect(updateDedicatedPool).not.toHaveBeenCalled()

    await user.click(confirmation)
    await user.click(screen.getByRole('button', { name: /save changes/i }))

    await waitFor(() =>
      expect(updateDedicatedPool).toHaveBeenCalledWith(
        'pool-1',
        expect.objectContaining({
          expected_config_revision: 7,
          network_change_confirmed: true,
          region_id: 'cn-hangzhou',
          zone_id: 'cn-hangzhou-k',
          vpc_id: 'vpc-2',
          vswitch_id: 'vsw-1',
          security_ip_list: null,
        }),
      ),
    )
  })
})
