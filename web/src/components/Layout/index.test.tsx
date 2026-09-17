import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { expect, it, vi } from 'vitest'

import AppLayout from './index'

const admin = {
  id: 'admin-1',
  external_id: 'admin',
  display_name: 'Administrator',
  email: null,
  role: 'admin' as const,
  status: 'active' as const,
}

it('groups advanced configuration under the unified resource and account navigation', () => {
  render(
    <MemoryRouter initialEntries={['/dashboard']}>
      <AppLayout
        user={admin}
        onLogout={vi.fn()}
        authMode="builtin"
      />
    </MemoryRouter>,
  )

  expect(screen.getByText('Resources')).toBeInTheDocument()
  expect(screen.getByText('Access management')).toBeInTheDocument()
  expect(screen.getByText('Connect MCP')).toBeInTheDocument()
  fireEvent.click(screen.getByText('Advanced settings'))
  expect(screen.getByText('Service Configuration')).toBeInTheDocument()
  expect(screen.queryByText('Quota Management')).not.toBeInTheDocument()
  expect(screen.queryByText(/^Settings$/)).not.toBeInTheDocument()
})
