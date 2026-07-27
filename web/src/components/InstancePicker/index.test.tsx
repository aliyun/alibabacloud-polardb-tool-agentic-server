import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import InstancePicker from './index'

const instances = [
  {
    id: 'ready',
    cluster_id: 'pc-ready',
    name: 'Orders',
    usage: null,
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
  },
  {
    id: 'blocked',
    cluster_id: 'pc-blocked',
    name: 'Archive',
    usage: null,
    engine: 'polardb_mysql' as const,
    topology: 'single_tenant' as const,
    allocation_mode: 'registered' as const,
    region: 'cn-hangzhou',
    host: null,
    port: null,
    status: 'stopped',
    owner_user_id: null,
    health: null,
    binding_counts: { users: 0, departments: 0, agents: 0 },
  },
]

describe('InstancePicker', () => {
  it('shows why an instance is not eligible and prevents selecting it', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(
      <InstancePicker
        instances={instances}
        onChange={onChange}
        getEligibility={(instance) =>
          instance.status === 'active'
            ? { eligible: true }
            : { eligible: false, reason: 'Instance is stopped' }
        }
      />,
    )

    await user.click(screen.getByRole('combobox', { name: /database instance/i }))
    expect(screen.getByText(/archive.*stopped.*instance is stopped/i)).toBeInTheDocument()
    await user.click(screen.getByText(/archive.*stopped.*instance is stopped/i))
    expect(onChange).not.toHaveBeenCalled()
  })
})
