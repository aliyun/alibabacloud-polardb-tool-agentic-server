import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import BackendSelector from './index'

describe('BackendSelector', () => {
  it('makes backend status and creation eligibility explicit', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(
      <BackendSelector
        backends={[
          {
            id: 'draining',
            instance_id: 'instance-1',
            admin_credential_id: 'credential-1',
            status: 'draining',
            priority: 0,
            max_active_resources: 10,
            resource_min_cpu: 1,
            resource_max_cpu: 2,
            ddl_concurrency: 4,
            config_revision: 1,
            healthy: false,
            health_checked_at: null,
            available_for_create: false,
            created_at: '2026-07-26T00:00:00Z',
            updated_at: null,
          },
        ]}
        instanceNames={{ 'instance-1': 'Tenant host' }}
        onChange={onChange}
      />,
    )

    await user.click(screen.getByRole('combobox', { name: /provisioning backend/i }))
    expect(screen.getByText(/tenant host.*draining.*not accepting new resources/i)).toBeInTheDocument()
    await user.click(
      screen.getByText(/tenant host.*draining.*not accepting new resources/i),
    )
    expect(onChange).not.toHaveBeenCalled()
  })
})
