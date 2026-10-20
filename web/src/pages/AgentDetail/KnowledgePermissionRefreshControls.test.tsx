import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { getAgentKnowledgePermissionRefresh, refreshAgentKnowledgePermissions } from '../../api/agents'
import KnowledgePermissionRefreshControls from './KnowledgePermissionRefreshControls'
vi.mock('../../api/agents', () => ({ getAgentKnowledgePermissionRefresh: vi.fn(), refreshAgentKnowledgePermissions: vi.fn() }))
const job = (state: 'RUNNING' | 'SUCCEEDED' | 'FAILED' = 'RUNNING') => ({ job_id: 'job-1', agent_id: 'agent-1', state, resources_total: 1, resources_completed: state === 'SUCCEEDED' ? 1 : 0 })
const renderControls = (selectedResourceIds: string[] = []) => {
  const onChanged = vi.fn()
  const onBusyChange = vi.fn()
  const view = render(<KnowledgePermissionRefreshControls agentId="agent-1" selectedResourceIds={selectedResourceIds} disabled={false} onChanged={onChanged} onBusyChange={onBusyChange} />)
  return { ...view, onChanged, onBusyChange }
}
beforeEach(() => { vi.resetAllMocks(); sessionStorage.clear() })
it('refreshes the whole Agent and follows 202 progress to completion', async () => {
  vi.mocked(refreshAgentKnowledgePermissions).mockResolvedValue({ data: job() } as never)
  vi.mocked(getAgentKnowledgePermissionRefresh).mockResolvedValue({ data: job('SUCCEEDED') } as never)
  const { onChanged } = renderControls()
  fireEvent.click(screen.getByRole('button', { name: 'Refresh all knowledge permissions for this Agent' }))
  await waitFor(() => expect(refreshAgentKnowledgePermissions).toHaveBeenCalledWith('agent-1', undefined))
  expect(screen.getByRole('button', { name: 'Refresh all knowledge permissions for this Agent' })).toBeDisabled()
  await screen.findByText('Permissions refreshed', {}, { timeout: 3000 })
  expect(getAgentKnowledgePermissionRefresh).toHaveBeenCalledWith('agent-1', 'job-1')
  expect(onChanged).toHaveBeenCalledOnce()
  expect(sessionStorage.getItem('pas.knowledge-refresh.agent-1')).toBeNull()
})
it('targets exactly one selected KB and handles a synchronous completion', async () => {
  vi.mocked(refreshAgentKnowledgePermissions).mockResolvedValue({ data: job('SUCCEEDED') } as never)
  const { onChanged } = renderControls(['kb-1'])
  fireEvent.click(screen.getByRole('button', { name: 'Refresh selected knowledge bases' }))
  await screen.findByText('Permissions refreshed')
  expect(refreshAgentKnowledgePermissions).toHaveBeenCalledWith('agent-1', ['kb-1'])
  expect(onChanged).toHaveBeenCalledOnce()
  expect(getAgentKnowledgePermissionRefresh).not.toHaveBeenCalled()
})
it('allows multiple selections and displays terminal failure without refreshing bindings', async () => {
  vi.mocked(refreshAgentKnowledgePermissions).mockResolvedValue({ data: job('FAILED') } as never)
  const { onChanged } = renderControls(['kb-1', 'kb-2'])
  expect(screen.getByRole('button', { name: 'Refresh selected knowledge bases' })).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: 'Refresh selected knowledge bases' }))
  await screen.findByText('Permission refresh failed')
  expect(refreshAgentKnowledgePermissions).toHaveBeenCalledWith('agent-1', ['kb-1', 'kb-2'])
  fireEvent.click(screen.getByRole('button', { name: 'Refresh all knowledge permissions for this Agent' }))
  await screen.findByText('Permission refresh failed')
  expect(onChanged).not.toHaveBeenCalled()
})
it('resumes an already running task after 409', async () => {
  vi.mocked(refreshAgentKnowledgePermissions).mockRejectedValue({ response: { data: { detail: { code: 'REFRESH_ALREADY_RUNNING', job_id: 'job-1' } } } })
  vi.mocked(getAgentKnowledgePermissionRefresh).mockResolvedValue({ data: job('SUCCEEDED') } as never)
  renderControls()
  fireEvent.click(screen.getByRole('button', { name: 'Refresh all knowledge permissions for this Agent' }))
  await screen.findByText('Permissions refreshed', {}, { timeout: 3000 })
})
it('restores progress after remount and ignores a response from the previous Agent', async () => {
  sessionStorage.setItem('pas.knowledge-refresh.agent-1', 'job-1')
  let resolve!: (value: never) => void
  vi.mocked(getAgentKnowledgePermissionRefresh).mockImplementation(() => new Promise((done) => { resolve = done }))
  const { rerender, onChanged, onBusyChange } = renderControls()
  await waitFor(() => expect(getAgentKnowledgePermissionRefresh).toHaveBeenCalled(), { timeout: 3000 })
  rerender(<KnowledgePermissionRefreshControls agentId="agent-2" selectedResourceIds={[]} disabled={false} onChanged={onChanged} onBusyChange={onBusyChange} />)
  await act(async () => resolve({ data: job('SUCCEEDED') } as never))
  expect(onChanged).not.toHaveBeenCalled()
  expect(screen.queryByText('Permissions refreshed')).not.toBeInTheDocument()
})
it('retries a failed progress query without starting another task', async () => {
  sessionStorage.setItem('pas.knowledge-refresh.agent-1', 'job-1')
  vi.mocked(getAgentKnowledgePermissionRefresh).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ data: job('SUCCEEDED') } as never)
  const { onChanged } = renderControls()
  await screen.findByRole('alert', {}, { timeout: 3000 })
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  await screen.findByText('Permissions refreshed', {}, { timeout: 3000 })
  expect(refreshAgentKnowledgePermissions).not.toHaveBeenCalled()
  expect(onChanged).toHaveBeenCalledOnce()
})
it('exposes the row action and sends only that resource', async () => {
  const { createRef } = await import('react')
  const ref = createRef<import('./KnowledgePermissionRefreshControls').KnowledgePermissionRefreshHandle>()
  vi.mocked(refreshAgentKnowledgePermissions).mockResolvedValue({ data: job('SUCCEEDED') } as never)
  render(<KnowledgePermissionRefreshControls agentId="agent-1" actionRef={ref} disabled={false} onChanged={vi.fn()} onBusyChange={vi.fn()} />)
  await act(async () => ref.current?.refreshResource('row-kb'))
  expect(refreshAgentKnowledgePermissions).toHaveBeenCalledWith('agent-1', 'row-kb')
  await waitFor(() => expect(screen.getAllByText('Permissions refreshed')).toHaveLength(2))
})

