import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
} from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ConfigModule } from '../../api/configuration'
import { listEnterpriseIdentitySources } from '../../api/enterpriseAccess'
import UserSSOForm from './index'

vi.mock('../../api/enterpriseAccess', async () => {
  const actual = await vi.importActual<
    typeof import('../../api/enterpriseAccess')
  >('../../api/enterpriseAccess')
  return {
    ...actual,
    listEnterpriseIdentitySources: vi.fn(),
  }
})

const module: ConfigModule = {
  name: 'user_sso',
  revision: 1,
  workflow_state: 'VALIDATED',
  draft: {
    discovery_url: 'https://idp.example.com/.well-known/openid-configuration',
    client_id: 'client-id',
    client_secret: { configured: true },
    external_token_trust: {
      enabled: true,
      provider: 'feishu',
    },
  },
  effective: null,
  dependencies: ['token_security'],
  dependents: [],
  ui_hints: { secret_fields: ['client_secret'] },
  schema: { type: 'object', properties: {} },
}

const identitySourceModule: ConfigModule = {
  ...module,
  draft: {
    ...module.draft,
    external_token_trust: {
      enabled: true,
      provider: 'oauth2_introspection',
      introspection_endpoint: 'https://provider.example.com/introspect',
      userinfo_endpoint: 'https://provider.example.com/user_info',
    },
  },
}

function LocationProbe() {
  const location = useLocation()
  return <output>{`${location.pathname}${location.search}`}</output>
}

function renderForm(
  configModule: ConfigModule = module,
  initialSection?: string,
  onSubmit = vi.fn(),
) {
  return render(
    <MemoryRouter initialEntries={['/settings/configuration']}>
      <Routes>
        <Route
          path="/settings/configuration"
          element={(
            <UserSSOForm
              module={configModule}
              initialSection={initialSection}
              onSubmit={onSubmit}
            />
          )}
        />
        <Route path="/users" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  )
}

async function openIdentitySourceSelector(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByText('External access token trust'))
  const selector = await screen.findByRole('combobox', {
    name: 'Verified identity source',
  })
  await user.click(selector)
  return selector
}

