import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  getAgentKnowledgeBindingStatus,
  getAgentKnowledgeAllUsersStatus,
  getAgentKnowledgeVisibility,
  updateAgentKnowledgeAllUsers,
  listAgentKnowledgeResourceOptions,
  listAgentKnowledgeSubjectOptions,
  updateAgentKnowledgeBindingsBatch,
} from '../../api/agents'
import KnowledgeBindingsPanel from './KnowledgeBindingsPanel'

vi.mock('./ExternalKnowledgeAutoBindPanel', () => ({ default: ({ onChanged }: { onChanged: () => void }) => <div>External auto-binding settings<button onClick={onChanged}>Complete permission refresh</button></div> }))

vi.mock('../../api/agents', () => ({
  getAgentKnowledgeBindingStatus: vi.fn(),
  getAgentKnowledgeAllUsersStatus: vi.fn(),
  getAgentKnowledgeVisibility: vi.fn(),
  updateAgentKnowledgeAllUsers: vi.fn(),
  listAgentKnowledgeResourceOptions: vi.fn(),
  listAgentKnowledgeSubjectOptions: vi.fn(),
  updateAgentKnowledgeBindingsBatch: vi.fn(),
}))

describe('Agent knowledge bindings panel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(listAgentKnowledgeResourceOptions).mockResolvedValue({ data: {
      items: [
        { knowledge_resource_id: 'resource-1', space_id: 'space-1', space_name: 'Finance', kb_id: 'kb-1', name: 'Finance policies', management_mode: 'NATIVE', identity_source_id: null, identity_source_ids: [] },
        { knowledge_resource_id: 'resource-2', space_id: 'space-1', space_name: 'Finance', kb_id: 'kb-2', name: 'Finance reports', management_mode: 'EXTERNAL_SYNC', identity_source_id: null, identity_source_ids: [] },
      ], total: 2, offset: 0, limit: 20,
    } } as never)
    vi.mocked(getAgentKnowledgeAllUsersStatus).mockImplementation(async (_agent, ids) => ({
      data: { items: ids.map((id) => ({ knowledge_resource_id: id, select_all: false, excluded_subject_count: 0, status: 'PARTIAL' })) },
    }) as never)
    vi.mocked(getAgentKnowledgeVisibility).mockResolvedValue({ data: { instance_count: 1, statuses: { PUBLIC: 'BOUND', PERSONAL: 'BOUND' } } } as never)
    vi.mocked(updateAgentKnowledgeAllUsers).mockImplementation(async (_agent, _ids, operation) => {
      vi.mocked(getAgentKnowledgeAllUsersStatus).mockImplementation(async (_agent, ids) => ({
        data: { items: ids.map((id) => ({ knowledge_resource_id: id, select_all: operation === 'BIND', excluded_subject_count: 0, status: operation === 'BIND' ? 'BOUND' : 'UNBOUND' })) },
      }) as never)
      return { data: { items: [] } } as never
    })
    vi.mocked(listAgentKnowledgeSubjectOptions).mockResolvedValue({ data: {
      items: [
        { subject: { type: 'USER', user_id: 'user-1' }, label: 'Alice', identity_source: 'PAS', status: 'PARTIAL', inheritance_sources: [] },
        { subject: { type: 'USER', user_id: 'user-2' }, label: 'Bob', identity_source: 'PAS', status: 'UNBOUND', inheritance_sources: [] },
      ], next_offset: null,
    } } as never)
    vi.mocked(getAgentKnowledgeBindingStatus).mockImplementation(async (_agentId, subjects, ids) => ({ data: { items: ids.map((id) => {
      const indexes = subjects.flatMap((subject, index) => id === 'resource-1' && subject.user_id === 'user-1' ? [index] : [])
      return { knowledge_resource_id: id, status: indexes.length === 0 ? 'UNBOUND' : indexes.length === subjects.length ? 'BOUND' : 'PARTIAL', bound_subject_count: indexes.length, subject_count: subjects.length, bound_subject_indexes: indexes, direct_bound_subject_indexes: indexes, inherited_subject_sources: {} }
    }) } }) as never)
    vi.mocked(updateAgentKnowledgeBindingsBatch).mockResolvedValue({ data: {} } as never)
  })


  it('keeps current rows mounted until refreshed bindings arrive', async () => {
    render(<KnowledgeBindingsPanel agentId="agent-1" />)
    const original = await screen.findByText('Finance policies')
    const response = await vi.mocked(listAgentKnowledgeResourceOptions).mock.results[0].value
    let resolve!: (value: never) => void
    vi.mocked(listAgentKnowledgeResourceOptions).mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    fireEvent.click(screen.getByRole('button', { name: 'Complete permission refresh' }))
    expect(screen.getByText('Finance policies')).toBe(original)
    expect(screen.getByText('Finance reports')).toBeInTheDocument()
    await act(async () => resolve(response as never))
    expect(screen.getByText('Finance policies')).toBe(original)
    expect(listAgentKnowledgeResourceOptions).toHaveBeenCalledTimes(2)
  })

  it('retains displayed bindings when the post-refresh list request fails', async () => {
    render(<KnowledgeBindingsPanel agentId="agent-1" />)
    const original = await screen.findByText('Finance policies')
    vi.mocked(listAgentKnowledgeResourceOptions).mockRejectedValueOnce({ isAxiosError: true, response: { data: { detail: 'List unavailable' } } })
    fireEvent.click(screen.getByRole('button', { name: 'Complete permission refresh' }))
    await screen.findByText(/The rows below show pre-refresh data/)
    expect(screen.getByText(/List unavailable/)).toBeInTheDocument()
    expect(screen.getByText('Finance policies')).toBe(original)
    expect(screen.getByText('Finance reports')).toBeInTheDocument()
  })

  it.each([new Error('offline'), { isAxiosError: true }])('shows only the stale-data warning when reload has no server reason', async (failure) => {
    render(<KnowledgeBindingsPanel agentId="agent-1" />)
    const original = await screen.findByText('Finance policies')
    vi.mocked(listAgentKnowledgeResourceOptions).mockRejectedValueOnce(failure)
    fireEvent.click(screen.getByRole('button', { name: 'Complete permission refresh' }))
    const warning = await screen.findByText('Unable to reload bindings. The rows below show pre-refresh data; retry before making further changes.')
    expect(warning).not.toHaveTextContent('Could not load knowledge bindings.')
    expect(screen.getByText('Finance policies')).toBe(original)
    expect(screen.getByText('Finance reports')).toBeInTheDocument()
  })

  it('shows a unified list and never sends a management mode filter', async () => {
    render(<KnowledgeBindingsPanel agentId="agent-1" />)
    expect(await screen.findByText('Finance reports')).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: /enterprise app-synced knowledge/i })).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /manage user bindings/i })).toHaveLength(2)
    for (const call of vi.mocked(listAgentKnowledgeResourceOptions).mock.calls) {
      expect(call[1]).not.toHaveProperty('management_mode')
    }
  })

  it('loads unified knowledge bases and manages a single KB in its own drawer', async () => {
    const user = userEvent.setup()
    render(<KnowledgeBindingsPanel agentId="agent-1" />)

    expect(await screen.findByText('Finance policies')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Configure PUBLIC scope' })).not.toBeInTheDocument()
    expect(listAgentKnowledgeResourceOptions).toHaveBeenCalledWith('agent-1', {
      limit: 20, binding_status: 'bound', offset: 0, search: undefined,
    })
    expect(screen.queryByRole('button', { name: 'Manage bindings in bulk' })).not.toBeInTheDocument()
    expect(screen.getByText('External auto-binding settings')).toBeInTheDocument()
    await user.click((await screen.findAllByRole('button', { name: 'Manage user bindings' }))[0])
    expect(await screen.findByRole('dialog', { name: 'Manage user bindings · Finance policies' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('labels a disabled historical user and still permits removing its binding', async () => {
    vi.mocked(listAgentKnowledgeSubjectOptions).mockResolvedValue({ data: {
      items: [{ subject: { type: 'USER', user_id: 'user-1' }, label: 'Departed user',
        identity_source: 'Feishu', status: 'BOUND', inheritance_sources: [], availability: 'disabled' }],
      next_offset: null,
    } } as never)
    render(<KnowledgeBindingsPanel agentId="agent-1" />)
    await screen.findByText('Finance policies')
    fireEvent.click(screen.getAllByRole('button', { name: 'Manage user bindings' })[0])
    const drawer = within(await screen.findByRole('dialog'))
    fireEvent.click(drawer.getByRole('radio', { name: 'Bound' }))
    expect(await drawer.findByText('Disabled')).toBeInTheDocument()
    fireEvent.click(drawer.getByRole('button', { name: 'Select current page' }))
    const remove = await drawer.findByRole('button', { name: 'Remove from selected KBs' })
    await waitFor(() => expect(remove).toBeEnabled())
    fireEvent.click(remove)
    await waitFor(() => expect(updateAgentKnowledgeBindingsBatch).toHaveBeenCalledWith('agent-1', {
      operations: [{ operation: 'UNBIND', subjects: [{ type: 'USER', user_id: 'user-1' }],
        targets: [{ knowledge_resource_id: 'resource-1' }] }], activate_scoped_mode: false,
    }))
  })

  it('adds selected subjects to multiple KBs without replacing existing grants', async () => {
    const user = userEvent.setup()
    render(<KnowledgeBindingsPanel agentId="agent-1" />)
    expect(await screen.findByText('Finance policies')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Select current page' }))
    expect(screen.getByRole('button', { name: 'Clear current page selection' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Manage bindings in bulk' }))
    expect(await screen.findByText('Bound to some selected KBs')).toBeInTheDocument()
    expect(await screen.findByText('Not bound to any selected KB')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Select current page' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add to selected KBs' })).toBeEnabled())
    expect(screen.queryByRole('button', { name: 'Remove from selected KBs' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Add to selected KBs' }))

    await waitFor(() => expect(updateAgentKnowledgeBindingsBatch).toHaveBeenCalledWith('agent-1', {
      operations: [
        { operation: 'BIND', subjects: [{ type: 'USER', user_id: 'user-1' }, { type: 'USER', user_id: 'user-2' }], targets: [{ knowledge_resource_id: 'resource-1' }] },
        { operation: 'BIND', subjects: [{ type: 'USER', user_id: 'user-1' }, { type: 'USER', user_id: 'user-2' }], targets: [{ knowledge_resource_id: 'resource-2' }] },
      ],
      activate_scoped_mode: false,
    }))
  })

  it('refreshes the all-user action after binding and unbinding all users', async () => {
    const user = userEvent.setup()
    render(<KnowledgeBindingsPanel agentId="agent-1" />)
    expect(await screen.findByText('Finance policies')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Select current page' }))
    await user.click(screen.getByRole('button', { name: 'Manage bindings in bulk' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Bind all users' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Bind all users' }))
    await waitFor(() => expect(updateAgentKnowledgeAllUsers).toHaveBeenCalledWith('agent-1', ['resource-1', 'resource-2'], 'BIND'))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Unbind all users' })).toBeEnabled())
    expect(screen.queryByRole('button', { name: 'Bind all users' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Unbind all users' }))
    await waitFor(() => expect(updateAgentKnowledgeAllUsers).toHaveBeenLastCalledWith('agent-1', ['resource-1', 'resource-2'], 'UNBIND'))
  })

  it('debounces search and ignores a previous response even during the debounce window', async () => {
    const user = userEvent.setup()
    let finishOld!: (value: never) => void
    vi.mocked(listAgentKnowledgeSubjectOptions).mockImplementationOnce(() => new Promise((resolve) => {
      finishOld = resolve
    }))
    render(<KnowledgeBindingsPanel agentId="agent-1" />)
    await screen.findByText('Finance policies')
    await user.click(screen.getAllByRole('button', { name: 'Manage user bindings' })[0])
    await waitFor(() => expect(listAgentKnowledgeSubjectOptions).toHaveBeenCalledTimes(1))
    const search = screen.getByPlaceholderText('Search users or groups')
    fireEvent.change(search, { target: { value: 'A' } })
    fireEvent.change(search, { target: { value: 'Al' } })
    fireEvent.change(search, { target: { value: 'Alice' } })
    expect(listAgentKnowledgeSubjectOptions).toHaveBeenCalledTimes(1)
    await act(async () => finishOld({ data: { items: [{
      subject: { type: 'USER', user_id: 'stale' }, label: 'Stale user', identity_source: 'PAS',
      status: 'UNBOUND', inheritance_sources: [],
    }], next_offset: 100 } } as never))
    expect(screen.queryByText('Stale user')).not.toBeInTheDocument()
    await screen.findByText('Alice')
    expect(listAgentKnowledgeSubjectOptions).toHaveBeenCalledTimes(2)
    expect(listAgentKnowledgeSubjectOptions).toHaveBeenLastCalledWith('agent-1', expect.objectContaining({
      search: 'Alice', offset: 0,
    }))
  })

  it('does not apply a previous tab error to the new candidate list', async () => {
    const user = userEvent.setup()
    let failOld!: (error: Error) => void
    vi.mocked(listAgentKnowledgeSubjectOptions).mockImplementationOnce(() => new Promise((_resolve, reject) => {
      failOld = reject
    }))
    render(<KnowledgeBindingsPanel agentId="agent-1" />)
    await screen.findByText('Finance policies')
    await user.click(screen.getAllByRole('button', { name: 'Manage user bindings' })[0])
    await waitFor(() => expect(listAgentKnowledgeSubjectOptions).toHaveBeenCalledTimes(1))
    await user.click(screen.getByRole('tab', { name: 'Departments and groups' }))
    await screen.findByText('Alice')
    await act(async () => failOld(new Error('Obsolete request failed')))
    expect(screen.queryByText('Obsolete request failed')).not.toBeInTheDocument()
    expect(screen.getByText('Alice')).toBeInTheDocument()
    expect(listAgentKnowledgeSubjectOptions).toHaveBeenLastCalledWith('agent-1', expect.objectContaining({
      category: 'groups', offset: 0,
    }))
  })
})
