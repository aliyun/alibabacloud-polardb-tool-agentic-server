import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, expect, it, vi } from 'vitest'

import { listAgents } from '../../api/agents'
import { listEnterpriseIdentitySources } from '../../api/enterpriseAccess'
import {
  createExternalApplication,
  getExternalApplicationContext,
  listExternalApplications,
  testExternalApplication,
  type ExternalApplicationContext,
  type ExternalApplicationSecret,
} from '../../api/externalApplications'
import ExternalApplications from './index'

vi.mock('../../api/agents', () => ({
  listAgents: vi.fn(),
}))

vi.mock('../../api/enterpriseAccess', async () => {
  const actual = await vi.importActual<
    typeof import('../../api/enterpriseAccess')
  >('../../api/enterpriseAccess')
  return {
    ...actual,
    listEnterpriseIdentitySources: vi.fn(),
  }
})

vi.mock('../../api/externalApplications', async () => {
  const actual = await vi.importActual<
    typeof import('../../api/externalApplications')
  >('../../api/externalApplications')
  return {
    ...actual,
    createExternalApplication: vi.fn(),
    getExternalApplicationContext: vi.fn(),
    listExternalApplications: vi.fn(),
    rotateExternalApplicationSecret: vi.fn(),
    testExternalApplication: vi.fn(),
    updateExternalApplicationStatus: vi.fn(),
  }
})

const enabledContext: ExternalApplicationContext = {
  provider_enabled: true,
  provider_type: 'oauth2_introspection',
  token_endpoint: 'https://pas.example.com/token',
  compatibility_token_endpoint:
    'https://pas.example.com/api/v1/external-auth/token',
  resources: [
    {
      target: 'mcp',
      resource: 'https://pas.example.com/mcp',
      scope: 'mcp',
    },
    {
      target: 'api',
      resource: 'https://pas.example.com/api/v1',
      scope: 'polarrag',
    },
  ],
}

const createdApplication: ExternalApplicationSecret = {
  client_id: 'pas_external_demo',
  client_secret: 'one-time-secret',
  name: 'Knowledge assistant',
  provider_type: 'oauth2_introspection',
  targets: ['mcp'],
  agent_policy: 'workspace_default',
  fixed_agent_id: null,
  status: 'active',
  secret_expires_at: null,
  secret_created_at: '2026-09-04T01:00:00Z',
  last_used_at: null,
  created_at: '2026-09-04T01:00:00Z',
  updated_at: null,
  token_endpoint: 'https://pas.example.com/token',
  compatibility_token_endpoint:
    'https://pas.example.com/api/v1/external-auth/token',
  resources: [enabledContext.resources[0]],
}

const feishuApplication: ExternalApplicationSecret = {
  ...createdApplication,
  client_id: 'pas_external_feishu',
  name: 'Feishu assistant',
  provider_type: 'feishu',
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listAgents).mockResolvedValue({
    data: { items: [], total: 0, offset: 0, limit: 100 },
  } as never)
  vi.mocked(listExternalApplications).mockResolvedValue({
    data: { items: [], total: 0, offset: 0, limit: 20 },
  } as never)
  vi.mocked(createExternalApplication).mockResolvedValue({
    data: createdApplication,
  } as never)
  vi.mocked(listEnterpriseIdentitySources).mockResolvedValue({
    data: {
      items: [{
        id: 'source-1',
        name: 'Verified Feishu tenant',
        provider: 'feishu',
        status: 'active',
        last_synced_at: null,
      }],
    },
  } as never)
  vi.mocked(testExternalApplication).mockResolvedValue({
    data: { authenticated: true, expires_in: 300, scope: 'mcp' },
  } as never)
})

function renderPage() {
  return render(
    <MemoryRouter>
      <ExternalApplications />
      <LocationProbe />
    </MemoryRouter>,
  )
}

function LocationProbe() {
  const location = useLocation()
  return (
    <output data-testid="location">
      {`${location.pathname}${location.search}`}
    </output>
  )
}

it('guides the administrator to configure external token validation', async () => {
  vi.mocked(getExternalApplicationContext).mockResolvedValue({
    data: {
      ...enabledContext,
      provider_enabled: false,
      provider_type: 'none',
    },
  } as never)

  const user = userEvent.setup()
  renderPage()

  expect(
    await screen.findByText('External Token validation is not active'),
  ).toBeInTheDocument()
  const configureButton = screen.getByRole('button', {
    name: 'Configure external Token validation',
  })
  await user.click(configureButton)
  expect(screen.getByTestId('location')).toHaveTextContent(
    '/settings/configuration?module=user_sso'
    + '&section=external-token-trust',
  )
  for (const button of screen.getAllByRole('button', {
    name: 'Register application',
  })) {
    expect(button).toBeDisabled()
  }
})

it('returns all integration values after registering an application', async () => {
  vi.mocked(getExternalApplicationContext).mockResolvedValue({
    data: enabledContext,
  } as never)
  const user = userEvent.setup()

  renderPage()

  await screen.findByText('oauth2_introspection Provider is active')
  await user.click(
    screen.getAllByRole('button', {
      name: 'Register application',
    })[0],
  )
  await user.type(
    screen.getByRole('textbox', { name: 'Application name' }),
    'Knowledge assistant',
  )
  await user.click(screen.getByRole('button', { name: 'OK' }))

  await waitFor(() => {
    expect(createExternalApplication).toHaveBeenCalledWith({
      name: 'Knowledge assistant',
      targets: ['mcp'],
      agent_policy: 'workspace_default',
      fixed_agent_id: null,
      secret_expires_at: null,
    })
  })
  expect(await screen.findByText('one-time-secret')).toBeInTheDocument()
  expect(screen.getByText('pas_external_demo')).toBeInTheDocument()
  expect(
    screen.getByText(/grant_type=urn:ietf:params:oauth:grant-type:token-exchange/),
  ).toBeInTheDocument()
  expect(
    screen.getByText(/resource=https:\/\/pas\.example\.com\/mcp/),
  ).toBeInTheDocument()
})

it('submits Feishu identity context from the application test dialog', async () => {
  vi.mocked(getExternalApplicationContext).mockResolvedValue({
    data: { ...enabledContext, provider_type: 'feishu' },
  } as never)
  vi.mocked(listExternalApplications).mockResolvedValue({
    data: {
      items: [feishuApplication],
      total: 1,
      offset: 0,
      limit: 20,
    },
  } as never)
  const user = userEvent.setup()

  renderPage()

  await screen.findByText('feishu Provider is active')
  await user.click(screen.getByRole('button', { name: /Test$/ }))
  await user.type(
    screen.getByLabelText('External subject token'),
    'feishu-user-token',
  )
  await user.click(screen.getByRole('combobox', {
    name: 'Verified Feishu identity source',
  }))
  await user.click(await screen.findByText(
    'Verified Feishu tenant (source-1)',
  ))
  await user.type(screen.getByLabelText('Feishu user ID'), 'ou_123')
  await user.type(screen.getByLabelText('Feishu union ID'), 'on_456')
  await user.click(within(screen.getByRole('dialog', {
    name: 'Test external assertion',
  })).getByRole('button', { name: 'OK' }))

  await waitFor(() => {
    expect(testExternalApplication).toHaveBeenCalledWith(
      'pas_external_feishu',
      {
        subject_token: 'feishu-user-token',
        resource: 'https://pas.example.com/mcp',
        agent_id: null,
        identity_source_id: 'source-1',
        feishu_user_id: 'ou_123',
        feishu_union_id: 'on_456',
      },
    )
  })
})
