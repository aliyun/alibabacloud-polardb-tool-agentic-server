import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  applyAgentEnterpriseAccess,
  listEnterpriseIdentitySourceDirectory,
  listEnterpriseIdentitySources,
  listEnterpriseIdentitySourceSpaces,
  previewAgentEnterpriseAccess,
} from '../../api/enterpriseAccess'
import EnterpriseAccessDrawer from './EnterpriseAccessDrawer'

vi.mock('../../api/enterpriseAccess', () => ({
  applyAgentEnterpriseAccess: vi.fn(),
  listEnterpriseIdentitySourceDirectory: vi.fn(),
  listEnterpriseIdentitySources: vi.fn(),
  listEnterpriseIdentitySourceSpaces: vi.fn(),
  previewAgentEnterpriseAccess: vi.fn(),
}))

const bindings = [
  {
    id: 'binding-1',
    polarrag_instance_id: 'rag-1',
    instance_name: 'Primary RAG',
    public_knowledge_resource_ids: ['resource-1'],
    created_at: '2026-08-24T00:00:00Z',
  },
]

const initialPreview = {
  selection: {
    identity_source_id: 'source-1',
    all_synced_users: false,
    directory_group_ids: ['directory-group-1'],
    pas_user_ids: ['pas-user-1'],
    knowledge_space_ids: ['space-1'],
  },
  creates: [
    {
      relation_type: 'agent_identity_source_group',
      relation_id: null,
      display_name: 'Engineering',
      scope: 'agent',
    },
    {
      relation_type: 'agent_user',
      relation_id: null,
      display_name: 'Alice',
      scope: 'agent',
    },
  ],
  global_changes: [
    {
      relation_type: 'identity_source_space_binding',
      relation_id: null,
      display_name: 'Bound Space',
      scope: 'global',
    },
  ],
  reuses: [
    {
      relation_type: 'identity_source_space_binding',
      relation_id: 'space-binding-2',
      display_name: 'Existing Space',
      scope: 'global',
    },
  ],
  preview_hash: 'a'.repeat(64),
}

async function selectSource(user: ReturnType<typeof userEvent.setup>) {
  await user.click(
    await screen.findByRole('combobox', {
      name: 'Enterprise identity source',
    }),
  )
  await user.click(await screen.findByText('SharePoint directory'))
}

