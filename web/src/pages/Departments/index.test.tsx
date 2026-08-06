import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from '../../api/client'
import Departments from './index'
import { createTestI18n } from '../../i18n/i18n'
import LocaleProvider from '../../i18n/LocaleProvider'

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    default: {
      delete: vi.fn(),
      get: vi.fn(),
      post: vi.fn(),
      put: vi.fn(),
    },
  }
})

const multitenantInstance = {
  id: 'instance-mt',
  cluster_id: 'pc-multitenant',
  name: 'Shared multitenant',
  engine: 'polardb_mysql',
  topology: 'multitenant',
  allocation_mode: 'registered',
  status: 'active',
  region: 'cn-hangzhou',
  host: 'db.example.com',
  port: 3306,
  owner_user_id: null,
  health: null,
  binding_counts: { users: 0, departments: 0, agents: 0 },
}

describe('Departments page', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.get).mockImplementation(async (url) => {
      if (url === '/api/departments') {
        return {
          data: [
            {
              id: 'department-1',
              name: 'Finance',
              description: null,
              max_instances: null,
            },
          ],
        } as never
      }
      if (url === '/api/departments/department-1/multitenant-instance') {
        return { data: null } as never
      }
      if (url === '/api/instances') {
        return {
          data: {
            items: [multitenantInstance],
            total: 1,
            offset: 0,
            limit: 200,
          },
        } as never
      }
      throw new Error(`Unexpected GET ${url}`)
    })
    vi.mocked(api.post).mockResolvedValue({ data: {} } as never)
  })

  it('binds a previously registered multitenant instance', async () => {
    const user = userEvent.setup()
    render(<Departments />)

    await user.click(
      await screen.findByRole('button', { name: /expand row/i }),
    )
    await user.click(
      screen.getByRole('button', { name: /^bind instance$/i }),
    )

    expect(
      await screen.findByRole('combobox', {
        name: /^multitenant instance$/i,
      }),
    ).toBeInTheDocument()
    expect(screen.queryByLabelText(/^host$/i)).not.toBeInTheDocument()
    expect(
      screen.queryByLabelText(/admin password/i),
    ).not.toBeInTheDocument()

    await user.click(
      screen.getByRole('combobox', { name: /^multitenant instance$/i }),
    )
    await user.click(
      await screen.findByText(/shared multitenant.*pc-multitenant/i),
    )
    await user.click(screen.getByRole('button', { name: /^ok$/i }))

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith(
        '/api/departments/department-1/multitenant-instance',
        { instance_id: 'instance-mt' },
      ),
    )
  })

  it('renders department management in Simplified Chinese', async () => {
    render(
      <LocaleProvider i18nInstance={createTestI18n('zh-CN')}>
        <Departments />
      </LocaleProvider>,
    )

    expect(await screen.findByRole('heading', { name: '部门' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /新建部门/ })).toBeInTheDocument()
  })
})