describe('User SSO identity source selector', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('opens external Token trust when linked from external applications', () => {
    renderForm(module, 'external-token-trust')

    expect(screen.getByRole('switch', {
      name: 'Enable browser SSO login',
    })).toBeChecked()
    expect(screen.getByRole('switch', {
      name: 'Accept trusted external access tokens',
    })).toBeChecked()
    expect(screen.getByText('Identity provider callback URL')).toBeVisible()
    expect(screen.getByRole('textbox', { name: 'Client ID' })).toHaveValue(
      'client-id',
    )
  })

  it('uses request identity context for Feishu Token Exchange', () => {
    renderForm(module, 'external-token-trust')

    expect(screen.getByText(
      /identity source ID, user ID, and union ID in each token exchange request/i,
    )).toBeVisible()
    expect(screen.queryByRole('combobox', {
      name: 'Verified identity source',
    })).not.toBeInTheDocument()
  })

  it('disables direct MCP for Feishu without a static identity source', () => {
    renderForm(module, 'external-token-trust')

    expect(screen.getByRole('switch', {
      name: 'Allow external Bearer Token directly at /mcp',
    })).toBeDisabled()
    expect(screen.getByText(
      /direct MCP requires a static verified identity source/i,
    )).toBeVisible()
  })

  it('honors disabling Feishu external token trust', async () => {
    const onSubmit = vi.fn()
    const user = userEvent.setup()
    renderForm(module, 'external-token-trust', onSubmit)

    await user.click(screen.getByRole('switch', {
      name: 'Accept trusted external access tokens',
    }))
    fireEvent.submit(screen.getByRole('form', {
      name: 'User single sign-on configuration',
    }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    expect(onSubmit.mock.calls[0][0].external_token_trust).toMatchObject({
      enabled: false,
    })
  })

  it('round-trips static Feishu direct MCP configuration', async () => {
    const onSubmit = vi.fn()
    const staticFeishuModule: ConfigModule = {
      ...module,
      draft: {
        ...module.draft,
        browser_login_enabled: false,
        external_token_trust: {
          enabled: true,
          provider: 'feishu',
          identity_source_id: 'source-1',
          direct_mcp_enabled: true,
          access_token_ttl_seconds: 900,
        },
      },
    }
    renderForm(staticFeishuModule, 'external-token-trust', onSubmit)

    expect(screen.getByRole('switch', {
      name: 'Allow external Bearer Token directly at /mcp',
    })).toBeChecked()
    fireEvent.submit(screen.getByRole('form', {
      name: 'User single sign-on configuration',
    }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    expect(onSubmit.mock.calls[0][0].external_token_trust).toMatchObject({
      enabled: true,
      provider: 'feishu',
      identity_source_id: 'source-1',
      direct_mcp_enabled: true,
      access_token_ttl_seconds: 900,
    })
  })

  it('offers source management and refresh actions when no active source exists', async () => {
    vi.mocked(listEnterpriseIdentitySources).mockResolvedValue({
      data: { items: [] },
    } as never)
    const user = userEvent.setup()
    renderForm(identitySourceModule)

    const selector = await openIdentitySourceSelector(user)

    expect(
      await screen.findAllByText(/create and verify an active identity source/i),
    ).toHaveLength(2)
    await user.click(screen.getByRole('button', { name: /refresh/i }))

    await waitFor(() => {
      expect(listEnterpriseIdentitySources).toHaveBeenCalledTimes(2)
    })

    if (selector.getAttribute('aria-expanded') !== 'true') {
      await user.click(selector)
    }
    await user.click(
      screen.getByRole('button', { name: /create or verify a source/i }),
    )
    expect(
      await screen.findByText('/users?tab=identity-sources'),
    ).toBeInTheDocument()
  })

  it('distinguishes a loading failure from an empty result and allows retry', async () => {
    vi.mocked(listEnterpriseIdentitySources)
      .mockRejectedValueOnce(new Error('network error'))
      .mockResolvedValueOnce({ data: { items: [] } } as never)
    const user = userEvent.setup()
    renderForm(identitySourceModule)

    await openIdentitySourceSelector(user)

    expect(
      await screen.findAllByText(/could not load verified identity sources/i),
    ).toHaveLength(2)
    await user.click(screen.getByRole('button', { name: /refresh/i }))

    await waitFor(() => {
      expect(
        screen.getAllByText(/create and verify an active identity source/i),
      ).toHaveLength(2)
    })
  })

  it('shows external UserInfo mapping and provider credentials', async () => {
    vi.mocked(listEnterpriseIdentitySources).mockResolvedValue({
      data: {
        items: [{
          id: 'source-1',
          name: 'External directory',
          provider: 'sharepoint',
          status: 'active',
        }, {
          id: 'source-stale',
          name: 'Stale directory',
          provider: 'feishu',
          status: 'stale',
        }],
      },
    } as never)
    const introspectionModule: ConfigModule = {
      ...module,
      draft: {
        ...module.draft,
        external_token_trust: {
          enabled: true,
          provider: 'oauth2_introspection',
          introspection_endpoint:
            'https://provider.example.com/introspect',
          userinfo_endpoint: 'https://provider.example.com/user_info',
          client_id: 'pas-to-provider',
          client_secret: { configured: true },
        },
      },
      ui_hints: {
        secret_fields: [
          'client_secret',
          'external_token_trust.client_secret',
        ],
      },
    }
    const user = userEvent.setup()
    renderForm(introspectionModule)

    await user.click(screen.getByText('External access token trust'))

    expect(
      screen.getByRole('textbox', {
        name: /External UserInfo endpoint/i,
      }),
    ).toHaveValue('https://provider.example.com/user_info')
    expect(
      screen.getByRole('textbox', {
        name: /PAS-to-provider client ID/i,
      }),
    ).toHaveValue('pas-to-provider')
    expect(
      await screen.findByRole('combobox', {
        name: 'Verified identity source',
      }),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('combobox', {
      name: 'Verified identity source',
    }))
    expect(
      await screen.findByRole('option', { name: 'External directory' }),
    ).toBeVisible()
    expect(
      screen.queryByRole('option', { name: 'Stale directory' }),
    ).not.toBeInTheDocument()
  })

  it('warns when external validation endpoints use HTTP', async () => {
    vi.mocked(listEnterpriseIdentitySources).mockResolvedValue({
      data: { items: [] },
    } as never)
    const introspectionModule: ConfigModule = {
      ...module,
      draft: {
        ...module.draft,
        external_token_trust: {
          enabled: true,
          provider: 'oauth2_introspection',
          introspection_endpoint:
            'http://winner.internal/oauth2/introspect',
          userinfo_endpoint:
            'http://winner.internal/oauth2/user_info',
        },
      },
    }
    const user = userEvent.setup()
    renderForm(introspectionModule)

    await user.click(screen.getByText('External access token trust'))

    expect(
      screen.getByText('These external endpoints use HTTP'),
    ).toBeVisible()
    expect(
      screen.getByText(/trusted, isolated private network/i),
    ).toBeVisible()
  })

  it('reveals the identity source after entering external UserInfo', async () => {
    vi.mocked(listEnterpriseIdentitySources).mockResolvedValue({
      data: { items: [] },
    } as never)
    const introspectionModule: ConfigModule = {
      ...module,
      draft: {
        ...module.draft,
        external_token_trust: {
          enabled: true,
          provider: 'oauth2_introspection',
          introspection_endpoint:
            'https://provider.example.com/introspect',
        },
      },
    }
    const user = userEvent.setup()
    renderForm(introspectionModule)

    await user.click(screen.getByText('External access token trust'))
    expect(
      screen.queryByRole('combobox', {
        name: 'Verified identity source',
      }),
    ).not.toBeInTheDocument()

    await user.type(
      screen.getByRole('textbox', {
        name: /External UserInfo endpoint/i,
      }),
      'https://provider.example.com/user_info',
    )

    expect(
      await screen.findByRole('combobox', {
        name: 'Verified identity source',
      }),
    ).toBeInTheDocument()
  })
})
