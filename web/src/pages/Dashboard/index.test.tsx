import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, it, vi } from 'vitest'

import { getDashboardStats } from '../../api/dashboard'
import Dashboard from './index'
import { createTestI18n } from '../../i18n/i18n'
import LocaleProvider from '../../i18n/LocaleProvider'

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

it('renders the dashboard in Simplified Chinese', async () => {
  vi.mocked(getDashboardStats).mockResolvedValue({
    total_users: 3,
    total_instances: 2,
    active_instances: 1,
    pool_available: 0,
    departments: 1,
    queries_today: 8,
  })

  render(
    <LocaleProvider i18nInstance={createTestI18n('zh-CN')}>
      <MemoryRouter><Dashboard /></MemoryRouter>
    </LocaleProvider>,
  )

  expect(await screen.findByRole('heading', { name: '仪表盘' })).toBeInTheDocument()
  expect(screen.getByText('快速操作')).toBeInTheDocument()
})
