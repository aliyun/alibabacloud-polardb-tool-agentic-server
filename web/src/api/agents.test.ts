import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from './client'
import { revealAgentToken } from './agents'

vi.mock('./client', () => ({
  default: { post: vi.fn() },
}))

describe('Agent API', () => {
  beforeEach(() => vi.clearAllMocks())

  it('keeps password verification failures on the current page', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: { token: null } } as never)

    await revealAgentToken('agent/1', { password: 'wrong-password' })

    expect(api.post).toHaveBeenCalledWith(
      '/api/agents/agent%2F1/token/reveal',
      { password: 'wrong-password' },
      { pasSkipAuthRedirect: true },
    )
  })
})
