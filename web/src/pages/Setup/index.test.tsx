import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, expect, it, vi } from 'vitest'

import App from '../../App'
import {
  discoverSystemState,
  executeConfig,
  type ConfigModule,
  type ConfigResponse,
} from '../../api/configuration'
import Setup from './index'
import { createTestI18n } from '../../i18n/i18n'
import LocaleProvider from '../../i18n/LocaleProvider'

vi.mock('../../hooks/useAuth', () => ({
  useAuth: () => ({
    user: {
      id: 'admin-1',
      external_id: 'admin',
      display_name: 'Administrator',
      email: null,
      role: 'admin',
      status: 'active',
    },
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
    authMode: 'builtin',
  }),
}))

vi.mock('../../api/configuration', async () => {
  const actual = await vi.importActual<typeof import('../../api/configuration')>(
    '../../api/configuration',
  )
  return {
    ...actual,
    discoverSystemState: vi.fn(),
    executeConfig: vi.fn(),
  }
})

beforeEach(() => {
  vi.mocked(discoverSystemState).mockResolvedValue('SETUP')
  vi.mocked(executeConfig).mockReset()
  window.history.replaceState({}, '', '/dashboard')
})

function coreAdmin(revision = 0): ConfigModule {
  return {
    name: 'core_admin',
    revision,
    workflow_state: revision === 0 ? 'NOT_CONFIGURED' : 'VALIDATED',
    draft: revision === 0 ? null : { username: 'admin' },
    effective: null,
    dependencies: ['token_security'],
    dependents: [],
    ui_hints: { secret_fields: ['password'] },
    schema: {
      type: 'object',
      required: ['password'],
      properties: {
        username: {
          type: 'string',
          title: 'Username',
          default: 'admin',
        },
        password: {
          type: 'string',
          title: 'Administrator password',
          minLength: 12,
        },
      },
    },
  }
}

function response(
  values: Partial<ConfigResponse> = {},
): ConfigResponse {
  return {
    config_version: 1,
    system_state: 'SETUP',
    ...values,
  }
}

function runtimePolicy(): ConfigModule {
  return {
    name: 'runtime_policy',
    revision: 4,
    workflow_state: 'ACTIVE',
    draft: null,
    effective: {
      revision: 4,
      state: 'ACTIVE',
      config: { config_poll_interval_seconds: 5 },
    },
    dependencies: [],
    dependents: [],
    schema: {
      type: 'object',
      properties: {
        config_poll_interval_seconds: {
          type: 'integer',
          title: 'Configuration poll interval seconds',
          minimum: 1,
          maximum: 60,
        },
      },
    },
  }
}

function activeCoreAdmin(): ConfigModule {
  return {
    ...coreAdmin(4),
    workflow_state: 'ACTIVE',
    effective: {
      revision: 4,
      state: 'ACTIVE',
      config: { username: 'admin' },
    },
  }
}

async function claimInstallation(
  user: ReturnType<typeof userEvent.setup>,
) {
  await user.type(screen.getByLabelText(/bootstrap token/i), 'bootstrap')
  await user.click(
    screen.getByRole('button', { name: /verify and continue/i }),
  )
  await screen.findByRole('heading', { name: /core admin/i })
}

function renderedActions() {
  return vi.mocked(executeConfig).mock.calls.map(([command]) => command.action)
}

it('routes setup mode to a standalone ownership screen', async () => {
  render(<App />)

  expect(await screen.findByRole('heading', { name: /claim this installation/i })).toBeInTheDocument()
  expect(screen.getByLabelText(/bootstrap token/i)).toHaveAttribute('type', 'password')
  expect(screen.queryByText(/dashboard/i)).not.toBeInTheDocument()
  await waitFor(() => expect(window.location.pathname).toBe('/setup'))
})

it('renders the ownership screen and language control in Chinese', () => {
  render(
    <LocaleProvider i18nInstance={createTestI18n('zh-CN')}>
      <MemoryRouter>
        <Setup />
      </MemoryRouter>
    </LocaleProvider>,
  )

  expect(screen.getByRole('heading', { name: '接管此安装' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '切换语言' })).toBeInTheDocument()
})

it('does not route to login when setup discovery fails', async () => {
  vi.mocked(discoverSystemState).mockRejectedValue(
    new Error('backend unavailable'),
  )

  render(<App />)

  expect(
    await screen.findByRole('heading', {
      name: /cannot determine server state/i,
    }),
  ).toBeInTheDocument()
  expect(
    screen.queryByRole('heading', { name: /sign in/i }),
  ).not.toBeInTheDocument()
  expect(window.location.pathname).toBe('/dashboard')
})

