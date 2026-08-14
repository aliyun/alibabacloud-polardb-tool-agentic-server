import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  issueMyAgentToken,
  listMyAgentConnections,
  revealMyAgentToken,
} from '../../api/agentConnections'
import MCPConnections from './MCPConnections'

vi.mock('../../api/agentConnections', () => ({
  issueMyAgentToken: vi.fn(),
  listMyAgentConnections: vi.fn(),
  regenerateMyAgentToken: vi.fn(),
  revealMyAgentToken: vi.fn(),
  revokeMyAgentToken: vi.fn(),
}))

const connection = {
  assignment_id: null,
  agent_id: 'agent-1',
  agent_name: 'Knowledge Agent',
  agent_status: 'active' as const,
  polarrag_instances: [{ id: 'rag-1', name: 'RAG' }],
  password_reveal_available: true,
  token: null,
}

const activeConnection = {
  ...connection,
  assignment_id: 'assignment-1',
  token: {
    token_prefix: 'pas_user_agent_example',
    status: 'active' as const,
    expires_at: null,
    last_used_at: null,
  },
}

describe('MCP connections', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(listMyAgentConnections).mockResolvedValue({
      data: [connection],
    } as never)
  })

  it('issues without exposing plaintext and reloads the masked status', async () => {
    vi.mocked(issueMyAgentToken).mockResolvedValue({
      data: {
        assignment_id: 'assignment-1',
        token_prefix: 'pas_user_agent_example',
        status: 'active',
        expires_at: null,
        last_used_at: null,
        token: null,
      },
    } as never)
    vi.mocked(listMyAgentConnections)
      .mockResolvedValueOnce({ data: [connection] } as never)
      .mockResolvedValueOnce({ data: [activeConnection] } as never)
    const user = userEvent.setup()
    render(<MCPConnections />)

    await user.click(await screen.findByRole('button', { name: 'Issue Token' }))
    await user.click(screen.getByRole('button', { name: 'Issue' }))

    expect(issueMyAgentToken).toHaveBeenCalledWith('agent-1', undefined)
    expect(await screen.findByText('pas_user_agent_••••••••')).toBeInTheDocument()
    expect(screen.queryByText(/pas_user_agent_plaintext/)).not.toBeInTheDocument()
  })

  it('copies an OIDC one-time token without rendering plaintext', async () => {
    const oidcConnection = {
      ...connection,
      password_reveal_available: false,
    }
    vi.mocked(listMyAgentConnections)
      .mockResolvedValueOnce({ data: [oidcConnection] } as never)
      .mockResolvedValueOnce({
        data: [{ ...activeConnection, password_reveal_available: false }],
      } as never)
    vi.mocked(issueMyAgentToken).mockResolvedValue({
      data: {
        assignment_id: 'assignment-1',
        token_prefix: 'pas_user_agent_example',
        status: 'active',
        expires_at: null,
        last_used_at: null,
        token: 'pas_user_agent_oidc_once',
      },
    } as never)
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    const user = userEvent.setup()
    render(<MCPConnections />)

    await user.click(await screen.findByRole('button', { name: 'Issue Token' }))
    await user.click(screen.getByRole('button', { name: 'Issue' }))

    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith('pas_user_agent_oidc_once'),
    )
    expect(issueMyAgentToken).toHaveBeenCalledWith('agent-1', undefined)
    expect(screen.queryByText('pas_user_agent_oidc_once')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Copy Token' })).not.toBeInTheDocument()
  })

  it('sends an optional expiration when issuing a Token', async () => {
    vi.mocked(issueMyAgentToken).mockResolvedValue({ data: {} } as never)
    const user = userEvent.setup()
    render(<MCPConnections />)

    await user.click(await screen.findByRole('button', { name: 'Issue Token' }))
    const expiration = '2030-01-02T03:04'
    await user.type(
      screen.getByLabelText('Token expiration (optional)'),
      expiration,
    )
    await user.click(screen.getByRole('button', { name: 'Issue' }))

    expect(issueMyAgentToken).toHaveBeenCalledWith(
      'agent-1',
      new Date(expiration).toISOString(),
    )
  })

  it('requires the user password and copies JSON without rendering plaintext', async () => {
    vi.mocked(listMyAgentConnections).mockResolvedValue({
      data: [activeConnection],
    } as never)
    vi.mocked(revealMyAgentToken).mockResolvedValue({
      data: { token: 'pas_user_agent_revealed' },
    } as never)
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    const user = userEvent.setup()
    render(<MCPConnections />)

    await user.click(
      await screen.findByRole('button', { name: /copy json configuration/i }),
    )
    await user.type(screen.getByLabelText(/current password/i), 'password')
    await user.click(screen.getByRole('button', { name: /^copy$/i }))

    await waitFor(() =>
      expect(revealMyAgentToken).toHaveBeenCalledWith('agent-1', 'password'),
    )
    expect(writeText).toHaveBeenCalledWith(
      expect.stringContaining('Bearer pas_user_agent_revealed'),
    )
    expect(screen.queryByText('pas_user_agent_revealed')).not.toBeInTheDocument()
  })

  it('falls back to transient DOM copying when Clipboard API is blocked', async () => {
    vi.mocked(listMyAgentConnections).mockResolvedValue({
      data: [activeConnection],
    } as never)
    vi.mocked(revealMyAgentToken).mockResolvedValue({
      data: { token: 'pas_user_agent_revealed' },
    } as never)
    const user = userEvent.setup()
    vi.spyOn(navigator.clipboard, 'writeText').mockRejectedValue(
      new DOMException('Clipboard access denied', 'NotAllowedError'),
    )
    const execCommand = vi.fn(() => true)
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: execCommand,
    })
    render(<MCPConnections />)

    await user.click(await screen.findByRole('button', { name: 'Copy Token' }))
    await user.type(screen.getByLabelText(/current password/i), 'password')
    await user.click(screen.getByRole('button', { name: /^copy$/i }))

    expect(await screen.findByText('Agent Token copied.')).toBeInTheDocument()
    expect(execCommand).toHaveBeenCalledWith('copy')
    expect(document.querySelector('textarea')).toBeNull()
    expect(screen.queryByText('pas_user_agent_revealed')).not.toBeInTheDocument()
  })
})
