import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createDedicatedPool,
  getDedicatedPurchaseProfile,
  getDedicatedReadiness,
} from '../../api/dedicatedPools'
import { listPermissionTemplates } from '../../api/permissionTemplates'
import DedicatedPoolCreatePage from './DedicatedPoolCreatePage'

vi.mock('../../api/dedicatedPools', async () => {
  const actual = await vi.importActual('../../api/dedicatedPools')
  return {
    ...actual,
    createDedicatedPool: vi.fn(),
    getDedicatedPurchaseProfile: vi.fn(),
    getDedicatedReadiness: vi.fn(),
  }
})

vi.mock('../../api/permissionTemplates', async () => {
  const actual = await vi.importActual('../../api/permissionTemplates')
  return { ...actual, listPermissionTemplates: vi.fn() }
})

const profile = {
  profile_id: 'agentic-dedicated-mysql',
  revision: 1,
  default_storage_type: 'essdpl1',
  supported_storage_types: ['essdpl1'],
  fixed_parameters: {
    db_type: 'MySQL',
    db_version: '8.0',
    db_minor_version: '8.0.2',
    serverless_type: 'AgileServerless',
  },
}

const blockedReadiness = {
  worker: {
    configured: true,
    active_worker_count: 0,
    last_heartbeat_at: null,
  },
  aliyun_access: {
    configured: false,
    validated: false,
    credential_mode: 'direct_ak' as const,
  },
  purchase_profile: {
    valid: true,
    profile_id: profile.profile_id,
    revision: profile.revision,
    default_storage_type: profile.default_storage_type,
    supported_storage_types: profile.supported_storage_types,
  },
  permission_template: {
    valid: true,
    default_revision_id: 'builtin-mysql-default-v1',
  },
  simulation_mode: false,
  preparation_mode: 'full' as const,
  blocking_reasons: [
    'DEDICATED_WORKER_NOT_RUNNING',
    'ALIYUN_ACCESS_NOT_CONFIGURED',
  ],
}

