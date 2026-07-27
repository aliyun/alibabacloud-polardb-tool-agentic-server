import api from './client'

export type AgentStatus = 'active' | 'disabled'
export type AgentTokenStatus = 'active' | 'revoked' | 'expired'

export interface AgentTokenSummary {
  id: string
  token_prefix: string
  status: AgentTokenStatus
  expires_at: string | null
  revoked_at: string | null
  last_used_at: string | null
  created_at: string
  updated_at: string | null
}

export interface Agent {
  id: string
  name: string
  description: string | null
  status: AgentStatus
  max_active_resources: number | null
  created_by: string | null
  created_at: string
  updated_at: string | null
  token_summary: AgentTokenSummary | null
}

export interface AgentInput {
  name: string
  description?: string | null
  max_active_resources?: number | null
}

export interface AgentUpdate {
  name?: string
  description?: string | null
  status?: AgentStatus
  max_active_resources?: number | null
}

export interface AgentCreated extends Agent {
  token_id: string
  token_prefix: string
  token_expires_at: string | null
  token: string
}

export interface AgentToken {
  id: string
  agent_id: string
  token_prefix: string
  expires_at: string | null
  revoked_at: string | null
  last_used_at: string | null
  created_at: string
  updated_at: string | null
  token: string | null
}

export interface AgentTokenRevealRequest {
  confirmed: true
}

export const listAgents = () => api.get<Agent[]>('/api/agents')

export const getAgent = (agentId: string) =>
  api.get<Agent>(`/api/agents/${encodeURIComponent(agentId)}`)

export const createAgent = (input: AgentInput) =>
  api.post<AgentCreated>('/api/agents', input)

export const updateAgent = (agentId: string, input: AgentUpdate) =>
  api.patch<Agent>(`/api/agents/${encodeURIComponent(agentId)}`, input)

export const regenerateAgentToken = (
  agentId: string,
  expiresAt: string | null = null,
) =>
  api.post<AgentToken>(
    `/api/agents/${encodeURIComponent(agentId)}/token/regenerate`,
    { expires_at: expiresAt },
  )

export const revealAgentToken = (
  agentId: string,
  request: AgentTokenRevealRequest,
) =>
  api.post<AgentToken>(
    `/api/agents/${encodeURIComponent(agentId)}/token/reveal`,
    request,
  )

export const revokeAgentToken = (agentId: string) =>
  api.post<AgentToken>(
    `/api/agents/${encodeURIComponent(agentId)}/token/revoke`,
  )
