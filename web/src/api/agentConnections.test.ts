import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from './client'
import { revealMyAgentToken } from './agentConnections'

vi.mock('./client', () => ({
  default: { post: vi.fn() },
}))

describe('Agent connection API', () => {
  beforeEach(() => vi.clearAllMocks())

  it('keeps password verification failures on the current page', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: { token: null } } as never)

    await revealMyAgentToken('agent/1', 'wrong-password')

    expect(api.post).toHaveBeenCalledWith(
      '/api/me/agent-connections/agent%2F1/token/reveal',
      { password: 'wrong-password' },
      { pasSkipAuthRedirect: true },
    )
  })
})
