import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from './client'
import { getDashboardStats } from './dashboard'
import { listAllAdminInstances } from './instances'

vi.mock('./client', () => ({
  default: { get: vi.fn() },
}))
vi.mock('./instances', () => ({
  listAllAdminInstances: vi.fn(),
}))

describe('dashboard statistics', () => {
  beforeEach(() => vi.clearAllMocks())

  it('uses paged instance total and exact active items', async () => {
    vi.mocked(listAllAdminInstances).mockResolvedValue({
      items: [
        { status: 'active' },
        { status: 'stopped' },
        { status: 'active' },
      ],
      total: 3,
      offset: 0,
      limit: 200,
    } as never)
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/api/users') {
        return Promise.resolve({ data: { total: 7 } } as never)
      }
      if (url === '/api/pool/status') {
        return Promise.resolve({ data: { available: 4 } } as never)
      }
      return Promise.resolve({ data: [{ id: 'department-1' }] } as never)
    })

    await expect(getDashboardStats()).resolves.toEqual({
      total_users: 7,
      total_instances: 3,
      active_instances: 2,
      pool_available: 4,
      departments: 1,
      queries_today: 0,
    })
  })

  it('propagates a statistics request failure', async () => {
    vi.mocked(listAllAdminInstances).mockRejectedValue(
      new Error('instances unavailable'),
    )
    vi.mocked(api.get).mockResolvedValue({ data: [] } as never)

    await expect(getDashboardStats()).rejects.toThrow('instances unavailable')
  })
})