describe('DedicatedPoolCreatePage', () => {
  beforeEach(() => {
    vi.mocked(getDedicatedPurchaseProfile).mockResolvedValue({ data: profile } as never)
    vi.mocked(getDedicatedReadiness).mockResolvedValue({ data: blockedReadiness } as never)
    vi.mocked(listPermissionTemplates).mockResolvedValue({
      data: [
        {
          id: 'builtin-mysql-default',
          name: 'Default MySQL sandbox permissions',
          description: null,
          created_at: '2026-08-10T00:00:00Z',
          revisions: [
            {
              id: 'builtin-mysql-default-v1',
              revision: 1,
              privileges: ['SELECT', 'INSERT'],
              grant_option: false,
              created_at: '2026-08-10T00:00:00Z',
            },
          ],
        },
      ],
    } as never)
    vi.mocked(createDedicatedPool).mockResolvedValue({
      data: { id: 'pool-created' },
    } as never)
  })

  it('guides a blocked environment to a typed save-as-not-started request', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter>
        <DedicatedPoolCreatePage />
      </MemoryRouter>,
    )

    expect(await screen.findByRole('heading', { name: /create auto-provisioning pool/i }))
      .toBeInTheDocument()
    expect(screen.getByText(/AgenticDB Dedicated/i)).toBeInTheDocument()
    expect(screen.getByText(
      /purchases, initializes, health-checks, recycles, and deletes/i,
    )).toBeInTheDocument()
    expect(screen.getByRole('link', {
      name: /configure auto-provisioning worker/i,
    })).toHaveAttribute(
      'href',
      '/settings/configuration?module=runtime_policy',
    )
    expect(screen.getByText(/current mode: full preparation/i))
      .toBeInTheDocument()
    expect(screen.getByRole('link', {
      name: /configure preparation mode/i,
    })).toHaveAttribute(
      'href',
      '/settings/configuration?module=runtime_policy&field=dedicated_pool_preparation_mode',
    )
    expect(screen.getByText(/global PAS worker setting/i))
      .toBeInTheDocument()
    expect(screen.getByRole('link', {
      name: /configure Alibaba Cloud credentials/i,
    })).toHaveAttribute(
      'href',
      '/settings/configuration?module=aliyun_access',
    )
    expect(screen.queryByText(/Agentic Dedicated purchase profile/i))
      .not.toBeInTheDocument()
    expect(await screen.findByText(/Alibaba Cloud access is not configured/i))
      .toBeInTheDocument()
    expect(screen.getAllByText('Runtime prerequisites')).toHaveLength(2)
    expect(screen.getByText('Capacity and cost')).toBeInTheDocument()
    expect(screen.getByText('Network and specification')).toBeInTheDocument()
    expect(screen.getByText('Agent access and lifecycle')).toBeInTheDocument()
    expect(screen.queryByLabelText(/purchase configuration/i)).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /next/i }))
    await user.type(screen.getByLabelText(/pool name/i), 'production-agents')
    expect(screen.getByText(/cooling instances do not count toward/i)).toBeInTheDocument()
    await user.click(screen.getByText(/advanced cost protection/i))
    expect(await screen.findByText('Purchases / hour')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /next/i }))

    await user.type(screen.getByLabelText(/^region id/i), 'cn-beijing')
    expect(await screen.findByRole('link', { name: /open Alibaba Cloud VPC console/i }))
      .toHaveAttribute('href', 'https://vpc.console.aliyun.com/vpc/cn-beijing/vpcs')
    await user.type(screen.getByLabelText(/^zone id/i), 'cn-beijing-k')
    await user.type(screen.getByLabelText(/^VPC id/i), 'vpc-test')
    await user.type(screen.getByLabelText(/^vSwitch id/i), 'vsw-test')
    expect(screen.getByText('essdpl1')).toBeInTheDocument()
    await user.click(screen.getByText(/view complete purchase parameters/i))
    expect(screen.getByText('8.0.2')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /next/i }))

    expect(screen.getByLabelText('Agent MySQL default permissions')).toBeInTheDocument()
    expect(screen.getByText(/Permissions granted to the MySQL account PAS creates/i))
      .toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /save as not started/i }))

    await waitFor(() => expect(createDedicatedPool).toHaveBeenCalledTimes(1))
    const request = vi.mocked(createDedicatedPool).mock.calls[0][0]
    expect(request).toMatchObject({
      name: 'production-agents',
      purchase_profile_id: 'agentic-dedicated-mysql',
      purchase_profile_revision: 1,
      storage_type: 'essdpl1',
      region_id: 'cn-beijing',
      zone_id: 'cn-beijing-k',
      vpc_id: 'vpc-test',
      vswitch_id: 'vsw-test',
      reclaim_policy: 'destroy',
      lifecycle_admin_policy: 'pas_managed',
    })
    expect(request).not.toHaveProperty('purchase_config')
  })

  it('offers prewarming when readiness has no blockers', async () => {
    vi.mocked(getDedicatedReadiness).mockResolvedValue({
      data: { ...blockedReadiness, blocking_reasons: [] },
    } as never)
    render(
      <MemoryRouter>
        <DedicatedPoolCreatePage />
      </MemoryRouter>,
    )

    expect(await screen.findByText(/all runtime prerequisites are ready/i))
      .toBeInTheDocument()
    expect(screen.queryByRole('link', {
      name: /configure auto-provisioning worker/i,
    })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', {
      name: /configure Alibaba Cloud credentials/i,
    })).not.toBeInTheDocument()
  })

  it.each([
    [
      'PURCHASE_PROFILE_INVALID',
      /upgrade PAS or contact the operator/i,
    ],
    [
      'PERMISSION_TEMPLATE_UNAVAILABLE',
      /installation or database migration/i,
    ],
  ])('explains internal readiness error %s without an incorrect repair link', async (
    code,
    expectedGuidance,
  ) => {
    vi.mocked(getDedicatedReadiness).mockResolvedValue({
      data: {
        ...blockedReadiness,
        worker: {
          ...blockedReadiness.worker,
          active_worker_count: 1,
        },
        aliyun_access: {
          ...blockedReadiness.aliyun_access,
          configured: true,
          validated: true,
        },
        blocking_reasons: [code],
      },
    } as never)

    render(
      <MemoryRouter>
        <DedicatedPoolCreatePage />
      </MemoryRouter>,
    )

    expect(await screen.findByText(expectedGuidance)).toBeInTheDocument()
    expect(screen.getByRole('link', {
      name: /configure preparation mode/i,
    })).toBeInTheDocument()
    expect(screen.queryByRole('link', {
      name: /configure auto-provisioning worker/i,
    })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', {
      name: /configure Alibaba Cloud credentials/i,
    })).not.toBeInTheDocument()
  })
})