it.each(['<html>Bad Gateway</html>', { job_id: 'broken' }, { ...job(), state: 'UNKNOWN' }])('rejects non-job gateway responses without leaving controls busy', async (data) => {
  vi.mocked(refreshAgentKnowledgePermissions).mockResolvedValue({ data } as never)
  const { onBusyChange } = renderControls()
  fireEvent.click(screen.getByRole('button', { name: 'Refresh all knowledge permissions for this Agent' }))
  await screen.findByRole('alert')
  expect(screen.getByRole('button', { name: 'Refresh all knowledge permissions for this Agent' })).toBeEnabled()
  expect(onBusyChange).toHaveBeenLastCalledWith(false)
  expect(getAgentKnowledgePermissionRefresh).not.toHaveBeenCalled()
})


it.each([1, 3])('warns when all %i target KBs have no permission snapshots', async (count) => {
  vi.mocked(refreshAgentKnowledgePermissions).mockResolvedValue({ data: {
    ...job('SUCCEEDED'), resources_total: count, resources_completed: count, resources_skipped: count,
  } } as never)
  renderControls()
  fireEvent.click(screen.getByRole('button', { name: 'Refresh all knowledge permissions for this Agent' }))
  await screen.findByText('No permission snapshots available; nothing was applied')
  expect(screen.getByRole('status')).toHaveClass('ant-alert-warning')
  expect(screen.queryByText('Permissions refreshed')).not.toBeInTheDocument()
})

it('warns in the row notification when the single KB was skipped', async () => {
  const { createRef } = await import('react')
  const ref = createRef<import('./KnowledgePermissionRefreshControls').KnowledgePermissionRefreshHandle>()
  vi.mocked(refreshAgentKnowledgePermissions).mockResolvedValue({ data: { ...job('SUCCEEDED'), resources_skipped: 1 } } as never)
  render(<KnowledgePermissionRefreshControls agentId="agent-1" actionRef={ref} disabled={false} onChanged={vi.fn()} onBusyChange={vi.fn()} />)
  await act(async () => ref.current?.refreshResource('row-kb'))
  await waitFor(() => expect(screen.getAllByText('No permission snapshots available; nothing was applied')).toHaveLength(2))
  expect(document.querySelector('.ant-notification-notice-icon-warning')).toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveClass('ant-alert-warning')
})

it.each([
  { resources_total: 2, resources_completed: 2, resources_skipped: 1 },
  { resources_total: 1, resources_completed: 1, resources_skipped: 0 },
  { resources_total: 0, resources_completed: 0, resources_skipped: 0 },
])('retains success when a snapshot was applied or there were no targets', async (counts) => {
  vi.mocked(refreshAgentKnowledgePermissions).mockResolvedValue({ data: { ...job('SUCCEEDED'), ...counts } } as never)
  renderControls()
  fireEvent.click(screen.getByRole('button', { name: 'Refresh all knowledge permissions for this Agent' }))
  await screen.findByText('Permissions refreshed')
  expect(screen.getByRole('status')).toHaveClass('ant-alert-success')
})