it('runs a read-only dry-run and invalidates it after editing', async () => {
  const user = userEvent.setup()
  vi.mocked(executeConfig)
    .mockResolvedValueOnce(response({ modules: [coreAdmin()] }))
    .mockResolvedValueOnce(
      response({
        plan: {
          valid: true,
          message: 'Configuration is valid',
          writes: false,
        },
      }),
    )

  render(
    <MemoryRouter>
      <Setup />
    </MemoryRouter>,
  )
  await claimInstallation(user)
  expect(
    screen.queryByRole('button', {
      name: /enter administration console/i,
    }),
  ).not.toBeInTheDocument()
  await user.type(
    screen.getByLabelText(/administrator password/i),
    'correct horse battery staple',
  )
  await user.click(
    screen.getByRole('button', { name: /^run dry-run$/i }),
  )

  await screen.findByText(/dry-run checks passed/i)
  expect(renderedActions()).toEqual(['describe', 'plan'])
  expect(vi.mocked(executeConfig).mock.calls[1][0].config).toEqual({
    username: 'admin',
    password: 'correct horse battery staple',
  })
  expect(
    screen.getByRole('button', { name: /activate module/i }),
  ).toBeEnabled()

  await user.type(screen.getByLabelText(/username/i), '-edited')

  expect(
    screen.queryByRole('button', { name: /activate module/i }),
  ).not.toBeInTheDocument()
  expect(
    screen.queryByText(/dry-run checks passed/i),
  ).not.toBeInTheDocument()
})

it('shows backend OpenAPI endpoints returned by an Aliyun dry-run', async () => {
  const user = userEvent.setup()
  const aliyunModule: ConfigModule = {
    name: 'aliyun_access',
    revision: 0,
    workflow_state: 'NOT_CONFIGURED',
    draft: null,
    effective: null,
    dependencies: [],
    dependents: [],
    ui_hints: { secret_fields: ['access_key_id', 'access_key_secret'] },
    schema: {
      type: 'object',
      properties: {
        access_key_id: { type: 'string', title: 'Access key ID' },
        access_key_secret: {
          type: 'string',
          title: 'Access key secret',
        },
      },
      required: ['access_key_id', 'access_key_secret'],
    },
  }
  vi.mocked(executeConfig)
    .mockResolvedValueOnce(
      response({ modules: [coreAdmin(), aliyunModule] }),
    )
    .mockResolvedValueOnce(
      response({
        plan: {
          valid: true,
          message: 'Configuration is valid',
          writes: false,
          external_validation: {
            status: 'PASSED',
            checks: [
              {
                service: 'polardb',
                network: 'vpc',
                endpoint: 'polardb-vpc.cn-beijing.aliyuncs.com',
                status: 'REACHABLE',
              },
            ],
          },
        },
      }),
    )

  render(
    <MemoryRouter>
      <Setup />
    </MemoryRouter>,
  )
  await claimInstallation(user)
  await user.click(
    screen.getByRole('button', { name: /aliyun access/i }),
  )
  await user.type(screen.getByLabelText(/access key id/i), 'test-ak')
  await user.type(
    screen.getByLabelText(/access key secret/i),
    'test-secret',
  )
  await user.click(screen.getByRole('button', { name: /^run dry-run$/i }))

  expect(
    await screen.findByText(
      /polardb-vpc\.cn-beijing\.aliyuncs\.com/,
    ),
  ).toBeInTheDocument()
  expect(screen.getByText(/checked by the backend pod/i)).toBeInTheDocument()
})

it('refreshes the revision after a failed activation mutation', async () => {
  const user = userEvent.setup()
  vi.mocked(executeConfig)
    .mockResolvedValueOnce(response({ modules: [coreAdmin()] }))
    .mockResolvedValueOnce(
      response({
        plan: {
          valid: true,
          message: 'Configuration is valid',
          writes: false,
        },
      }),
    )
    .mockResolvedValueOnce(
      response({ module: { ...coreAdmin(1), workflow_state: 'DRAFT' } }),
    )
    .mockResolvedValueOnce(
      response({
        module: coreAdmin(3),
        validation: {
          status: 'PASSED',
          validation_id: 'proof',
        },
      }),
    )
    .mockRejectedValueOnce(new Error('activation rejected'))
    .mockResolvedValueOnce(response({ modules: [coreAdmin(3)] }))

  render(
    <MemoryRouter>
      <Setup />
    </MemoryRouter>,
  )
  await claimInstallation(user)
  await user.type(
    screen.getByLabelText(/administrator password/i),
    'correct horse battery staple',
  )
  await user.click(
    screen.getByRole('button', { name: /^run dry-run$/i }),
  )
  await user.click(
    await screen.findByRole('button', { name: /activate module/i }),
  )

  expect(await screen.findByText('Revision 3')).toBeInTheDocument()
  expect(renderedActions()).toEqual([
    'describe',
    'plan',
    'save_draft',
    'validate',
    'activate',
    'describe',
  ])
  expect(vi.mocked(executeConfig).mock.calls[2][0].config).toEqual({
    username: 'admin',
  })
  expect(vi.mocked(executeConfig).mock.calls[4][0].config).toEqual({
    password: 'correct horse battery staple',
  })
  expect(
    screen.queryByRole('button', { name: /activate module/i }),
  ).not.toBeInTheDocument()
  expect(
    screen.getByRole('button', { name: /^run dry-run$/i }),
  ).toBeEnabled()
})

