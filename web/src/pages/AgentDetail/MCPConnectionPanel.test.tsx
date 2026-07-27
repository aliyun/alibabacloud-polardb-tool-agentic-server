import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import MCPConnectionPanel, {
  type MCPConnectionPanelProps,
} from './MCPConnectionPanel'

const baseProps: MCPConnectionPanelProps = {
  agentName: 'production-reader',
  mcpUrl: 'https://console.example.com/mcp',
  token: 'pas_agent_secret',
  loading: false,
  error: null,
  tokenStatus: 'active',
  expiresAt: null,
  lastUsedAt: null,
  onRetry: vi.fn(),
  onRegenerate: vi.fn(),
  onRevoke: vi.fn(),
}

describe('Agent MCP connection panel', () => {
  it('shows connection values and copies exact client JSON', async () => {
    const user = userEvent.setup()
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    render(<MCPConnectionPanel {...baseProps} />)

    expect(
      screen.getByRole('heading', { name: /mcp connection/i }),
    ).toBeInTheDocument()
    expect(screen.getByText(baseProps.mcpUrl)).toBeInTheDocument()
    expect(screen.getByText(baseProps.token!)).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /reveal credential/i }),
    ).not.toBeInTheDocument()

    await user.click(
      screen.getByRole('button', { name: /copy json configuration/i }),
    )

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
    expect(screen.getByRole('status')).toHaveTextContent(
      /json configuration copied/i,
    )
    expect(localStorage).toHaveLength(0)
    expect(sessionStorage).toHaveLength(0)
  })

  it('keeps a clipboard failure visible', async () => {
    const user = userEvent.setup()
    vi.spyOn(navigator.clipboard, 'writeText').mockRejectedValueOnce(
      new Error('permission denied'),
    )
    render(<MCPConnectionPanel {...baseProps} />)

    await user.click(
      screen.getByRole('button', { name: /copy json configuration/i }),
    )

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /could not copy json configuration/i,
    )
  })

  it.each([
    {
      name: 'loading',
      props: { loading: true, token: null },
    },
    {
      name: 'revoked',
      props: { tokenStatus: 'revoked' as const, token: null },
    },
    {
      name: 'expired',
      props: { tokenStatus: 'expired' as const, token: null },
    },
    {
      name: 'missing',
      props: { tokenStatus: null, token: null },
    },
  ])('disables JSON copy when the Token is $name', ({ props }) => {
    render(<MCPConnectionPanel {...baseProps} {...props} />)

    expect(
      screen.getByRole('button', { name: /copy json configuration/i }),
    ).toBeDisabled()
  })

  it('shows a persistent reveal error with a retry action', async () => {
    const user = userEvent.setup()
    const onRetry = vi.fn()
    render(
      <MCPConnectionPanel
        {...baseProps}
        token={null}
        error="Token reveal rate limit exceeded"
        onRetry={onRetry}
      />,
    )

    expect(screen.getByRole('alert')).toHaveTextContent(
      /token reveal rate limit exceeded/i,
    )
    await user.click(screen.getByRole('button', { name: /retry/i }))
    expect(onRetry).toHaveBeenCalledOnce()
  })
})