async function selectSourceByName(
  user: ReturnType<typeof userEvent.setup>,
  name: string,
) {
  await user.click(
    await screen.findByRole('combobox', {
      name: 'Enterprise identity source',
    }),
  )
  await user.click(await screen.findByText(name))
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

describe('Agent enterprise access drawer', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(listEnterpriseIdentitySources).mockResolvedValue({
      data: {
        items: [
          {
            id: 'source-1',
            name: 'SharePoint directory',
            provider: 'sharepoint',
            status: 'active',
            last_synced_at: '2026-08-24T00:00:00Z',
          },
          {
            id: 'source-disabled',
            name: 'Disabled directory',
            provider: 'feishu',
            status: 'disabled',
            last_synced_at: null,
          },
          {
            id: 'source-a',
            name: 'Directory A',
            provider: 'sharepoint',
            status: 'active',
            last_synced_at: '2026-08-24T00:00:00Z',
          },
          {
            id: 'source-b',
            name: 'Directory B',
            provider: 'feishu',
            status: 'active',
            last_synced_at: '2026-08-24T00:00:00Z',
          },
        ],
      },
    } as never)
    vi.mocked(listEnterpriseIdentitySourceDirectory).mockImplementation(
      async (_sourceId, entryType) =>
        (entryType === 'groups'
          ? {
              data: {
                users: [],
                groups: [
                  {
                    id: 'directory-group-1',
                    external_group_id: 'external-group-1',
                    display_name: 'Engineering',
                    principal_type: 'group',
                    status: 'active',
                  },
                ],
                total: 1,
              },
            }
          : {
              data: {
                users: [
                  {
                    id: 'directory-user-1',
                    pas_user_id: 'pas-user-1',
                    external_user_id: 'external-user-1',
                    display_name: 'Alice',
                    email: 'alice@example.test',
                    status: 'active',
                  },
                ],
                groups: [],
                total: 1,
              },
            }) as never,
    )
    vi.mocked(listEnterpriseIdentitySourceSpaces).mockResolvedValue({
      data: {
        items: [
          {
            knowledge_space_id: 'space-1',
            polarrag_instance_id: 'rag-1',
            name: 'Bound Space',
            identity_domain: 'domain-1',
          },
          {
            knowledge_space_id: 'space-2',
            polarrag_instance_id: 'rag-2',
            name: 'Unbound Space',
            identity_domain: 'domain-2',
          },
        ],
      },
    } as never)
    vi.mocked(previewAgentEnterpriseAccess).mockResolvedValue({
      data: initialPreview,
    } as never)
    vi.mocked(applyAgentEnterpriseAccess).mockResolvedValue({
      data: initialPreview,
    } as never)
  })

  it('keeps all synchronized users first, explicit, and reset on reopen', async () => {
    const user = userEvent.setup()
    const { rerender } = render(
      <EnterpriseAccessDrawer
        agentId="agent-1"
        bindings={bindings}
        open
        onClose={vi.fn()}
        onApplied={vi.fn()}
      />,
    )
    await selectSource(user)

    const allUsers = await screen.findByRole('checkbox', {
      name: 'All synchronized users',
    })
    const groups = screen.getByRole('combobox', { name: 'Enterprise groups' })
    expect(allUsers).not.toBeChecked()
    expect(
      screen.getByRole('button', { name: 'Preview changes' }),
    ).toBeDisabled()
    expect(
      allUsers.compareDocumentPosition(groups) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()

    await user.click(allUsers)
    expect(
      screen.queryByRole('combobox', { name: 'Enterprise groups' }),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole('combobox', { name: 'Synchronized PAS users' }),
    ).not.toBeInTheDocument()
    rerender(
      <EnterpriseAccessDrawer
        agentId="agent-1"
        bindings={bindings}
        open={false}
        onClose={vi.fn()}
        onApplied={vi.fn()}
      />,
    )
    rerender(
      <EnterpriseAccessDrawer
        agentId="agent-1"
        bindings={bindings}
        open
        onClose={vi.fn()}
        onApplied={vi.fn()}
      />,
    )

    await selectSource(user)
    expect(
      await screen.findByRole('checkbox', {
        name: 'All synchronized users',
      }),
    ).not.toBeChecked()
  })

  it('shows only Spaces on instances already bound to the Agent', async () => {
    const user = userEvent.setup()
    render(
      <EnterpriseAccessDrawer
        agentId="agent-1"
        bindings={bindings}
        open
        onClose={vi.fn()}
        onApplied={vi.fn()}
      />,
    )
    await selectSource(user)

    await user.click(screen.getByRole('combobox', { name: 'PolarRAG Spaces' }))

    expect(await screen.findByText('Bound Space')).toBeInTheDocument()
    expect(screen.queryByText('Unbound Space')).not.toBeInTheDocument()
  })

  it('selects and clears all eligible Spaces from the dropdown', async () => {
    const user = userEvent.setup()
    render(
      <EnterpriseAccessDrawer
        agentId="agent-1"
        bindings={bindings}
        open
        onClose={vi.fn()}
        onApplied={vi.fn()}
      />,
    )
    await selectSource(user)

    await user.click(
      screen.getByRole('combobox', { name: 'Enterprise groups' }),
    )
    await user.click(await screen.findByText('Engineering'))
    await user.click(screen.getByRole('combobox', { name: 'PolarRAG Spaces' }))
    await user.click(await screen.findByRole('button', { name: 'Select all' }))

    expect(
      screen.getByRole('button', { name: 'Preview changes' }),
    ).toBeEnabled()
    expect(
      screen.getByRole('button', { name: 'Clear selection' }),
    ).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Clear selection' }))

    expect(
      screen.getByRole('button', { name: 'Preview changes' }),
    ).toBeDisabled()
  })

  it('loads one bounded directory page by default and for a search', async () => {
    vi.mocked(listEnterpriseIdentitySourceDirectory).mockImplementation(
      async (_sourceId, entryType, options) => {
        if (entryType === 'users') {
          return {
            data: {
              users: [
                {
                  id: 'directory-user-1',
                  pas_user_id: 'pas-user-1',
                  external_user_id: 'external-user-1',
                  display_name: 'Alice',
                  email: 'alice@example.test',
                  status: 'active',
                },
              ],
              groups: [],
              total: 100_000,
            },
          } as never
        }
        return {
          data: {
            users: [],
            groups: [
              {
                id: options?.search ? 'directory-group-search' : 'directory-group-1',
                external_group_id: options?.search ? 'external-group-search' : 'external-group-1',
                display_name: options?.search ? 'Search result' : 'Engineering',
                principal_type: 'group',
                status: 'active',
              },
            ],
            total: 100_000,
          },
        } as never
      },
    )
    const user = userEvent.setup()
    render(
      <EnterpriseAccessDrawer
        agentId="agent-1"
        bindings={bindings}
        open
        onClose={vi.fn()}
        onApplied={vi.fn()}
      />,
    )

    await selectSource(user)

    expect(listEnterpriseIdentitySourceDirectory).toHaveBeenCalledTimes(2)
    expect(listEnterpriseIdentitySourceDirectory).toHaveBeenCalledWith(
      'source-1',
      'groups',
      { offset: 0, limit: 100 },
    )

    const groupSelect = screen.getByRole('combobox', {
      name: 'Enterprise groups',
    })
    await user.click(groupSelect)
    await user.type(groupSelect, 'engineering')

    await waitFor(() =>
      expect(listEnterpriseIdentitySourceDirectory).toHaveBeenCalledWith(
        'source-1',
        'groups',
        { offset: 0, limit: 100, search: 'engineering' },
      ),
    )
    expect(
      vi
        .mocked(listEnterpriseIdentitySourceDirectory)
        .mock.calls.filter(([, entryType]) => entryType === 'groups'),
    ).toHaveLength(2)
  })

  it('keeps directory candidates from the most recently selected source', async () => {
    const sourceAGroups = deferred<unknown>()
    const sourceAUsers = deferred<unknown>()
    const sourceBGroups = deferred<unknown>()
    const sourceBUsers = deferred<unknown>()
    vi.mocked(listEnterpriseIdentitySourceDirectory).mockImplementation(
      (sourceId, entryType) => {
        const request =
          sourceId === 'source-a'
            ? entryType === 'groups'
              ? sourceAGroups
              : sourceAUsers
            : entryType === 'groups'
              ? sourceBGroups
              : sourceBUsers
        return request.promise as never
      },
    )
    const user = userEvent.setup()
    render(
      <EnterpriseAccessDrawer
        agentId="agent-1"
        bindings={bindings}
        open
        onClose={vi.fn()}
        onApplied={vi.fn()}
      />,
    )

    await selectSourceByName(user, 'Directory A')
    await selectSourceByName(user, 'Directory B')
    await act(async () => {
      sourceBGroups.resolve({
        data: {
          users: [],
          groups: [
            {
              id: 'group-b',
              external_group_id: 'group-b',
              display_name: 'Group B',
              principal_type: 'group',
              status: 'active',
            },
          ],
          total: 1,
        },
      })
      sourceBUsers.resolve({
        data: {
          users: [
            {
              id: 'user-b',
              pas_user_id: 'pas-user-b',
              external_user_id: 'user-b',
              display_name: 'User B',
              email: 'user-b@example.test',
              status: 'active',
            },
          ],
          groups: [],
          total: 1,
        },
      })
      await Promise.resolve()
    })

    await act(async () => {
      sourceAGroups.resolve({
        data: {
          users: [],
          groups: [
            {
              id: 'group-a',
              external_group_id: 'group-a',
              display_name: 'Group A',
              principal_type: 'group',
              status: 'active',
            },
          ],
          total: 1,
        },
      })
      sourceAUsers.resolve({
        data: {
          users: [
            {
              id: 'user-a',
              pas_user_id: 'pas-user-a',
              external_user_id: 'user-a',
              display_name: 'User A',
              email: 'user-a@example.test',
              status: 'active',
            },
          ],
          groups: [],
          total: 1,
        },
      })
      await Promise.resolve()
    })

    await user.click(screen.getByRole('combobox', { name: 'Enterprise groups' }))
    expect(await screen.findByText('Group B')).toBeInTheDocument()
    expect(screen.queryByText('Group A')).not.toBeInTheDocument()
    await user.click(screen.getByRole('combobox', { name: 'Synchronized PAS users' }))
    expect(await screen.findByText('User B')).toBeInTheDocument()
    expect(screen.queryByText('User A')).not.toBeInTheDocument()
  })

  it('invalidates directory requests when the drawer is closed', async () => {
    const groups = deferred<unknown>()
    const users = deferred<unknown>()
    vi.mocked(listEnterpriseIdentitySourceDirectory).mockImplementation(
      (_sourceId, entryType) =>
        (entryType === 'groups' ? groups.promise : users.promise) as never,
    )
    const user = userEvent.setup()
    const { rerender } = render(
      <EnterpriseAccessDrawer
        agentId="agent-1"
        bindings={bindings}
        open
        onClose={vi.fn()}
        onApplied={vi.fn()}
      />,
    )

    await selectSourceByName(user, 'Directory A')
    rerender(
      <EnterpriseAccessDrawer
        agentId="agent-1"
        bindings={bindings}
        open={false}
        onClose={vi.fn()}
        onApplied={vi.fn()}
      />,
    )
    rerender(
      <EnterpriseAccessDrawer
        agentId="agent-1"
        bindings={bindings}
        open
        onClose={vi.fn()}
        onApplied={vi.fn()}
      />,
    )
    await act(async () => {
      groups.resolve({
        data: {
          users: [],
          groups: [
            {
              id: 'group-a',
              external_group_id: 'group-a',
              display_name: 'Group A',
              principal_type: 'group',
              status: 'active',
            },
          ],
          total: 1,
        },
      })
      users.resolve({ data: { users: [], groups: [], total: 0 } })
      await Promise.resolve()
    })

    expect(screen.queryByText('Group A')).not.toBeInTheDocument()
  })

  it('deduplicates directory identities for the same PAS user', async () => {
    vi.mocked(listEnterpriseIdentitySourceDirectory).mockImplementation(
      async (_sourceId, entryType) =>
        (entryType === 'users'
          ? {
              data: {
                users: [
                  {
                    id: 'directory-user-1',
                    pas_user_id: 'pas-user-1',
                    external_user_id: 'external-user-1',
                    display_name: 'Alice',
                    email: 'alice@example.test',
                    status: 'active',
                  },
                  {
                    id: 'directory-user-2',
                    pas_user_id: 'pas-user-1',
                    external_user_id: 'external-user-2',
                    display_name: 'Alice duplicate identity',
                    email: 'alice@example.test',
                    status: 'active',
                  },
                ],
                groups: [],
                total: 2,
              },
            }
          : {
              data: {
                users: [],
                groups: [],
                total: 0,
              },
            }) as never,
    )
    const user = userEvent.setup()
    render(
      <EnterpriseAccessDrawer
        agentId="agent-1"
        bindings={bindings}
        open
        onClose={vi.fn()}
        onApplied={vi.fn()}
      />,
    )

    await selectSource(user)
    await user.click(
      screen.getByRole('combobox', { name: 'Synchronized PAS users' }),
    )
    await user.click(await screen.findByText('Alice'))
    expect(screen.queryByText('Alice duplicate identity')).not.toBeInTheDocument()
    await user.click(screen.getByRole('combobox', { name: 'PolarRAG Spaces' }))
    await user.click(await screen.findByText('Bound Space'))
    await user.click(screen.getByRole('button', { name: 'Preview changes' }))

    expect(previewAgentEnterpriseAccess).toHaveBeenCalledWith('agent-1', {
      identity_source_id: 'source-1',
      all_synced_users: false,
      directory_group_ids: [],
      pas_user_ids: ['pas-user-1'],
      knowledge_space_ids: ['space-1'],
    })
  })

  it('keeps a group selected while an older search result is pending', async () => {
    const searchGroups = deferred<unknown>()
    vi.mocked(listEnterpriseIdentitySourceDirectory).mockImplementation(
      async (_sourceId, entryType, options) => {
        if (entryType === 'users') {
          return { data: { users: [], groups: [], total: 0 } } as never
        }
        if (options?.search) return searchGroups.promise as never
        return {
          data: {
            users: [],
            groups: [
              {
                id: 'directory-group-1',
                external_group_id: 'external-group-1',
                display_name: 'Engineering',
                principal_type: 'group',
                status: 'active',
              },
            ],
            total: 1,
          },
        } as never
      },
    )
    const user = userEvent.setup()
    render(
      <EnterpriseAccessDrawer
        agentId="agent-1"
        bindings={bindings}
        open
        onClose={vi.fn()}
        onApplied={vi.fn()}
      />,
    )

    await selectSource(user)
    const groupSelect = screen.getByRole('combobox', {
      name: 'Enterprise groups',
    })
    await user.click(groupSelect)
    await user.type(groupSelect, 'platform')
    await waitFor(() =>
      expect(listEnterpriseIdentitySourceDirectory).toHaveBeenCalledWith(
        'source-1',
        'groups',
        { offset: 0, limit: 100, search: 'platform' },
      ),
    )
    await user.click(await screen.findByText('Engineering'))
    await act(async () => {
      searchGroups.resolve({
        data: {
          users: [],
          groups: [
            {
              id: 'directory-group-platform',
              external_group_id: 'external-group-platform',
              display_name: 'Platform',
              principal_type: 'group',
              status: 'active',
            },
          ],
          total: 1,
        },
      })
      await Promise.resolve()
    })

    expect(screen.getAllByText('Engineering')).not.toHaveLength(0)
    await user.click(screen.getByRole('combobox', { name: 'PolarRAG Spaces' }))
    await user.click(await screen.findByText('Bound Space'))
    await user.click(screen.getByRole('button', { name: 'Preview changes' }))
    expect(previewAgentEnterpriseAccess).toHaveBeenCalledWith('agent-1', {
      identity_source_id: 'source-1',
      all_synced_users: false,
      directory_group_ids: ['directory-group-1'],
      pas_user_ids: [],
      knowledge_space_ids: ['space-1'],
    })
  })

  it('previews trusted row and PAS user IDs and separates impact scopes', async () => {
    const user = userEvent.setup()
    render(
      <EnterpriseAccessDrawer
        agentId="agent-1"
        bindings={bindings}
        open
        onClose={vi.fn()}
        onApplied={vi.fn()}
      />,
    )
    await selectSource(user)

    await user.click(screen.getByRole('combobox', { name: 'Enterprise groups' }))
    await user.click(await screen.findByText('Engineering'))
    await user.click(screen.getByRole('combobox', { name: 'Synchronized PAS users' }))
    await user.click(await screen.findByText('Alice'))
    await user.click(screen.getByRole('combobox', { name: 'PolarRAG Spaces' }))
    await user.click(await screen.findByText('Bound Space'))
    await user.click(screen.getByRole('button', { name: 'Preview changes' }))

    expect(previewAgentEnterpriseAccess).toHaveBeenCalledWith('agent-1', {
      identity_source_id: 'source-1',
      all_synced_users: false,
      directory_group_ids: ['directory-group-1'],
      pas_user_ids: ['pas-user-1'],
      knowledge_space_ids: ['space-1'],
    })
    const request = vi.mocked(previewAgentEnterpriseAccess).mock.calls[0][1]
    expect(request).not.toHaveProperty('provider')
    expect(request).not.toHaveProperty('principal')
    expect(request).not.toHaveProperty('identity_domain')
    expect(request).not.toHaveProperty('acl_context')
    expect(await screen.findByText('Agent changes')).toBeInTheDocument()
    expect(screen.getByText('Global changes')).toBeInTheDocument()
    expect(screen.getByText('Already configured')).toBeInTheDocument()
  })

  it('requires another confirmation after a stale preview', async () => {
    const refreshedPreview = {
      ...initialPreview,
      creates: [],
      global_changes: [],
      reuses: [
        ...initialPreview.reuses,
        {
          relation_type: 'agent_identity_source_group',
          relation_id: 'group-assignment-1',
          display_name: 'Engineering',
          scope: 'agent',
        },
      ],
      preview_hash: 'b'.repeat(64),
    }
    vi.mocked(applyAgentEnterpriseAccess)
      .mockRejectedValueOnce({
        isAxiosError: true,
        response: {
          status: 409,
          data: {
            detail: {
              code: 'ENTERPRISE_ACCESS_PREVIEW_STALE',
              preview: refreshedPreview,
            },
          },
        },
      })
      .mockResolvedValueOnce({ data: refreshedPreview } as never)
    const onApplied = vi.fn()
    const user = userEvent.setup()
    render(
      <EnterpriseAccessDrawer
        agentId="agent-1"
        bindings={bindings}
        open
        onClose={vi.fn()}
        onApplied={onApplied}
      />,
    )
    await selectSource(user)
    await user.click(
      screen.getByRole('checkbox', { name: 'All synchronized users' }),
    )
    await user.click(screen.getByRole('combobox', { name: 'PolarRAG Spaces' }))
    await user.click(await screen.findByText('Bound Space'))
    await user.click(screen.getByRole('button', { name: 'Preview changes' }))
    await user.click(
      await screen.findByRole('button', { name: 'Confirm and configure' }),
    )

    expect(onApplied).not.toHaveBeenCalled()
    expect(
      await screen.findByText(
        'Configuration changed; review the refreshed preview',
      ),
    ).toBeInTheDocument()
    expect(applyAgentEnterpriseAccess).toHaveBeenCalledTimes(1)

    await user.click(
      screen.getByRole('button', { name: 'Confirm and configure' }),
    )
    expect(applyAgentEnterpriseAccess).toHaveBeenCalledTimes(2)
    expect(onApplied).toHaveBeenCalledTimes(1)
  })
})