it('offers dashboard navigation when discovery reports READY', async () => {
  const user = userEvent.setup()
  vi.mocked(executeConfig).mockResolvedValueOnce(
    response({
      system_state: 'READY',
      modules: [
        {
          ...coreAdmin(4),
          workflow_state: 'ACTIVE',
          effective: {
            revision: 4,
            state: 'ACTIVE',
            config: { username: 'admin' },
          },
        },
      ],
    }),
  )

  render(
    <MemoryRouter initialEntries={['/setup']}>
      <Routes>
        <Route path="/setup" element={<Setup />} />
        <Route
          path="/dashboard"
          element={<h1>Dashboard destination</h1>}
        />
      </Routes>
    </MemoryRouter>,
  )
  await claimInstallation(user)

  await user.click(
    screen.getByRole('button', {
      name: /enter administration console/i,
    }),
  )
  expect(
    await screen.findByRole('heading', {
      name: /dashboard destination/i,
    }),
  ).toBeInTheDocument()
})

it('enters the dashboard after setup becomes READY', async () => {
  const user = userEvent.setup()
  vi.mocked(executeConfig).mockResolvedValueOnce(
    response({
      system_state: 'READY',
      modules: [activeCoreAdmin()],
    }),
  )

  render(<App />)
  await screen.findByRole('heading', { name: /claim this installation/i })
  await claimInstallation(user)

  await user.click(
    screen.getByRole('button', {
      name: /enter administration console/i,
    }),
  )

  await waitFor(() =>
    expect(window.location.pathname).toBe('/dashboard'),
  )
  expect(
    screen.queryByRole('heading', { name: /claim this installation/i }),
  ).not.toBeInTheDocument()
})

it('redirects READY setup visits to authenticated configuration', async () => {
  vi.mocked(discoverSystemState).mockResolvedValue('READY')
  vi.mocked(executeConfig).mockResolvedValueOnce(
    response({
      system_state: 'READY',
      modules: [activeCoreAdmin(), runtimePolicy()],
    }),
  )
  window.history.replaceState({}, '', '/setup')

  render(<App />)

  await waitFor(() =>
    expect(window.location.pathname).toBe('/settings/configuration'),
  )
  expect(
    await screen.findByRole('heading', { name: /configure the server/i }),
  ).toBeInTheDocument()
  expect(executeConfig).toHaveBeenCalledWith(
    { action: 'describe' },
    undefined,
  )
  expect(
    screen.queryByRole('heading', { name: /claim this installation/i }),
  ).not.toBeInTheDocument()
})

it('allows an administrator to dry-run an active module update', async () => {
  const user = userEvent.setup()
  vi.mocked(executeConfig)
    .mockResolvedValueOnce(
      response({
        system_state: 'READY',
        modules: [activeCoreAdmin(), runtimePolicy()],
      }),
    )
    .mockResolvedValueOnce(
      response({
        system_state: 'READY',
        plan: {
          valid: true,
          message: 'Configuration is valid',
          writes: false,
        },
      }),
    )

  render(
    <MemoryRouter>
      <Setup mode="admin" />
    </MemoryRouter>,
  )

  await user.click(
    await screen.findByRole('button', { name: /runtime policy/i }),
  )
  const interval = await screen.findByLabelText(
    /configuration poll interval seconds/i,
  )
  expect(interval).toBeEnabled()
  await user.clear(interval)
  await user.type(interval, '2')
  await user.click(
    screen.getByRole('button', { name: /^run dry-run$/i }),
  )

  expect(await screen.findByText(/dry-run checks passed/i)).toBeInTheDocument()
  expect(vi.mocked(executeConfig).mock.calls[1][0]).toMatchObject({
    action: 'plan',
    module: 'runtime_policy',
    config: { config_poll_interval_seconds: 2 },
  })
})
