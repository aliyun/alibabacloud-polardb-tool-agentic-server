import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  getMyWorkspace,
  issueMyAgentToken,
  listMyAgentConnections,
  regenerateMyAgentToken,
  revealMyAgentToken,
  selectMyDefaultAgent,
} from '../../api/agentConnections'
import MCPConnections from './MCPConnections'

const features = vi.hoisted(() => ({ knowledge: true }))
vi.mock('../../hooks/useFeatures', () => ({ useFeatures: () => features }))

vi.mock('../../api/agentConnections', () => ({
  getMyWorkspace: vi.fn(),
  issueMyAgentToken: vi.fn(),
  listMyAgentConnections: vi.fn(),
  regenerateMyAgentToken: vi.fn(),
  revealMyAgentToken: vi.fn(),
  revokeMyAgentToken: vi.fn(),
  selectMyDefaultAgent: vi.fn(),
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

const workspace = {
  id: 'workspace-1',
  status: 'ready' as const,
  default_agent: {
    id: connection.agent_id,
    name: connection.agent_name,
  },
  available_agents: [{
    id: connection.agent_id,
    name: connection.agent_name,
  }],
}

function pagedConnections(items: unknown[]) {
  return {
    data: { items, total: items.length, offset: 0, limit: 20 },
  } as never
}

describe('MCP connections', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    features.knowledge = true
    vi.mocked(listMyAgentConnections).mockResolvedValue(pagedConnections([connection]))
    vi.mocked(getMyWorkspace).mockResolvedValue({ data: workspace } as never)
  })

  it('keeps personal MCP access available without knowledge bindings', async () => {
    features.knowledge = false
    vi.mocked(listMyAgentConnections).mockResolvedValue(pagedConnections([{ ...connection, polarrag_instances: [] }]))
    render(<MCPConnections />)
    expect(await screen.findByRole('button', { name: 'Issue Token' })).toBeEnabled()
    expect(getMyWorkspace).not.toHaveBeenCalled()
    expect(screen.queryByRole('columnheader', { name: 'PolarRAG Instances' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /knowledge bases/i })).not.toBeInTheDocument()
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
      .mockResolvedValueOnce(pagedConnections([connection]))
      .mockResolvedValueOnce(pagedConnections([activeConnection]))
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
      .mockResolvedValueOnce(pagedConnections([oidcConnection]))
      .mockResolvedValueOnce(
        pagedConnections([{ ...activeConnection, password_reveal_available: false }]),
      )
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

    expect(issueMyAgentToken).toHaveBeenCalledWith('agent-1', undefined)
    expect(writeText).not.toHaveBeenCalled()
    await user.click(
      within(await screen.findByRole('dialog')).getByRole('button', {
        name: 'Copy Token',
      }),
    )
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith('pas_user_agent_oidc_once'),
    )
    expect(screen.queryByText('pas_user_agent_oidc_once')).not.toBeInTheDocument()
  })

  it('requires confirmation before regenerating and offers a JSON copy', async () => {
    const regeneratedToken = ['pas_user_agent', 'regenerated_once'].join('_')
    const oidcConnection = {
      ...activeConnection,
      password_reveal_available: false,
    }
    vi.mocked(listMyAgentConnections)
      .mockResolvedValueOnce(pagedConnections([oidcConnection]))
      .mockResolvedValueOnce(pagedConnections([oidcConnection]))
    vi.mocked(regenerateMyAgentToken).mockResolvedValue({
      data: {
        assignment_id: 'assignment-1',
        token_prefix: 'pas_user_agent_example',
        status: 'active',
        expires_at: null,
        last_used_at: null,
        token: regeneratedToken,
      },
    } as never)
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    const user = userEvent.setup()
    render(<MCPConnections />)

    await user.click(await screen.findByRole('button', { name: 'Regenerate' }))

    expect(
      screen.getByText(/previous token becomes invalid immediately/i),
    ).toHaveClass('ant-typography-danger')
    expect(regenerateMyAgentToken).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Confirm regenerate' }))

    expect(regenerateMyAgentToken).toHaveBeenCalledWith('agent-1', undefined)
    await user.click(
      within(await screen.findByRole('dialog')).getByRole('button', {
        name: 'Copy JSON configuration',
      }),
    )
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(
        expect.stringContaining(`Bearer ${regeneratedToken}`),
      ),
    )
    expect(screen.queryByText(regeneratedToken)).not.toBeInTheDocument()
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

  it('offers direct Token copying for an active connection', async () => {
    vi.mocked(listMyAgentConnections).mockResolvedValue(pagedConnections([activeConnection]))
    render(<MCPConnections />)

    await screen.findByRole('button', { name: 'Copy Token' })
    expect(screen.getByRole('button', { name: 'Copy Token' })).toBeInTheDocument()
  })

  it('copies JSON with the authenticated session without rendering plaintext', async () => {
    vi.mocked(listMyAgentConnections).mockResolvedValue(pagedConnections([activeConnection]))
    vi.mocked(revealMyAgentToken).mockResolvedValue({
      data: { token: 'pas_user_agent_revealed' },
    } as never)
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    const user = userEvent.setup()
    render(<MCPConnections />)

    await user.click(
      await screen.findByRole('button', { name: /copy json configuration/i }),
    )

    await waitFor(() =>
      expect(revealMyAgentToken).toHaveBeenCalledWith('agent-1'),
    )
    expect(writeText).toHaveBeenCalledWith(
      expect.stringContaining('Bearer pas_user_agent_revealed'),
    )
    expect(screen.queryByText('pas_user_agent_revealed')).not.toBeInTheDocument()
  })

  it('falls back to transient DOM copying when Clipboard API is blocked', async () => {
    vi.mocked(listMyAgentConnections).mockResolvedValue(pagedConnections([activeConnection]))
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

    expect(await screen.findByText('Agent Token copied.')).toBeInTheDocument()
    expect(execCommand).toHaveBeenCalledWith('copy')
    expect(document.querySelector('textarea')).toBeNull()
    expect(screen.queryByText('pas_user_agent_revealed')).not.toBeInTheDocument()
  })

  it('selects an authorized Agent as the workspace default', async () => {
    const secondConnection = {
      ...connection,
      agent_id: 'agent-2',
      agent_name: 'Analytics Agent',
    }
    vi.mocked(listMyAgentConnections).mockResolvedValue(
      pagedConnections([connection, secondConnection]),
    )
    vi.mocked(getMyWorkspace).mockResolvedValue({
      data: {
        ...workspace,
        status: 'selection_required',
        default_agent: null,
        available_agents: [
          ...workspace.available_agents,
          { id: 'agent-2', name: 'Analytics Agent' },
        ],
      },
    } as never)
    vi.mocked(selectMyDefaultAgent).mockResolvedValue({
      data: {
        ...workspace,
        default_agent: { id: 'agent-2', name: 'Analytics Agent' },
      },
    } as never)
    const user = userEvent.setup()
    render(<MCPConnections />)

    await user.click(await screen.findByRole('radio', {
      name: 'Use Analytics Agent as the default Agent',
    }))

    expect(selectMyDefaultAgent).toHaveBeenCalledWith('agent-2')
    expect(await screen.findByText('Default Agent updated.')).toBeInTheDocument()
  })

  it('tolerates an older workspace response without available Agents', async () => {
    vi.mocked(getMyWorkspace).mockResolvedValue({
      data: {
        id: 'workspace-1',
        status: 'ready',
        default_agent: null,
      },
    } as never)

    render(<MCPConnections />)

    expect(
      await screen.findByRole('button', {
        name: 'Knowledge bases for Knowledge Agent',
      }),
    ).toBeEnabled()
    expect(
      screen.getByRole('radio', {
        name: 'Use Knowledge Agent as the default Agent',
      }),
    ).toBeDisabled()
  })
})
