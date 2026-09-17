import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  listAgentGroupAssignments,
  listAgentKnowledgeBindings,
  listAgentKnowledgeResourceOptions,
  listAgentUserAssignments,
  updateAgentKnowledgeBindingsBatch,
} from '../../api/agents'
import KnowledgeBindingsPanel from './KnowledgeBindingsPanel'

vi.mock('../../api/agents', () => ({
  listAgentGroupAssignments: vi.fn(),
  listAgentKnowledgeBindings: vi.fn(),
  listAgentKnowledgeResourceOptions: vi.fn(),
  listAgentUserAssignments: vi.fn(),
  updateAgentKnowledgeBindingsBatch: vi.fn(),
}))

describe('Agent knowledge bindings panel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(listAgentKnowledgeBindings).mockResolvedValue({
      data: {
        items: [
          {
            binding_id: 'binding-1',
            scope_id: 'scope-1',
            origin: 'MANUAL',
            identity_source_id: null,
            external_scope_id: null,
            subject: {
              type: 'USER',
              user_id: 'user-1',
              display_name: 'Alice',
              external_id: 'alice',
            },
            knowledge_resources: [
              {
                knowledge_resource_id: 'resource-1',
                space_id: 'space-1',
                space_name: 'Finance',
                kb_id: 'kb-1',
                name: 'Finance policies',
              },
            ],
            created_at: '2026-09-13T00:00:00Z',
            updated_at: null,
          },
          {
            binding_id: 'binding-2',
            scope_id: 'scope-2',
            origin: 'EXTERNAL_SYNC',
            identity_source_id: 'source-1',
            external_scope_id: 'wiki-space-1',
            subject: {
              type: 'USER',
              user_id: 'user-2',
              display_name: 'Bob',
              external_id: 'bob',
            },
            knowledge_resources: [
              {
                knowledge_resource_id: 'resource-2',
                space_id: 'space-1',
                space_name: 'Finance',
                kb_id: 'kb-2',
                name: 'Finance reports',
              },
            ],
            created_at: '2026-09-13T00:00:00Z',
            updated_at: null,
          },
        ],
        total: 2,
        offset: 0,
        limit: 20,
        knowledge_scope_mode: 'SCOPED',
      },
    } as never)
    vi.mocked(listAgentKnowledgeResourceOptions).mockResolvedValue({
      data: {
        items: [
          {
            knowledge_resource_id: 'resource-1',
            space_id: 'space-1',
            space_name: 'Finance',
            kb_id: 'kb-1',
            name: 'Finance policies',
          },
          {
            knowledge_resource_id: 'resource-2',
            space_id: 'space-1',
            space_name: 'Finance',
            kb_id: 'kb-2',
            name: 'Finance reports',
          },
        ],
        total: 2,
        offset: 0,
        limit: 50,
      },
    } as never)
    vi.mocked(listAgentUserAssignments).mockResolvedValue({
      data: {
        items: [
          {
            id: 'assignment-1',
            user_id: 'user-1',
            user_name: 'Alice',
            user_status: 'active',
            token: null,
            created_at: '2026-09-13T00:00:00Z',
          },
        ],
        total: 1,
        offset: 0,
        limit: 50,
      },
    } as never)
    vi.mocked(listAgentGroupAssignments).mockResolvedValue({
      data: { items: [], total: 0, offset: 0, limit: 50 },
    } as never)
    vi.mocked(updateAgentKnowledgeBindingsBatch).mockResolvedValue({
      data: {
        agent_id: 'agent-1',
        operations_processed: 1,
        knowledge_scope_mode: 'SCOPED',
      },
    } as never)
  })

  it('shows manual and externally managed relationships', async () => {
    render(<KnowledgeBindingsPanel agentId="agent-1" />)

    expect(await screen.findByText('Knowledge bindings')).toBeInTheDocument()
    expect(screen.getByText('Alice')).toBeInTheDocument()
    expect(screen.getByText('Finance policies')).toBeInTheDocument()
    expect(screen.getByText('External sync')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Unbind Alice' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Unbind Bob' })).not.toBeInTheDocument()
  })

  it('binds selected Agent users to multiple knowledge bases', async () => {
    const user = userEvent.setup()
    render(<KnowledgeBindingsPanel agentId="agent-1" />)

    await user.click(await screen.findByRole('button', { name: 'Bind' }))
    const dialog = screen.getByRole('dialog')
    await user.click(within(dialog).getByRole('combobox', { name: 'Users' }))
    await user.click(await screen.findByText('Alice (user-1)'))
    await user.click(
      within(dialog).getByRole('combobox', { name: 'Knowledge bases' }),
    )
    await user.click(
      await screen.findByText('Finance policies · Finance / kb-1'),
    )
    await user.click(
      await screen.findByText('Finance reports · Finance / kb-2'),
    )
    expect(within(dialog).getByText('Alice (user-1)')).toBeInTheDocument()
    expect(
      within(dialog).getByText('Finance policies · Finance / kb-1'),
    ).toBeInTheDocument()
    expect(
      within(dialog).getByText('Finance reports · Finance / kb-2'),
    ).toBeInTheDocument()
    const confirm = within(dialog).getByRole('button', { name: 'Bind' })
    expect(confirm).toBeEnabled()
    await user.click(confirm)

    expect(updateAgentKnowledgeBindingsBatch).toHaveBeenCalledWith('agent-1', {
      operations: [
        {
          operation: 'BIND',
          subjects: [{ type: 'USER', user_id: 'user-1' }],
          targets: [
            { space_id: 'space-1', kb_id: 'kb-1' },
            { space_id: 'space-1', kb_id: 'kb-2' },
          ],
        },
      ],
      activate_scoped_mode: true,
    })
  })

  it('unbinds a Department directly from its relationship row', async () => {
    vi.mocked(listAgentKnowledgeBindings).mockResolvedValue({
      data: {
        items: [
          {
            binding_id: 'binding-department',
            scope_id: 'scope-department',
            origin: 'MANUAL',
            identity_source_id: null,
            external_scope_id: null,
            subject: {
              type: 'DEPARTMENT',
              department_id: 'department-1',
              display_name: 'Engineering',
            },
            knowledge_resources: [
              {
                knowledge_resource_id: 'resource-1',
                space_id: 'space-1',
                space_name: 'Finance',
                kb_id: 'kb-1',
                name: 'Finance policies',
              },
            ],
            created_at: '2026-09-13T00:00:00Z',
            updated_at: null,
          },
        ],
        total: 1,
        offset: 0,
        limit: 20,
        knowledge_scope_mode: 'SCOPED',
      },
    } as never)
    const user = userEvent.setup()
    render(<KnowledgeBindingsPanel agentId="agent-1" />)

    await user.click(
      await screen.findByRole('button', { name: 'Unbind Engineering' }),
    )
    const dialog = screen.getByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Unbind' }))

    expect(updateAgentKnowledgeBindingsBatch).toHaveBeenCalledWith('agent-1', {
      operations: [
        {
          operation: 'UNBIND',
          subjects: [{ type: 'DEPARTMENT', department_id: 'department-1' }],
          targets: [{ space_id: 'space-1', kb_id: 'kb-1' }],
        },
      ],
      activate_scoped_mode: false,
    })
  })
})
