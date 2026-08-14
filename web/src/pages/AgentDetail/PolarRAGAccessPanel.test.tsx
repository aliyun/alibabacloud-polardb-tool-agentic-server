import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createAgentGroupAssignment,
  createAgentPolarRAGBinding,
  createAgentUserAssignment,
  listAgentGroupAssignments,
  listAgentGroupOptions,
  listAgentPolarRAGBindings,
  listAgentUserAssignments,
} from '../../api/agents'
import api from '../../api/client'
import { listPolarRAGInstances } from '../../api/polarrag'
import PolarRAGAccessPanel from './PolarRAGAccessPanel'

vi.mock('../../api/agents', () => ({
  createAgentGroupAssignment: vi.fn(),
  createAgentPolarRAGBinding: vi.fn(),
  createAgentUserAssignment: vi.fn(),
  deleteAgentPolarRAGBinding: vi.fn(),
  deleteAgentUserAssignment: vi.fn(),
  forceRevokeAgentUserToken: vi.fn(),
  listAgentGroupAssignments: vi.fn(),
  listAgentGroupOptions: vi.fn(),
  listAgentPolarRAGBindings: vi.fn(),
  listAgentUserAssignments: vi.fn(),
}))
vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return { ...actual, default: { get: vi.fn() } }
})
vi.mock('../../api/polarrag', () => ({
  listPolarRAGInstances: vi.fn(),
}))

describe('Agent PolarRAG access panel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(listPolarRAGInstances).mockResolvedValue({
      data: { items: [{ id: 'rag-1', name: 'Primary RAG' }] },
    } as never)
    vi.mocked(api.get).mockResolvedValue({
      data: {
        total: 1,
        items: [
          {
            id: 'user-1',
            display_name: 'Alice',
            external_id: 'alice',
            status: 'active',
          },
        ],
      },
    } as never)
    vi.mocked(listAgentPolarRAGBindings).mockResolvedValue({ data: [] } as never)
    vi.mocked(listAgentUserAssignments).mockResolvedValue({ data: [] } as never)
    vi.mocked(listAgentGroupOptions).mockResolvedValue({ data: [] } as never)
    vi.mocked(listAgentGroupAssignments).mockResolvedValue({ data: [] } as never)
    vi.mocked(createAgentPolarRAGBinding).mockResolvedValue({
      data: {
        id: 'binding-1',
        polarrag_instance_id: 'rag-1',
        instance_name: 'Primary RAG',
        public_knowledge_resource_ids: null,
        created_at: '2026-08-05T00:00:00Z',
      },
    } as never)
    vi.mocked(createAgentUserAssignment).mockResolvedValue({ data: {} } as never)
    vi.mocked(createAgentGroupAssignment).mockResolvedValue({ data: {} } as never)
  })

  it('binds an instance and assigns a PAS user without exposing a token', async () => {
    const user = userEvent.setup()
    const onBindingsChange = vi.fn()
    render(
      <PolarRAGAccessPanel
        agentId="agent-1"
        bindings={[]}
        onBindingsChange={onBindingsChange}
      />,
    )

    await user.click(
      await screen.findByRole('combobox', { name: 'PolarRAG instance' }),
    )
    await user.click(await screen.findByText('Primary RAG'))
    await user.click(screen.getByRole('button', { name: 'Bind instance' }))
    expect(createAgentPolarRAGBinding).toHaveBeenCalledWith('agent-1', 'rag-1')
    expect(onBindingsChange).toHaveBeenCalledWith([
      expect.objectContaining({
        id: 'binding-1',
        polarrag_instance_id: 'rag-1',
      }),
    ])

    await user.click(screen.getByRole('combobox', { name: 'PAS user' }))
    await user.click(await screen.findByText('Alice (alice)'))
    await user.click(screen.getByRole('button', { name: 'Assign user' }))
    expect(createAgentUserAssignment).toHaveBeenCalledWith('agent-1', 'user-1')
    expect(screen.queryByText(/pas_user_agent_/)).not.toBeInTheDocument()
  })

  it('assigns a Department or registered enterprise group', async () => {
    vi.mocked(listAgentGroupOptions).mockResolvedValue({
      data: [
        {
          group_kind: 'department',
          department_id: 'department-1',
          department_name: 'Engineering',
          identity_domain: null,
          provider: null,
          principal_id: null,
          member_count: 2,
        },
        {
          group_kind: 'enterprise',
          department_id: null,
          department_name: null,
          identity_domain: 'mcp-e2e-domain',
          provider: 'feishu',
          principal_id: 'finance-e2e',
          member_count: 1,
        },
      ],
    } as never)
    const user = userEvent.setup()
    render(
      <PolarRAGAccessPanel
        agentId="agent-1"
        bindings={[]}
        onBindingsChange={vi.fn()}
      />,
    )

    await user.click(
      await screen.findByRole('combobox', {
        name: 'PAS or enterprise group',
      }),
    )
    await user.click(
      await screen.findByText('Department · Engineering (2 members)'),
    )
    await user.click(screen.getByRole('button', { name: 'Assign group' }))

    expect(createAgentGroupAssignment).toHaveBeenCalledWith(
      'agent-1',
      expect.objectContaining({
        group_kind: 'department',
        department_id: 'department-1',
      }),
    )
    expect(screen.queryByText(/pas_user_agent_/)).not.toBeInTheDocument()
  })

  it('explains when every PolarRAG instance is already bound', async () => {
    const user = userEvent.setup()
    render(
      <PolarRAGAccessPanel
        agentId="agent-1"
        bindings={[
          {
            id: 'binding-1',
            polarrag_instance_id: 'rag-1',
            instance_name: 'Primary RAG',
            public_knowledge_resource_ids: null,
            created_at: '2026-08-05T00:00:00Z',
          },
        ]}
        onBindingsChange={vi.fn()}
      />,
    )

    await user.click(
      await screen.findByRole('combobox', { name: 'PolarRAG instance' }),
    )
    expect(
      await screen.findByText('All PolarRAG instances are already bound'),
    ).toBeInTheDocument()
    expect(
      screen.getByText(
        /shown in this PolarRAG instances tab/,
      ),
    ).toBeInTheDocument()
  })
})
