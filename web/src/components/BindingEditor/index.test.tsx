import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StrictMode, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import type { InstanceCredential } from '../../api/credentials'
import type {
  AgentInstanceAccessInput,
  DirectBindingInput,
  InstanceSummary,
} from '../../api/instanceAccess'
import type { ProvisioningBackend } from '../../api/provisioningBackends'
import BindingEditor from './index'

const instance: InstanceSummary = {
  id: 'instance-1',
  cluster_id: 'pc-instance-1',
  name: 'Production',
  usage: null,
  engine: 'polardb_mysql',
  topology: 'single_tenant',
  allocation_mode: 'registered',
  region: 'cn-hangzhou',
  host: null,
  port: null,
  status: 'active',
  owner_user_id: null,
  health: null,
  binding_counts: { users: 0, departments: 0, agents: 0 },
}

const credentials: InstanceCredential[] = [
  {
    id: 'readonly',
    instance_id: instance.id,
    resource_id: null,
    name: 'Read only',
    purpose: 'direct_access',
    capability: 'readonly',
    database_name: null,
    status: 'active',
    version: 1,
    created_by_user_id: 'admin',
    created_at: '2026-07-26T00:00:00Z',
    updated_at: null,
  },
  {
    id: 'readwrite',
    instance_id: instance.id,
    resource_id: null,
    name: 'Read write',
    purpose: 'direct_access',
    capability: 'readwrite',
    database_name: null,
    status: 'active',
    version: 1,
    created_by_user_id: 'admin',
    created_at: '2026-07-26T00:00:00Z',
    updated_at: null,
  },
  {
    id: 'revoked',
    instance_id: instance.id,
    resource_id: null,
    name: 'Revoked',
    purpose: 'direct_access',
    capability: 'readwrite',
    database_name: null,
    status: 'revoked',
    version: 1,
    created_by_user_id: 'admin',
    created_at: '2026-07-26T00:00:00Z',
    updated_at: null,
  },
]

function binding(
  credentialId: string,
  permission: DirectBindingInput['permission'] = 'readwrite',
): DirectBindingInput {
  return {
    instance_id: instance.id,
    credential_id: credentialId,
    permission,
    capabilities: ['db_instance:list'],
    enabled: true,
  }
}

