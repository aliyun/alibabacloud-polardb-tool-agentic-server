import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from '../../api/client'
import PrincipalsPanel from './PrincipalsPanel'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  }
})

describe('PrincipalsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/api/identity-sources/users/user-1/identities') {
        return Promise.resolve({ data: { items: [] } } as never)
      }
      if (url === '/api/identity-sources') {
        return Promise.resolve({
          data: { items: [{ id: 'source-1', name: 'Feishu directory', provider: 'feishu' }] },
        } as never)
      }
      if (url.includes('/directory?entry_type=users')) {
        return Promise.resolve({
          data: {
            users: [{ external_user_id: 'user-201', display_name: 'User 201' }],
            total: 205,
          },
        } as never)
      }
      return Promise.resolve({ data: {} } as never)
    })
  })

  it('loads enterprise identity candidates through the searchable directory endpoint', async () => {
    const user = userEvent.setup()
    render(<PrincipalsPanel userId="user-1" userName="Local user" />)

    await user.click((await screen.findByText('Bind enterprise identity')).closest('button')!)

    await waitFor(() => {
      expect(api.get).toHaveBeenCalledWith(
        '/api/identity-sources/source-1/directory?entry_type=users&offset=0&limit=20',
      )
    })
  })

  it('keeps the newest candidate search result when responses arrive out of order', async () => {
    const alice = deferred<unknown>()
    const bob = deferred<unknown>()
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/api/identity-sources/users/user-1/identities') {
        return Promise.resolve({ data: { items: [] } } as never)
      }
      if (url === '/api/identity-sources') {
        return Promise.resolve({
          data: { items: [{ id: 'source-1', name: 'Feishu directory', provider: 'feishu' }] },
        } as never)
      }
      if (url.includes('search=alice')) return alice.promise as never
      if (url.includes('search=bob')) return bob.promise as never
      if (url.includes('/directory?entry_type=users')) {
        return Promise.resolve({ data: { users: [], total: 0 } } as never)
      }
      return Promise.resolve({ data: {} } as never)
    })
    const user = userEvent.setup()
    render(<PrincipalsPanel userId="user-1" userName="Local user" />)

    await user.click((await screen.findByText('Bind enterprise identity')).closest('button')!)
    const search = await screen.findByRole('combobox')
    await user.type(search, 'alice')
    await waitFor(() => expect(api.get).toHaveBeenCalledWith(
      '/api/identity-sources/source-1/directory?entry_type=users&offset=0&limit=20&search=alice',
    ))
    await user.clear(search)
    await user.type(search, 'bob')
    await waitFor(() => expect(api.get).toHaveBeenCalledWith(
      '/api/identity-sources/source-1/directory?entry_type=users&offset=0&limit=20&search=bob',
    ))

    bob.resolve({ data: { users: [{ external_user_id: 'bob', display_name: 'Bob' }], total: 1 } })
    await screen.findByText('Feishu directory · Bob · bob')
    alice.resolve({ data: { users: [{ external_user_id: 'alice', display_name: 'Alice' }], total: 1 } })
    await new Promise((resolve) => setTimeout(resolve, 0))

    await waitFor(() => {
      expect(screen.queryByText('Feishu directory · Alice · alice')).not.toBeInTheDocument()
      expect(screen.getByText('Feishu directory · Bob · bob')).toBeInTheDocument()
    })
  })
})
