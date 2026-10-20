import { beforeEach, expect, it, vi } from 'vitest'
import api from './client'
import { refreshAgentKnowledgePermissions } from './agents'
vi.mock('./client', () => ({ default: { post: vi.fn() } }))
beforeEach(() => vi.clearAllMocks())
it('uses bounded waiting only for a single resource and asynchronous requests for multiple resources', () => {
  refreshAgentKnowledgePermissions('agent', ['kb-a', 'kb-b'])
  expect(api.post).toHaveBeenLastCalledWith('/api/admin/agents/agent/knowledge-permissions/refresh', { knowledge_resource_ids: ['kb-a', 'kb-b'] }, expect.any(Object))
  refreshAgentKnowledgePermissions('agent', 'kb-a')
  expect(api.post).toHaveBeenLastCalledWith('/api/admin/agents/agent/knowledge-permissions/refresh', { knowledge_resource_ids: ['kb-a'], wait_timeout_seconds: 10 }, expect.any(Object))
  refreshAgentKnowledgePermissions('agent')
  expect(api.post).toHaveBeenLastCalledWith('/api/admin/agents/agent/knowledge-permissions/refresh', {}, expect.any(Object))
})
