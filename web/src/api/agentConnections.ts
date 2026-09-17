import api from './client'
import type { Page, PageParams } from './pagination'

export interface MyAgentTokenSummary {
  token_prefix: string
  status: 'active' | 'revoked' | 'expired'
  expires_at: string | null
  last_used_at: string | null
}

export interface MyAgentConnection {
  assignment_id: string | null
  agent_id: string
  agent_name: string
  agent_status: 'active' | 'disabled'
  polarrag_instances: { id: string; name: string }[]
  password_reveal_available: boolean
  token: MyAgentTokenSummary | null
}

export interface MyAgentToken extends MyAgentTokenSummary {
  assignment_id: string
  token: string | null
}

export type UserWorkspaceStatus =
  | 'ready'
  | 'selection_required'
  | 'no_agent_access'
  | 'default_agent_unavailable'

export interface UserWorkspace {
  id: string
  status: UserWorkspaceStatus
  default_agent: { id: string; name: string } | null
  available_agents: { id: string; name: string }[]
}

const path = (connectionId: string, operation: string) =>
  `/api/me/agent-connections/${encodeURIComponent(connectionId)}/token/${operation}`

export const listMyAgentConnections = (params: PageParams = {}) =>
  api.get<Page<MyAgentConnection>>('/api/me/agent-connections', { params })

export const getMyWorkspace = () =>
  api.get<UserWorkspace>('/api/me/workspace')

export const selectMyDefaultAgent = (agentId: string) =>
  api.put<UserWorkspace>('/api/me/workspace/default-agent', {
    agent_id: agentId,
  })

export const issueMyAgentToken = (
  connectionId: string,
  expiresAt?: string,
) =>
  api.post<MyAgentToken>(
    path(connectionId, 'issue'),
    expiresAt ? { expires_at: expiresAt } : {},
  )

export const revealMyAgentToken = (connectionId: string) =>
  api.post<MyAgentToken>(path(connectionId, 'reveal'))

export const regenerateMyAgentToken = (
  connectionId: string,
  expiresAt?: string,
) =>
  api.post<MyAgentToken>(path(connectionId, 'regenerate'), {
    confirmed: true,
    ...(expiresAt ? { expires_at: expiresAt } : {}),
  })

export const revokeMyAgentToken = (connectionId: string) =>
  api.post<MyAgentToken>(path(connectionId, 'revoke'))
