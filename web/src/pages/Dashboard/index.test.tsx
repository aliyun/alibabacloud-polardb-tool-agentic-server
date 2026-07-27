import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, it, vi } from 'vitest'

import { getDashboardStats } from '../../api/dashboard'
import Dashboard from './index'

vi.mock('../../api/dashboard', () => ({
  getDashboardStats: vi.fn(),
}))

beforeEach(() => vi.clearAllMocks())

it('shows an error instead of zero statistics when loading fails', async () => {
  vi.mocked(getDashboardStats).mockRejectedValue(new Error('unavailable'))

  render(
    <MemoryRouter>
      <Dashboard />
    </MemoryRouter>,
  )

  expect(
    await screen.findByText(/could not load dashboard statistics/i),
  ).toBeInTheDocument()
  expect(screen.queryByText('Total Users')).not.toBeInTheDocument()
})
