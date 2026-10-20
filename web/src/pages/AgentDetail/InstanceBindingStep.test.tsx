import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'

import { createAgentPolarRAGBinding, deleteAgentPolarRAGBinding, listAgentPolarRAGBindings } from '../../api/agents'
import { listPolarRAGInstances } from '../../api/polarrag'
import InstanceBindingStep from './InstanceBindingStep'

vi.mock('../../api/polarrag', () => ({ listPolarRAGInstances: vi.fn() }))
vi.mock('../../api/agents', () => ({
  createAgentPolarRAGBinding: vi.fn(),
  deleteAgentPolarRAGBinding: vi.fn(),
  listAgentPolarRAGBindings: vi.fn(),
}))

beforeEach(() => vi.clearAllMocks())

it('keeps the newest instance search results when responses arrive out of order', async () => {
  const resolve: ((value: unknown) => void)[] = []
  vi.mocked(listPolarRAGInstances).mockImplementation(
    () => new Promise((done) => resolve.push(done)) as never,
  )
  const result = (name: string) => ({
    data: { items: [{ id: name, name, status: 'active' }], total: 1 },
  })
  render(<InstanceBindingStep agentId="agent-1" bindings={[]} onBindingsChange={vi.fn()} />)
  await waitFor(() => expect(resolve).toHaveLength(1))
  await act(async () => resolve[0](result('Initial')))
  fireEvent.click(screen.getByRole('button', { name: 'Select current page instances' }))
  expect(screen.getByRole('button', { name: 'Bind selected instances' })).toBeEnabled()
  fireEvent.change(screen.getByPlaceholderText('Search instances'), { target: { value: 'Old' } })
  expect(screen.getByRole('button', { name: 'Bind selected instances' })).toBeDisabled()
  await waitFor(() => expect(resolve).toHaveLength(2))
  fireEvent.change(screen.getByPlaceholderText('Search instances'), { target: { value: 'New' } })
  await waitFor(() => expect(resolve).toHaveLength(3))
  await act(async () => resolve[2](result('New result')))
  expect(screen.getAllByText('New result').length).toBeGreaterThan(0)
  await act(async () => resolve[1](result('Old result')))
  expect(screen.queryAllByText('Old result')).toHaveLength(0)
})

it('shows the first page after binding an instance from a later page', async () => {
  vi.mocked(listPolarRAGInstances).mockImplementation(async (params) => ({
    data: {
      items: [{ id: params?.offset === 0 ? 'first' : 'second', name: params?.offset === 0 ? 'First instance' : 'Second instance', status: 'active' }],
      total: 21,
    },
  }) as never)
  vi.mocked(createAgentPolarRAGBinding).mockResolvedValue({ data: {} } as never)
  vi.mocked(listAgentPolarRAGBindings).mockResolvedValue({ data: { items: [] } } as never)
  render(<InstanceBindingStep agentId="agent-1" bindings={[]} onBindingsChange={vi.fn()} />)
  expect((await screen.findAllByText('First instance')).length).toBeGreaterThan(0)

  fireEvent.click(screen.getByTitle('Next Page'))
  expect((await screen.findAllByText('Second instance')).length).toBeGreaterThan(0)
  fireEvent.click(screen.getByRole('button', { name: 'Select current page instances' }))
  fireEvent.click(screen.getByRole('button', { name: 'Bind selected instances' }))

  await waitFor(() => expect(createAgentPolarRAGBinding).toHaveBeenCalledWith('agent-1', 'second'))
  await waitFor(() => expect(screen.getAllByText('First instance').length).toBeGreaterThan(0))
  expect(screen.queryAllByText('Second instance')).toHaveLength(0)
})

it('shows the first page after unbinding an instance from a later page', async () => {
  vi.mocked(listAgentPolarRAGBindings).mockImplementation(async (_agentId, params) => ({
    data: {
      items: [{
        id: params?.offset === 0 ? 'first-binding' : 'second-binding',
        instance_name: params?.offset === 0 ? 'First bound instance' : 'Second bound instance',
        polarrag_instance_id: params?.offset === 0 ? 'first' : 'second',
      }],
      total: 21,
    },
  }) as never)
  vi.mocked(deleteAgentPolarRAGBinding).mockResolvedValue({ data: {} } as never)
  render(<InstanceBindingStep agentId="agent-1" bindings={[]} onBindingsChange={vi.fn()} />)
  fireEvent.click(screen.getByRole('tab', { name: 'Bound instances' }))
  expect((await screen.findAllByText('First bound instance')).length).toBeGreaterThan(0)

  fireEvent.click(screen.getByTitle('Next Page'))
  expect((await screen.findAllByText('Second bound instance')).length).toBeGreaterThan(0)
  fireEvent.click(screen.getByRole('button', { name: 'Select current page instances' }))
  fireEvent.click(screen.getByRole('button', { name: 'Unbind selected instances' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Confirm unbinding' }))

  await waitFor(() => expect(deleteAgentPolarRAGBinding).toHaveBeenCalledWith('agent-1', 'second-binding'))
  await waitFor(() => expect(screen.getAllByText('First bound instance').length).toBeGreaterThan(0))
  expect(screen.queryAllByText('Second bound instance')).toHaveLength(0)
})

it('keeps the current search when binding refresh finishes later', async () => {
  let finishBindings: ((value: unknown) => void) | undefined
  vi.mocked(listPolarRAGInstances).mockImplementation(async (params) => ({
    data: {
      items: [{ id: params?.search ? 'new' : 'initial', name: params?.search ? 'New search result' : 'Initial result', status: 'active' }],
      total: 1,
    },
  }) as never)
  vi.mocked(createAgentPolarRAGBinding).mockResolvedValue({ data: {} } as never)
  vi.mocked(listAgentPolarRAGBindings).mockImplementation(
    () => new Promise((resolve) => { finishBindings = resolve }) as never,
  )
  render(<InstanceBindingStep agentId="agent-1" bindings={[]} onBindingsChange={vi.fn()} />)
  expect((await screen.findAllByText('Initial result')).length).toBeGreaterThan(0)
  fireEvent.click(screen.getByRole('button', { name: 'Select current page instances' }))
  fireEvent.click(screen.getByRole('button', { name: 'Bind selected instances' }))
  await waitFor(() => expect(finishBindings).toBeDefined())
  fireEvent.change(screen.getByPlaceholderText('Search instances'), { target: { value: 'New' } })
  expect((await screen.findAllByText('New search result')).length).toBeGreaterThan(0)

  await act(async () => finishBindings?.({ data: { items: [] } }))
  await waitFor(() => expect(screen.queryAllByText('Initial result')).toHaveLength(0))
  expect(screen.getAllByText('New search result').length).toBeGreaterThan(0)
})
