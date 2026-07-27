import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createAgent,
  listAgents,
  updateAgent,
} from '../../api/agents'
import Agents from './index'

vi.mock('../../api/agents', () => ({
  createAgent: vi.fn(),
  listAgents: vi.fn(),
  updateAgent: vi.fn(),
}))

const agent = {
  id: 'agent-1',
  name: 'production-reader',
  description: 'Reads production inventory',
  status: 'active' as const,
  max_active_resources: 4,
  created_by: 'admin-1',
  created_at: '2026-07-26T00:00:00Z',
  updated_at: null,
}

describe('Agents page', () => {
  beforeEach(() => {
    vi.mocked(listAgents).mockResolvedValue({ data: [agent] } as never)
    vi.mocked(updateAgent).mockResolvedValue({
      data: { ...agent, status: 'disabled' },
    } as never)
  })

  it('creates an Agent and keeps its initial token only in visible memory', async () => {
    const user = userEvent.setup()
    vi.mocked(createAgent).mockResolvedValue({
      data: {
        ...agent,
        id: 'agent-2',
        name: 'reporting-agent',
        token_id: 'token-2',
        token_prefix: 'pas_agent_abcd',
        token_expires_at: null,
        token: 'pas_agent_initial_plaintext',
      },
    } as never)

    render(
      <MemoryRouter>
        <Agents />
      </MemoryRouter>,
    )

    await user.click(
      await screen.findByRole('button', { name: /create agent/i }),
    )
    await user.type(screen.getByLabelText(/^name$/i), 'reporting-agent')
    await user.click(screen.getByRole('button', { name: /save agent/i }))

    expect(await screen.findByText('pas_agent_initial_plaintext')).toBeInTheDocument()
    expect(localStorage).toHaveLength(0)
    expect(sessionStorage).toHaveLength(0)

    await user.click(screen.getByRole('button', { name: /close token/i }))
    expect(screen.queryByText('pas_agent_initial_plaintext')).not.toBeInTheDocument()
  })

  it('changes status only after confirmation and asks clients to reconnect', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter>
        <Agents />
      </MemoryRouter>,
    )

    await user.click(
      await screen.findByRole('button', { name: /disable production-reader/i }),
    )
    expect(updateAgent).not.toHaveBeenCalled()
    expect(
      screen.getByText(/existing MCP sessions may keep an older tool list/i),
    ).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /^confirm disable$/i }))
    await waitFor(() =>
      expect(updateAgent).toHaveBeenCalledWith('agent-1', {
        status: 'disabled',
      }),
    )
    expect(await screen.findByRole('status')).toHaveTextContent(
      /reconnect the MCP client/i,
    )
  })

  it('shows an actionable empty state', async () => {
    vi.mocked(listAgents).mockResolvedValue({ data: [] } as never)
    render(
      <MemoryRouter>
        <Agents />
      </MemoryRouter>,
    )

    expect(
      await screen.findByText(/create an agent to issue a dedicated MCP identity/i),
    ).toBeInTheDocument()
  })
})
