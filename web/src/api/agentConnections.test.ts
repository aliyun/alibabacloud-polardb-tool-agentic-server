import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from './client'
import { revealMyAgentToken } from './agentConnections'

vi.mock('./client', () => ({
  default: { post: vi.fn() },
}))

describe('Agent connection API', () => {
  beforeEach(() => vi.clearAllMocks())

  it('reveals a user Agent Token with the authenticated session', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: { token: null } } as never)

    await revealMyAgentToken('agent/1')

    expect(api.post).toHaveBeenCalledWith(
      '/api/me/agent-connections/agent%2F1/token/reveal',
    )
  })
})
