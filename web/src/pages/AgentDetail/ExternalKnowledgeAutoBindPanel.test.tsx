import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { switchAgentKnowledgeScopeMode, getAgentExternalKnowledgeAutoBind, updateAgentExternalKnowledgeAutoBind, reconcileAgentExternalKnowledge } from '../../api/agents'
import ExternalKnowledgeAutoBindPanel from './ExternalKnowledgeAutoBindPanel'

vi.mock('../../api/agents', () => ({
  switchAgentKnowledgeScopeMode: vi.fn(),
  getAgentExternalKnowledgeAutoBind: vi.fn(),
  updateAgentExternalKnowledgeAutoBind: vi.fn(),
  reconcileAgentExternalKnowledge: vi.fn(),
}))
const label = 'Automatically bind external knowledge bases'
describe('External knowledge auto-binding switch', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(getAgentExternalKnowledgeAutoBind).mockImplementation(async (agent_id) => ({ data: {
      agent_id, enabled: false, knowledge_scope_mode: 'SCOPED',
    } }) as never)
    vi.mocked(updateAgentExternalKnowledgeAutoBind).mockImplementation(async (agent_id, enabled) => ({ data: { agent_id, enabled } }) as never)
  })

  it('loads the persisted state and saves both enabling and disabling', async () => {
    const onChanged = vi.fn()
    render(<ExternalKnowledgeAutoBindPanel agentId="agent-1" onChanged={onChanged} />)
    const toggle = await screen.findByRole('switch', { name: label })
    await waitFor(() => expect(toggle).toBeEnabled())
    expect(toggle).not.toBeChecked()
    fireEvent.click(toggle)
    await waitFor(() => expect(toggle).toBeChecked())
    expect(updateAgentExternalKnowledgeAutoBind).toHaveBeenLastCalledWith('agent-1', true)
    fireEvent.click(toggle)
    await waitFor(() => expect(toggle).not.toBeChecked())
    expect(updateAgentExternalKnowledgeAutoBind).toHaveBeenLastCalledWith('agent-1', false)
    expect(onChanged).toHaveBeenCalledTimes(2)
  })

  it('preserves the saved state on failure and prevents duplicate in-flight saves', async () => {
    let reject!: (error: Error) => void
    vi.mocked(updateAgentExternalKnowledgeAutoBind).mockImplementation(() => new Promise((_resolve, fail) => { reject = fail }))
    const onChanged = vi.fn()
    render(<ExternalKnowledgeAutoBindPanel agentId="agent-1" onChanged={onChanged} />)
    const toggle = await screen.findByRole('switch', { name: label })
    await waitFor(() => expect(toggle).toBeEnabled())
    fireEvent.click(toggle)
    expect(toggle).toBeDisabled()
    fireEvent.click(toggle)
    expect(updateAgentExternalKnowledgeAutoBind).toHaveBeenCalledTimes(1)
    await act(async () => reject(new Error('network failed')))
    await screen.findByRole('alert')
    expect(toggle).not.toBeChecked()
    expect(toggle).toBeEnabled()
    expect(onChanged).not.toHaveBeenCalled()
  })

  it('blocks changes when loading fails and supports retry', async () => {
    vi.mocked(getAgentExternalKnowledgeAutoBind).mockRejectedValueOnce(new Error('offline'))
    render(<ExternalKnowledgeAutoBindPanel agentId="agent-1" onChanged={vi.fn()} />)
    await screen.findByRole('alert')
    expect(screen.queryByRole('switch', { name: label })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.getByRole('switch', { name: label })).toBeEnabled())
  })

  it.each([false, true])('hides settings for a persisted LEGACY_ALL Agent (enabled=%s)', async (enabled) => {
    let resolve!: (result: never) => void
    vi.mocked(getAgentExternalKnowledgeAutoBind).mockImplementation(() => new Promise((done) => { resolve = done }))
    render(<ExternalKnowledgeAutoBindPanel agentId="agent-1" onChanged={vi.fn()} />)
    expect(screen.queryByRole('switch', { name: label })).not.toBeInTheDocument()
    await act(async () => resolve({ data: {
      agent_id: 'agent-1', enabled, knowledge_scope_mode: 'LEGACY_ALL',
    } } as never))
    expect(screen.queryByRole('switch', { name: label })).not.toBeInTheDocument()
    expect(screen.queryByText(label)).not.toBeInTheDocument()
    expect(updateAgentExternalKnowledgeAutoBind).not.toHaveBeenCalled()
  })

  it('ignores a save response after switching to another Agent', async () => {
    let resolve!: (result: never) => void
    vi.mocked(updateAgentExternalKnowledgeAutoBind).mockImplementation(() => new Promise((done) => { resolve = done }))
    const onChanged = vi.fn()
    const view = render(<ExternalKnowledgeAutoBindPanel agentId="agent-1" onChanged={onChanged} />)
    await waitFor(() => expect(screen.getByRole('switch', { name: label })).toBeEnabled())
    fireEvent.click(screen.getByRole('switch', { name: label }))
    view.rerender(<ExternalKnowledgeAutoBindPanel agentId="agent-2" onChanged={onChanged} />)
    await waitFor(() => expect(getAgentExternalKnowledgeAutoBind).toHaveBeenCalledWith('agent-2'))
    await act(async () => resolve({ data: { agent_id: 'agent-1', enabled: true } } as never))
    expect(screen.getByRole('switch', { name: label })).not.toBeChecked()
    expect(onChanged).not.toHaveBeenCalled()
  })
})