describe('BindingEditor', () => {
  it('notifies an inline parent callback once per validity state in StrictMode', async () => {
    const user = userEvent.setup()
    const notifications = vi.fn()

    function Harness() {
      const [draft, setDraft] = useState(binding('readwrite'))
      const [parentValidity, setParentValidity] = useState<boolean | null>(null)
      return (
        <>
          <button
            type="button"
            onClick={() =>
              setDraft((current) => ({
                ...current,
                credential_id:
                  current.credential_id === 'readwrite'
                    ? 'missing'
                    : 'readwrite',
              }))
            }
          >
            Toggle validity
          </button>
          <span>Parent validity: {String(parentValidity)}</span>
          <BindingEditor
            value={draft}
            onChange={setDraft}
            onValidityChange={(next) => {
              notifications(next)
              setParentValidity(next)
            }}
            instances={[instance]}
            credentials={credentials}
          />
        </>
      )
    }

    render(
      <StrictMode>
        <Harness />
      </StrictMode>,
    )

    expect(
      await screen.findByText('Parent validity: true'),
    ).toBeInTheDocument()
    expect(notifications.mock.calls).toEqual([[true]])

    await user.click(screen.getByRole('button', { name: /toggle validity/i }))
    expect(
      await screen.findByText('Parent validity: false'),
    ).toBeInTheDocument()
    expect(notifications.mock.calls).toEqual([[true], [false]])

    await user.click(screen.getByRole('button', { name: /toggle validity/i }))
    expect(
      await screen.findByText('Parent validity: true'),
    ).toBeInTheDocument()
    expect(notifications.mock.calls).toEqual([[true], [false], [true]])
  })

  it('uses a replacement callback for the next validity transition', async () => {
    const user = userEvent.setup()
    const firstCallback = vi.fn()
    const replacementCallback = vi.fn()

    function Harness() {
      const [draft, setDraft] = useState(binding('readwrite'))
      const [useReplacement, setUseReplacement] = useState(false)
      return (
        <>
          <button type="button" onClick={() => setUseReplacement(true)}>
            Replace callback
          </button>
          <button
            type="button"
            onClick={() =>
              setDraft((current) => ({
                ...current,
                credential_id: 'missing',
              }))
            }
          >
            Invalidate binding
          </button>
          <BindingEditor
            value={draft}
            onChange={setDraft}
            onValidityChange={(next) => {
              if (useReplacement) {
                replacementCallback(next)
              } else {
                firstCallback(next)
              }
            }}
            instances={[instance]}
            credentials={credentials}
          />
        </>
      )
    }

    render(<Harness />)
    await waitFor(() => expect(firstCallback).toHaveBeenCalledWith(true))

    await user.click(screen.getByRole('button', { name: /replace callback/i }))
    expect(replacementCallback).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: /invalidate binding/i }))
    await waitFor(() =>
      expect(replacementCallback).toHaveBeenCalledTimes(1),
    )
    expect(replacementCallback).toHaveBeenCalledWith(false)
    expect(firstCallback).toHaveBeenCalledTimes(1)
  })

  it.each([
    ['readonly', /permission exceeds.*credential capability/i],
    ['revoked', /select an active direct-access credential/i],
    ['missing', /select an active direct-access credential/i],
  ])(
    'marks a stale %s credential/permission draft invalid',
    async (credentialId, expectedMessage) => {
      const onValidityChange = vi.fn()
      render(
        <BindingEditor
          value={binding(credentialId)}
          onChange={vi.fn()}
          onValidityChange={onValidityChange}
          instances={[instance]}
          credentials={credentials}
        />,
      )

      expect(screen.getByRole('alert')).toHaveTextContent(expectedMessage)
      await waitFor(() =>
        expect(onValidityChange).toHaveBeenLastCalledWith(false),
      )
    },
  )

  it('reports a compatible draft as valid', async () => {
    const onValidityChange = vi.fn()
    render(
      <BindingEditor
        value={binding('readwrite')}
        onChange={vi.fn()}
        onValidityChange={onValidityChange}
        instances={[instance]}
        credentials={credentials}
      />,
    )

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await waitFor(() =>
      expect(onValidityChange).toHaveBeenLastCalledWith(true),
    )
  })

  it('locks the instance selector in edit mode', () => {
    render(
      <BindingEditor
        mode="edit"
        value={binding('readwrite')}
        onChange={vi.fn()}
        onValidityChange={vi.fn()}
        instances={[instance]}
        credentials={credentials}
      />,
    )

    expect(
      screen.getByRole('combobox', { name: /database instance/i }),
    ).toBeDisabled()
  })

  it('recomputes enabled SQL proxy capabilities when permission changes', async () => {
    const user = userEvent.setup()
    let latest: DirectBindingInput = {
      ...binding('readwrite'),
      capabilities: [
        'db_instance:list',
        'sql:read',
        'sql:write',
      ],
    }

    function Harness() {
      const [draft, setDraft] = useState(latest)
      return (
        <BindingEditor
          value={draft}
          onChange={(next) => {
            latest = next
            setDraft(next)
          }}
          onValidityChange={vi.fn()}
          instances={[instance]}
          credentials={credentials}
        />
      )
    }

    render(<Harness />)
    const permission = screen.getByRole('combobox', {
      name: /binding permission/i,
    })

    await user.click(permission)
    await user.click(await screen.findByText('Read only'))
    expect(latest.capabilities).toEqual([
      'db_instance:list',
      'sql:read',
    ])

    await user.click(permission)
    await user.click(await screen.findByText('Read and write'))
    expect(latest.capabilities).toEqual([
      'db_instance:list',
      'sql:read',
      'sql:write',
    ])
  })

  it('accepts provisioning-only access without a direct credential', async () => {
    const multitenant = {
      ...instance,
      id: 'multitenant-1',
      topology: 'multitenant' as const,
    }
    const backend = {
      id: 'backend-1',
      instance_id: multitenant.id,
      status: 'active',
      healthy: true,
      health_checked_at: '2026-07-27T00:00:00Z',
      available_for_create: true,
    } as ProvisioningBackend
    let latest: AgentInstanceAccessInput = {
      instance_id: multitenant.id,
      credential_id: null,
      permission: null,
      direct_enabled: null,
      capabilities: [],
    }
    const validity = vi.fn()

    function Harness() {
      const [draft, setDraft] = useState(latest)
      return (
        <BindingEditor
          value={draft}
          onChange={(next) => {
            latest = next
            setDraft(next)
          }}
          onValidityChange={validity}
          instances={[multitenant]}
          credentials={[]}
          provisioningBackends={[backend]}
        />
      )
    }

    const user = userEvent.setup()
    render(<Harness />)
    await user.click(
      screen.getByRole('checkbox', {
        name: /create managed databases/i,
      }),
    )

    expect(latest).toEqual({
      instance_id: multitenant.id,
      credential_id: null,
      permission: null,
      direct_enabled: null,
      capabilities: ['db_instance:create'],
    })
    await waitFor(() =>
      expect(validity).toHaveBeenLastCalledWith(true),
    )
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('enables SQL proxy by default when aggregate direct access first selects a credential', async () => {
    let latest: AgentInstanceAccessInput = {
      instance_id: instance.id,
      credential_id: null,
      permission: null,
      direct_enabled: null,
      capabilities: ['db_instance:list'],
    }

    function Harness() {
      const [draft, setDraft] = useState(latest)
      return (
        <BindingEditor
          value={draft}
          onChange={(next) => {
            latest = next
            setDraft(next)
          }}
          onValidityChange={vi.fn()}
          instances={[instance]}
          credentials={credentials}
        />
      )
    }

    const user = userEvent.setup()
    render(<Harness />)
    await user.click(
      screen.getByRole('combobox', {
        name: /direct access credential/i,
      }),
    )
    await user.click(await screen.findByText(/Read only.*readonly/i))

    expect(latest).toEqual({
      instance_id: instance.id,
      credential_id: 'readonly',
      permission: 'readonly',
      direct_enabled: true,
      capabilities: ['db_instance:list', 'sql:read'],
    })
    expect(
      screen.getByRole('checkbox', {
        name: /enable sql over http proxy/i,
      }),
    ).toBeChecked()
  })

  it('allows an existing provisioning-only binding to disable new creation', async () => {
    const multitenant = {
      ...instance,
      id: 'multitenant-1',
      topology: 'multitenant' as const,
    }
    const backend = {
      id: 'backend-1',
      instance_id: multitenant.id,
      status: 'active',
      healthy: true,
      health_checked_at: '2026-07-27T00:00:00Z',
      available_for_create: true,
    } as ProvisioningBackend
    const validity = vi.fn()

    function Harness() {
      const [draft, setDraft] = useState<AgentInstanceAccessInput>({
        instance_id: multitenant.id,
        credential_id: null,
        permission: null,
        direct_enabled: null,
        capabilities: ['db_instance:create'],
      })
      return (
        <BindingEditor
          mode="edit"
          value={draft}
          onChange={setDraft}
          onValidityChange={validity}
          instances={[multitenant]}
          credentials={[]}
          provisioningBackends={[backend]}
        />
      )
    }

    const user = userEvent.setup()
    render(<Harness />)
    await user.click(
      screen.getByRole('checkbox', {
        name: /create managed databases/i,
      }),
    )

    await waitFor(() =>
      expect(validity).toHaveBeenLastCalledWith(true),
    )
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
