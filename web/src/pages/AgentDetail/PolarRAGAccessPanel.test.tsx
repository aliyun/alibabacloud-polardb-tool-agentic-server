import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { listPolarRAGInstances } from '../../api/polarrag'
import PolarRAGAccessPanel from './PolarRAGAccessPanel'

vi.mock('../../api/polarrag', () => ({ listPolarRAGInstances: vi.fn() }))
vi.mock('./InstanceBindingStep', () => ({ default: () => <div>Instance step content</div> }))
vi.mock('./SubjectAssignmentsStep', () => ({ default: () => <div>Subject step content</div> }))
vi.mock('./KnowledgeBindingsPanel', () => ({ default: () => <div>Knowledge step content</div> }))

describe('Agent PolarRAG access panel', () => {
  beforeEach(() => {
    vi.mocked(listPolarRAGInstances).mockResolvedValue({ data: { items: [] } } as never)
  })

  it('organizes access configuration into the approved three-step flow', async () => {
    const user = userEvent.setup()
    render(<PolarRAGAccessPanel agentId="agent-1" bindings={[]} onBindingsChange={vi.fn()} />)

    expect(await screen.findByRole('tab', { name: /1 Bind instances/ })).toBeInTheDocument()
    expect(screen.getByText('Instance step content')).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: /2 Configure groups and users/ }))
    expect(screen.getByText('Subject step content')).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: /3 Bind knowledge bases/ }))
    expect(screen.getByText('Knowledge step content')).toBeInTheDocument()
  })
})
