import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createAgentGroupAssignment,
  deleteAgentUserAssignment,
  getAgentAllSubjectAssignments,
  listAgentGroupAssignments,
  listAgentGroupOptions,
  listAgentUserOptions,
} from '../../api/agents'
import SubjectAssignmentsStep from './SubjectAssignmentsStep'

vi.mock('../../api/agents', () => ({
  createAgentGroupAssignment: vi.fn(),
  getAgentAllSubjectAssignments: vi.fn(),
  updateAgentAllSubjectAssignments: vi.fn(),
  createAgentUserAssignment: vi.fn(),
  deleteAgentGroupAssignment: vi.fn(),
  deleteAgentUserAssignment: vi.fn(),
  listAgentGroupAssignments: vi.fn(),
  listAgentGroupOptions: vi.fn(),
  listAgentUserAssignments: vi.fn(),
  listAgentUserOptions: vi.fn(),
}))

describe('Agent subject assignments step', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(getAgentAllSubjectAssignments).mockResolvedValue({ data: {
      status: 'UNBOUND', eligible_source_count: 1, eligible_user_count: 0, assignment_count: 0,
    } } as never)
    vi.mocked(listAgentGroupOptions).mockResolvedValue({
      data: {
        items: [
          {
            group_kind: 'identity_source_all',
            department_id: null,
            department_name: null,
            identity_domain: null,
            provider: null,
            identity_source_id: 'source-1',
            identity_source_name: 'Feishu directory',
            external_group_id: null,
            external_group_name: null,
            principal_id: null,
            member_count: 21,
          },
        ],
        total: 1,
        offset: 0,
        limit: 20,
      },
    } as never)
    vi.mocked(listAgentGroupAssignments).mockResolvedValue({
      data: { items: [], total: 0, offset: 0, limit: 20 },
    } as never)
    vi.mocked(createAgentGroupAssignment).mockResolvedValue({ data: {} } as never)
  })

  it('filters unbound identity sources and binds only selected rows', async () => {
    const user = userEvent.setup()
    render(<SubjectAssignmentsStep agentId="agent-1" />)

    expect((await screen.findAllByText('Feishu directory')).length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: 'Unbind all users' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Unbind selected' })).not.toBeInTheDocument()
    expect(listAgentGroupOptions).toHaveBeenCalledWith('agent-1', {
      category: 'identity_source',
      limit: 20,
      offset: 0,
      search: undefined,
    })

    await user.click(screen.getByRole('button', { name: 'Select current page' }))
    expect(
      screen.getByRole('button', { name: 'Clear current page selection' }),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Bind selected' }))

    await waitFor(() =>
      expect(createAgentGroupAssignment).toHaveBeenCalledWith(
        'agent-1',
        expect.objectContaining({
          group_kind: 'identity_source_all',
          identity_source_id: 'source-1',
        }),
      ),
    )
  })

  it('warns before removing a direct binding that also inherits membership', async () => {
    vi.mocked(listAgentUserOptions).mockResolvedValue({ data: {
      items: [{ id: 'user-1', display_name: 'Local user', assignment_id: 'direct-1',
        identity_source_names: ['PAS'], inherited_source_names: ['PAS'], availability: 'disabled' }],
      total: 1, offset: 0, limit: 20,
    } } as never)
    vi.mocked(deleteAgentUserAssignment).mockResolvedValue({} as never)
    const user = userEvent.setup()
    render(<SubjectAssignmentsStep agentId="agent-1" />)
    await screen.findAllByText('Feishu directory')
    await user.click(screen.getByRole('tab', { name: 'Users' }))
    await user.click(screen.getByRole('radio', { name: 'Bound' }).closest('label')!)
    await screen.findByText('Local user')
    expect(screen.getByText('Disabled')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Select current page' }))
    await user.click(screen.getByRole('button', { name: 'Unbind selected' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/This removes only the selected direct bindings/)).toBeInTheDocument()
    expect(deleteAgentUserAssignment).not.toHaveBeenCalled()
    await user.click(within(dialog).getByRole('button', { name: 'Unbind selected' }))
    await waitFor(() => expect(deleteAgentUserAssignment).toHaveBeenCalledWith('agent-1', 'direct-1'))
  })
})
