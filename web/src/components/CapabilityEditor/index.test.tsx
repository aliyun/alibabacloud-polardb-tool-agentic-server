import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'

import type {
  AgentInstanceAccessCapability,
  BindingCapability,
  DirectBindingCapability,
} from '../../api/instanceAccess'
import type { InstanceSummary } from '../../api/instances'
import type { ProvisioningBackend } from '../../api/provisioningBackends'
import CapabilityEditor from './index'

describe('CapabilityEditor', () => {
  it('adds visible dependencies and removes dependent capabilities', async () => {
    const user = userEvent.setup()
    let latest: DirectBindingCapability[] = []

    function Harness() {
      const [value, setValue] = useState<DirectBindingCapability[]>([])
      return (
        <CapabilityEditor
          value={value}
          onChange={(next) => {
            latest = next
            setValue(next)
          }}
        />
      )
    }

    render(<Harness />)
    expect(
      screen.queryByRole('checkbox', {
        name: /enable sql over http proxy/i,
      }),
    ).not.toBeInTheDocument()
    await user.click(screen.getByRole('checkbox', { name: /reveal credentials/i }))

    expect(latest).toEqual([
      'db_instance:list',
      'db_instance:describe',
      'db_instance:credentials:read',
    ])
    expect(latest).not.toContain('sql:read')
    expect(latest).not.toContain('sql:write')

    await user.click(screen.getByRole('checkbox', { name: /list bound instances/i }))
    expect(latest).toEqual([])
  })

  it('toggles SQL proxy access without changing instance capabilities', async () => {
    const user = userEvent.setup()
    let latest: BindingCapability[] = []

    function Harness() {
      const [value, setValue] = useState<BindingCapability[]>([
        'db_instance:list',
        'sql:read',
      ])
      return (
        <CapabilityEditor
          permission="readonly"
          value={value}
          onChange={(next) => {
            latest = next
            setValue(next)
          }}
        />
      )
    }

    render(<Harness />)
    const proxy = screen.getByRole('checkbox', {
      name: /enable sql over http proxy/i,
    })
    expect(proxy).toBeChecked()

    await user.click(proxy)
    expect(latest).toEqual(['db_instance:list'])

    await user.click(proxy)
    expect(latest).toEqual(['db_instance:list', 'sql:read'])
  })

  it('offers managed database creation disabled by default', async () => {
    const user = userEvent.setup()
    let latest: AgentInstanceAccessCapability[] = []
    const multitenant = {
      id: 'multitenant-1',
      engine: 'polardb_mysql',
      topology: 'multitenant',
      allocation_mode: 'registered',
      status: 'active',
    } as InstanceSummary
    const backend = {
      id: 'backend-1',
      instance_id: multitenant.id,
      status: 'active',
      healthy: true,
      health_checked_at: '2026-07-27T00:00:00Z',
      available_for_create: true,
    } as ProvisioningBackend

    function Harness() {
      const [value, setValue] = useState<
        AgentInstanceAccessCapability[]
      >([])
      return (
        <CapabilityEditor
          value={value}
          onChange={(next) => {
            latest = next
            setValue(next)
          }}
          instance={multitenant}
          provisioningBackend={backend}
        />
      )
    }

    render(<Harness />)
    const create = screen.getByRole('checkbox', {
      name: /create managed databases/i,
    })
    expect(create).not.toBeChecked()
    await user.click(create)
    expect(latest).toEqual(['db_instance:create'])
  })

  it('explains when a multitenant backend must be configured', () => {
    render(
      <CapabilityEditor
        value={[]}
        onChange={() => undefined}
        instance={{
          id: 'multitenant-1',
          engine: 'polardb_mysql',
          topology: 'multitenant',
          allocation_mode: 'registered',
          status: 'active',
        } as InstanceSummary}
      />,
    )

    expect(
      screen.getByRole('checkbox', {
        name: /create managed databases/i,
      }),
    ).toBeDisabled()
    expect(
      screen.getByText(
        /configure a provisioning backend for this instance first/i,
      ),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: /configure backend/i }),
    ).toHaveAttribute('href', '/instances/multitenant-1')
  })
})
