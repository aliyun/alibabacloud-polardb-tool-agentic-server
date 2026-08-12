import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from './client'
import { getDashboardStats } from './dashboard'
import { listDedicatedPools } from './dedicatedPools'
import { listAllAdminInstances } from './instances'
import { listPolarRAGInstances } from './polarrag'

vi.mock('./client', () => ({
  default: { get: vi.fn() },
}))
vi.mock('./instances', () => ({
  listAllAdminInstances: vi.fn(),
}))
vi.mock('./dedicatedPools', () => ({
  listDedicatedPools: vi.fn(),
}))
vi.mock('./polarrag', () => ({
  listPolarRAGInstances: vi.fn(),
}))

describe('dashboard statistics', () => {
  beforeEach(() => vi.clearAllMocks())

  it('combines database and PolarRAG instance statistics', async () => {
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
    vi.mocked(listPolarRAGInstances).mockResolvedValue({
      data: {
        items: [
          { status: 'active' },
          { status: 'error' },
        ],
      },
    } as never)
    vi.mocked(api.get).mockImplementation((url: string, config) => {
      if (url === '/api/users') {
        return Promise.resolve({ data: { total: 7 } } as never)
      }
      if (url === '/api/audit-logs') {
        return Promise.resolve({
          data: {
            total: config?.params?.category === 'sql' ? 5 : 7,
            items: [],
          },
        } as never)
      }
      return Promise.resolve({ data: [{ id: 'department-1' }] } as never)
    })
    vi.mocked(listDedicatedPools).mockResolvedValue({
      data: [{ allocatable: 2 }, { allocatable: 3 }],
    } as never)

    await expect(getDashboardStats()).resolves.toEqual({
      total_users: 7,
      total_instances: 5,
      active_instances: 3,
      dedicated_allocatable: 5,
      departments: 1,
      queries_today: 12,
    })
  })

  it('propagates a statistics request failure', async () => {
    vi.mocked(listAllAdminInstances).mockRejectedValue(
      new Error('instances unavailable'),
    )
    vi.mocked(api.get).mockResolvedValue({ data: [] } as never)
    vi.mocked(listDedicatedPools).mockResolvedValue({ data: [] } as never)
    vi.mocked(listPolarRAGInstances).mockResolvedValue({
      data: { items: [] },
    } as never)

    await expect(getDashboardStats()).rejects.toThrow('instances unavailable')
  })

  it('uses only current-user resources for member statistics', async () => {
    vi.mocked(listAllAdminInstances).mockRejectedValue(
      new Error('admin instance API must not be called'),
    )
    vi.mocked(listPolarRAGInstances).mockRejectedValue(
      new Error('admin PolarRAG API must not be called'),
    )
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/api/me/resources') {
        return Promise.resolve({
          data: {
            database_instances: [{ db_instance_id: 'db-1' }],
            knowledge_resources: [
              { knowledge_resource_id: 'kb-1' },
              { knowledge_resource_id: 'kb-2' },
            ],
          },
        } as never)
      }
      return Promise.reject(new Error(`unexpected admin API: ${url}`))
    })

    await expect(getDashboardStats(false)).resolves.toEqual({
      database_instances: 1,
      knowledge_resources: 2,
    })
  })
})
