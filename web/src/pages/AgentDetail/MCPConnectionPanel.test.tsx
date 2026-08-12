import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import MCPConnectionPanel, {
  type MCPConnectionPanelProps,
} from './MCPConnectionPanel'

const baseProps: MCPConnectionPanelProps = {
  agentName: 'production-reader',
  mcpUrl: 'https://console.example.com/mcp',
  tokenPrefix: 'pas_agent_abcd',
  tokenStatus: 'active',
  expiresAt: null,
  lastUsedAt: null,
  revealToken: vi.fn().mockResolvedValue('pas_agent_secret'),
  onRegenerate: vi.fn(),
  onRevoke: vi.fn(),
}

describe('Agent MCP connection panel', () => {
  it('keeps the Token masked and requires a password before copying JSON', async () => {
    const user = userEvent.setup()
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    const revealToken = vi.fn().mockResolvedValue('pas_agent_secret')
    render(
      <MCPConnectionPanel {...baseProps} revealToken={revealToken} />,
    )

    expect(screen.getByText('pas_agent_••••••••')).toBeInTheDocument()
    expect(screen.queryByText('pas_agent_secret')).not.toBeInTheDocument()

    await user.click(
      screen.getByRole('button', { name: /copy json configuration/i }),
    )
    expect(writeText).not.toHaveBeenCalled()
    await user.type(screen.getByLabelText(/current password/i), 'password')
    await user.click(
      within(screen.getByRole('dialog')).getByRole('button', {
        name: /^copy$/i,
      }),
    )

    expect(revealToken).toHaveBeenCalledWith('password')
    expect(writeText).toHaveBeenCalledWith(`{
  "mcpServers": {
    "production-reader": {
      "url": "https://console.example.com/mcp",
      "headers": {
        "Authorization": "Bearer pas_agent_secret"
      }
    }
  }
}
`)
    expect(screen.queryByText('pas_agent_secret')).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/copied/i)
  })

  it('does not copy when password verification fails', async () => {
    const user = userEvent.setup()
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    render(
      <MCPConnectionPanel
        {...baseProps}
        revealToken={vi.fn().mockRejectedValue(new Error('unauthorized'))}
      />,
    )

    await user.click(screen.getByRole('button', { name: /copy token/i }))
    await user.type(screen.getByLabelText(/current password/i), 'wrong')
    await user.click(
      within(screen.getByRole('dialog')).getByRole('button', {
        name: /^copy$/i,
      }),
    )

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /password verification failed/i,
    )
    expect(writeText).not.toHaveBeenCalled()
  })

  it.each([
    { name: 'revoked', tokenStatus: 'revoked' as const },
    { name: 'expired', tokenStatus: 'expired' as const },
    { name: 'missing', tokenStatus: null },
  ])('disables copy when the Token is $name', ({ tokenStatus }) => {
    render(
      <MCPConnectionPanel
        {...baseProps}
        tokenPrefix={tokenStatus ? baseProps.tokenPrefix : null}
        tokenStatus={tokenStatus}
      />,
    )

    expect(screen.getByRole('button', { name: /copy token/i })).toBeDisabled()
    expect(
      screen.getByRole('button', { name: /copy json configuration/i }),
    ).toBeDisabled()
  })
})
