import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from '../../api/client'
import Instances from './index'

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    default: {
      delete: vi.fn(),
      get: vi.fn(),
      post: vi.fn(),
    },
  }
})

const instance = {
  id: 'instance-1',
  cluster_id: 'pc-production',
  name: 'Production',
  usage: 'Finance reporting',
  engine: 'polardb_mysql',
  topology: 'single_tenant',
  allocation_mode: 'registered',
  status: 'active',
  region: 'cn-hangzhou',
  host: null,
  port: null,
  owner_user_id: null,
  health: null,
  binding_counts: { users: 2, departments: 1, agents: 3 },
}

describe('Instances page', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.get).mockResolvedValue({
      data: { items: [instance], total: 1, offset: 0, limit: 20 },
    } as never)
    vi.mocked(api.post).mockResolvedValue({ data: instance } as never)
  })

  it('registers a tested connection without exposing allocation mode', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter>
        <Instances />
      </MemoryRouter>,
    )

    await user.click(
      await screen.findByRole('button', { name: /register instance/i }),
    )
    expect(screen.getByLabelText(/engine/i)).toBeInTheDocument()
    expect(screen.getByText('PolarDB for MySQL')).toBeInTheDocument()
    expect(screen.getByLabelText(/topology/i)).toBeInTheDocument()
    expect(screen.getByText('Single tenant')).toBeInTheDocument()
    expect(screen.queryByLabelText(/allocation mode/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/^type$/i)).not.toBeInTheDocument()
    expect(screen.getByLabelText(/^port$/i)).toHaveValue('3306')
    expect(
      screen.getByText(/permissions are enforced by the mysql backend/i),
    ).toBeInTheDocument()

    await user.type(screen.getByLabelText(/cluster id/i), 'pc-new')
    await user.type(screen.getByLabelText(/^name$/i), 'New production')
    await user.type(
      screen.getByLabelText(/^usage$/i),
      '  Disaster recovery  ',
    )
    await user.type(screen.getByLabelText(/^host$/i), 'db.example.com')
    await user.type(screen.getByLabelText(/^username$/i), 'proxy_user')
    await user.type(screen.getByLabelText(/^password$/i), 'proxy_password')
    await user.click(screen.getByRole('button', { name: /save instance/i }))

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/instances', {
        cluster_id: 'pc-new',
        name: 'New production',
        usage: 'Disaster recovery',
        engine: 'polardb_mysql',
        topology: 'single_tenant',
        region: undefined,
        host: 'db.example.com',
        port: 3306,
        username: 'proxy_user',
        password: 'proxy_password',
      }),
    )
    expect(JSON.stringify(vi.mocked(api.post).mock.calls)).not.toContain('"type"')
  })

  it('shows provisioning state without implying general health', async () => {
    render(
      <MemoryRouter>
        <Instances />
      </MemoryRouter>,
    )

    expect(await screen.findByText('polardb_mysql')).toBeInTheDocument()
    expect(screen.getByText('single_tenant')).toBeInTheDocument()
    expect(screen.getByText('registered')).toBeInTheDocument()
    expect(
      screen.getByRole('columnheader', { name: 'Provisioning' }),
    ).toBeInTheDocument()
    expect(screen.getByText(/not enabled/i)).toBeInTheDocument()
    expect(
      screen.queryByRole('columnheader', { name: 'Health' }),
    ).not.toBeInTheDocument()
    expect(screen.getByText(/2 users/i)).toBeInTheDocument()
    expect(screen.getByText(/1 department/i)).toBeInTheDocument()
    expect(screen.getByText(/3 agents/i)).toBeInTheDocument()
    expect(screen.getByText('Finance reporting')).toBeInTheDocument()
  })

  it('shows an explicit empty state when usage is not specified', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: {
        items: [{ ...instance, usage: null }],
        total: 1,
        offset: 0,
        limit: 20,
      },
    } as never)

    render(
      <MemoryRouter>
        <Instances />
      </MemoryRouter>,
    )

    expect(await screen.findByText('Not specified')).toBeInTheDocument()
  })

  it('tests the complete connection tuple before registration', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter>
        <Instances />
      </MemoryRouter>,
    )
    await user.click(
      await screen.findByRole('button', { name: /register instance/i }),
    )
    await user.type(screen.getByLabelText(/^host$/i), 'db.example.invalid')
    await user.type(screen.getByLabelText(/^username$/i), 'proxy_user')
    await user.type(screen.getByLabelText(/^password$/i), 'proxy_password')
    await user.click(
      screen.getByRole('button', { name: /test connection/i }),
    )

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith(
        '/api/instances/test-connection',
        {
          topology: 'single_tenant',
          host: 'db.example.invalid',
          port: 3306,
          username: 'proxy_user',
          password: 'proxy_password',
        },
      ),
    )
    expect(await screen.findByText(/connection succeeded/i)).toBeInTheDocument()
  })

  it('tests multitenant prerequisites and keeps the failure inline', async () => {
    const user = userEvent.setup()
    vi.mocked(api.post).mockRejectedValueOnce({
      isAxiosError: true,
      response: {
        data: {
          detail: {
            code: 'MULTITENANT_DISABLED',
            message:
              'PolarDB multitenant mode is not enabled. Enable it and restart the cluster before registration.',
          },
        },
      },
    })
    render(
      <MemoryRouter>
        <Instances />
      </MemoryRouter>,
    )

    await user.click(
      await screen.findByRole('button', { name: /register instance/i }),
    )
    await user.click(screen.getByLabelText(/topology/i))
    await user.click(await screen.findByText('Multi-tenant'))
    await user.type(screen.getByLabelText(/^host$/i), 'db.example.invalid')
    await user.type(screen.getByLabelText(/^username$/i), 'proxy_user')
    await user.type(screen.getByLabelText(/^password$/i), 'proxy_password')
    await user.click(
      screen.getByRole('button', { name: /test connection/i }),
    )

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith(
        '/api/instances/test-connection',
        {
          topology: 'multitenant',
          host: 'db.example.invalid',
          port: 3306,
          username: 'proxy_user',
          password: 'proxy_password',
        },
      ),
    )
    const dialog = screen.getByRole('dialog', {
      name: /register instance/i,
    })
    expect(
      await within(dialog).findByText(
        /polardb multitenant mode is not enabled/i,
      ),
    ).toBeInTheDocument()

    await user.click(screen.getByLabelText(/topology/i))
    await user.click(await screen.findByText('Single tenant'))
    expect(
      within(dialog).queryByText(
        /polardb multitenant mode is not enabled/i,
      ),
    ).not.toBeInTheDocument()
  })

  it('groups registration fields into a compact responsive layout', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter>
        <Instances />
      </MemoryRouter>,
    )

    await user.click(
      await screen.findByRole('button', { name: /register instance/i }),
    )

    const dialog = screen.getByRole('dialog', { name: /register instance/i })
    expect(dialog).toHaveStyle({ width: '760px' })

    const identity = within(dialog).getByRole('group', {
      name: /instance identity/i,
    })
    expect(within(identity).getByLabelText(/cluster id/i)).toBeInTheDocument()
    expect(within(identity).getByLabelText(/^name$/i)).toBeInTheDocument()

    const classification = within(dialog).getByRole('group', {
      name: /instance classification/i,
    })
    expect(within(classification).getByLabelText(/engine/i)).toBeInTheDocument()
    expect(
      within(classification).getByLabelText(/topology/i),
    ).toBeInTheDocument()

    const location = within(dialog).getByRole('group', {
      name: /instance location/i,
    })
    expect(within(location).getByLabelText(/region/i)).toBeInTheDocument()
    expect(within(location).getByLabelText(/^port$/i)).toBeInTheDocument()

    const endpoint = within(dialog).getByRole('group', {
      name: /instance endpoint/i,
    })
    expect(within(endpoint).getByLabelText(/^host$/i)).toBeInTheDocument()

    const credentials = within(dialog).getByRole('group', {
      name: /instance credentials/i,
    })
    expect(
      within(credentials).getByLabelText(/^username$/i),
    ).toBeInTheDocument()
    expect(
      within(credentials).getByLabelText(/^password$/i),
    ).toBeInTheDocument()
  })

  it('keeps a connection error inline until a connection field changes', async () => {
    const user = userEvent.setup()
    vi.mocked(api.post).mockRejectedValueOnce({
      isAxiosError: true,
      response: {
        data: {
          detail: {
            code: 'CONNECTION_FAILED',
            message: 'Database endpoint is unreachable',
          },
        },
      },
    })
    render(
      <MemoryRouter>
        <Instances />
      </MemoryRouter>,
    )

    await user.click(
      await screen.findByRole('button', { name: /register instance/i }),
    )
    await user.type(screen.getByLabelText(/^host$/i), 'db.example.invalid')
    await user.type(screen.getByLabelText(/^username$/i), 'proxy_user')
    await user.type(screen.getByLabelText(/^password$/i), 'proxy_password')
    await user.click(
      screen.getByRole('button', { name: /test connection/i }),
    )

    const dialog = screen.getByRole('dialog', { name: /register instance/i })
    const connectionError = await within(dialog).findByText(
      /database endpoint is unreachable/i,
    )
    expect(connectionError.closest('[role="alert"]')).toBeInTheDocument()

    await user.type(screen.getByLabelText(/^host$/i), '.cn')
    expect(
      within(dialog).queryByText(/database endpoint is unreachable/i),
    ).not.toBeInTheDocument()
  })
})