it('recomputes snapshots without toggling the saved setting', async () => {
  vi.mocked(getAgentExternalKnowledgeAutoBind).mockResolvedValue({ data: { agent_id: 'agent-1', enabled: true, knowledge_scope_mode: 'SCOPED' } } as never)
  vi.mocked(reconcileAgentExternalKnowledge).mockResolvedValue({ data: { agent_id: 'agent-1', snapshots_processed: 2, snapshots_skipped: 0, bindings_projected: 3 } } as never)
  vi.mocked(updateAgentExternalKnowledgeAutoBind).mockClear()
  const onChanged = vi.fn()
  render(<ExternalKnowledgeAutoBindPanel agentId="agent-1" onChanged={onChanged} />)
  const button = await screen.findByRole('button', { name: 'Recompute from imported permissions' })
  fireEvent.click(button)
  await waitFor(() => expect(reconcileAgentExternalKnowledge).toHaveBeenCalledWith('agent-1'))
  await screen.findByText('Processed 2 permission records, applied 3 knowledge access grants, skipped 0 records.')
  expect(updateAgentExternalKnowledgeAutoBind).not.toHaveBeenCalled()
  expect(onChanged).toHaveBeenCalled()
})


it('switches legacy mode with opt-in auto-binding in one request', async () => {
  vi.mocked(getAgentExternalKnowledgeAutoBind).mockResolvedValue({ data: { agent_id: 'agent-1', enabled: false, knowledge_scope_mode: 'LEGACY_ALL' } } as never)
  vi.mocked(switchAgentKnowledgeScopeMode).mockResolvedValue({ data: { agent_id: 'agent-1', mode: 'SCOPED', enabled: true, preserved_knowledge_resources: 2 } } as never)
  const changed = vi.fn()
  render(<ExternalKnowledgeAutoBindPanel agentId="agent-1" onChanged={changed} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Switch to SCOPED' }))
  fireEvent.click(await screen.findByRole('checkbox', { name: label }))
  fireEvent.click(screen.getByRole('button', { name: 'OK' }))
  await waitFor(() => expect(switchAgentKnowledgeScopeMode).toHaveBeenCalledWith('agent-1', true, 'SCOPED', true, 'LEGACY_ALL'))
  expect(await screen.findByRole('switch', { name: label })).toBeChecked()
  expect(changed).toHaveBeenCalledTimes(1)
})

it('keeps legacy mode and allows retry when switching fails', async () => {
  vi.mocked(getAgentExternalKnowledgeAutoBind).mockResolvedValue({ data: { agent_id: 'agent-1', enabled: false, knowledge_scope_mode: 'LEGACY_ALL' } } as never)
  vi.mocked(switchAgentKnowledgeScopeMode).mockRejectedValue(new Error('Switch failed'))
  const changed = vi.fn()
  render(<ExternalKnowledgeAutoBindPanel agentId="agent-1" onChanged={changed} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Switch to SCOPED' }))
  fireEvent.click(screen.getByRole('button', { name: 'OK' }))
  await screen.findByRole('alert')
  expect(changed).not.toHaveBeenCalled()
  expect(screen.queryByRole('switch', { name: label })).not.toBeInTheDocument()
})


it('switches SCOPED back to legacy and hides automatic binding settings', async () => {
  vi.mocked(getAgentExternalKnowledgeAutoBind).mockResolvedValue({ data: { agent_id: 'agent-1', enabled: true, knowledge_scope_mode: 'SCOPED' } } as never)
  vi.mocked(switchAgentKnowledgeScopeMode).mockResolvedValue({ data: { agent_id: 'agent-1', mode: 'LEGACY_ALL', enabled: false } } as never)
  render(<ExternalKnowledgeAutoBindPanel agentId="agent-1" onChanged={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Switch to LEGACY_ALL' }))
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'OK' }))
  await waitFor(() => expect(switchAgentKnowledgeScopeMode).toHaveBeenCalledWith('agent-1', false, 'LEGACY_ALL', false, 'SCOPED'))
  await screen.findByRole('button', { name: 'Switch to SCOPED' })
  expect(screen.queryByRole('switch')).not.toBeInTheDocument()
})
