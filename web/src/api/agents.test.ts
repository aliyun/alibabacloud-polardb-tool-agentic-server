import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from './client'
import {
  listAgentKnowledgeBindings,
  listAgentKnowledgeResourceOptions,
  revealAgentToken,
  updateAgentKnowledgeBindingsBatch,
} from './agents'

vi.mock('./client', () => ({
  default: { get: vi.fn(), post: vi.fn() },
}))

describe('Agent API', () => {
  beforeEach(() => vi.clearAllMocks())

  it('reveals an Agent Token with the authenticated admin session', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: { token: null } } as never)

    await revealAgentToken('agent/1')

    expect(api.post).toHaveBeenCalledWith(
      '/api/agents/agent%2F1/token/reveal',
    )
  })

  it('uses the admin knowledge-binding endpoints with encoded Agent ids', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: { items: [] } } as never)
    vi.mocked(api.post).mockResolvedValue({ data: {} } as never)

    await listAgentKnowledgeBindings('agent/1', {
      offset: 20,
      limit: 20,
      search: 'alice',
    })
    await listAgentKnowledgeResourceOptions('agent/1', {
      offset: 0,
      limit: 50,
      search: 'finance',
    })
    await updateAgentKnowledgeBindingsBatch('agent/1', {
      operations: [
        {
          operation: 'BIND',
          subjects: [{ type: 'USER', user_id: 'user-1' }],
          targets: [{ space_id: 'space-1', kb_id: 'kb-1' }],
        },
      ],
      activate_scoped_mode: true,
    })

    expect(api.get).toHaveBeenNthCalledWith(
      1,
      '/api/admin/agents/agent%2F1/knowledge-bindings',
      { params: { offset: 20, limit: 20, search: 'alice' } },
    )
    expect(api.get).toHaveBeenNthCalledWith(
      2,
      '/api/admin/agents/agent%2F1/knowledge-resource-options',
      { params: { offset: 0, limit: 50, search: 'finance' } },
    )
    expect(api.post).toHaveBeenCalledWith(
      '/api/admin/agents/agent%2F1/knowledge-bindings:batch',
      {
        operations: [
          {
            operation: 'BIND',
            subjects: [{ type: 'USER', user_id: 'user-1' }],
            targets: [{ space_id: 'space-1', kb_id: 'kb-1' }],
          },
        ],
        activate_scoped_mode: true,
      },
    )
  })
})
