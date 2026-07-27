import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from './client'
import { listAllAdminInstances } from './instances'

vi.mock('./client', () => ({
  default: { get: vi.fn() },
}))

function instance(id: number) {
  return { id: `instance-${id}`, status: id % 2 ? 'active' : 'stopped' }
}

describe('instance pagination client', () => {
  beforeEach(() => vi.clearAllMocks())

  it('iterates beyond the 200-row API page limit without truncation', async () => {
    vi.mocked(api.get)
      .mockResolvedValueOnce({
        data: {
          items: Array.from({ length: 200 }, (_, index) => instance(index)),
          total: 201,
          offset: 0,
          limit: 200,
        },
      } as never)
      .mockResolvedValueOnce({
        data: {
          items: [instance(200)],
          total: 201,
          offset: 200,
          limit: 200,
        },
      } as never)

    const response = await listAllAdminInstances()

    expect(response.items).toHaveLength(201)
    expect(api.get).toHaveBeenNthCalledWith(1, '/api/instances', {
      params: { offset: 0, limit: 200 },
    })
    expect(api.get).toHaveBeenNthCalledWith(2, '/api/instances', {
      params: { offset: 200, limit: 200 },
    })
  })

  it('fails closed when a page is empty before total is reached', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: { items: [], total: 1, offset: 0, limit: 200 },
    } as never)

    await expect(listAllAdminInstances()).rejects.toThrow(
      /stopped before all results/i,
    )
  })

  it('rejects a response whose offset does not match the request', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: { items: [instance(0)], total: 1, offset: 1, limit: 200 },
    } as never)

    await expect(listAllAdminInstances()).rejects.toThrow(/offset mismatch/i)
  })

  it('rejects total drift between pages', async () => {
    vi.mocked(api.get)
      .mockResolvedValueOnce({
        data: {
          items: Array.from({ length: 200 }, (_, index) => instance(index)),
          total: 201,
          offset: 0,
          limit: 200,
        },
      } as never)
      .mockResolvedValueOnce({
        data: { items: [instance(200)], total: 202, offset: 200, limit: 200 },
      } as never)

    await expect(listAllAdminInstances()).rejects.toThrow(/total changed/i)
  })

  it('rejects a duplicate id on a partial final page', async () => {
    vi.mocked(api.get)
      .mockResolvedValueOnce({
        data: {
          items: Array.from({ length: 200 }, (_, index) => instance(index)),
          total: 201,
          offset: 0,
          limit: 200,
        },
      } as never)
      .mockResolvedValueOnce({
        data: { items: [instance(199)], total: 201, offset: 200, limit: 200 },
      } as never)

    await expect(listAllAdminInstances()).rejects.toThrow(/duplicate instance/i)
  })

  it('rejects a repeated nonempty page without unique progress', async () => {
    vi.mocked(api.get)
      .mockResolvedValueOnce({
        data: {
          items: [instance(0), instance(1)],
          total: 3,
          offset: 0,
          limit: 2,
        },
      } as never)
      .mockResolvedValueOnce({
        data: {
          items: [instance(0), instance(1)],
          total: 3,
          offset: 2,
          limit: 2,
        },
      } as never)

    await expect(listAllAdminInstances(2)).rejects.toThrow(
      /duplicate instance|no unique progress/i,
    )
  })

  it('rejects a page that exceeds its declared limit', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: {
        items: [instance(0), instance(1)],
        total: 2,
        offset: 0,
        limit: 1,
      },
    } as never)

    await expect(listAllAdminInstances()).rejects.toThrow(/exceeded.*limit/i)
  })

  it('rejects more unique rows than the stable total', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: {
        items: [instance(0), instance(1)],
        total: 1,
        offset: 0,
        limit: 200,
      },
    } as never)

    await expect(listAllAdminInstances()).rejects.toThrow(
      /more unique rows than total/i,
    )
  })
})
