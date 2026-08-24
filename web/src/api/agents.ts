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
  password: string
}

export type AgentGroupKind =
  | 'department'
  | 'enterprise'
  | 'identity_source'
  | 'identity_source_all'

export interface AgentGroupOption {
  group_kind: AgentGroupKind
  department_id: string | null
  department_name: string | null
  identity_domain: string | null
  provider: string | null
  identity_source_id: string | null
  identity_source_name: string | null
  external_group_id: string | null
  external_group_name: string | null
  principal_id: string | null
  member_count: number
}

export interface AgentGroupAssignment extends AgentGroupOption {
  id: string
  created_at: string
}

export interface AgentPolarRAGBinding {
  id: string
  polarrag_instance_id: string
  instance_name: string
  public_knowledge_resource_ids: string[] | null
  created_at: string
}

export interface AgentPolarRAGPublicResource {
  knowledge_resource_id: string
  name: string
  knowledge_space_name: string
}

export interface AgentUserTokenSummary {
  token_prefix: string
  status: AgentTokenStatus
  last_used_at: string | null
  created_at: string
}

export interface AgentUserAssignment {
  id: string
  user_id: string
  user_name: string
  user_status: string
  token: AgentUserTokenSummary | null
  created_at: string
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
    { pasSkipAuthRedirect: true },
  )

export const revokeAgentToken = (agentId: string) =>
  api.post<AgentToken>(
    `/api/agents/${encodeURIComponent(agentId)}/token/revoke`,
  )

export const listAgentPolarRAGBindings = (agentId: string) =>
  api.get<AgentPolarRAGBinding[]>(
    `/api/agents/${encodeURIComponent(agentId)}/polarrag-bindings`,
  )

export const createAgentPolarRAGBinding = (
  agentId: string,
  polarragInstanceId: string,
) =>
  api.post<AgentPolarRAGBinding>(
    `/api/agents/${encodeURIComponent(agentId)}/polarrag-bindings`,
    { polarrag_instance_id: polarragInstanceId },
  )

export const listAgentPolarRAGPublicResources = (
  agentId: string,
  bindingId: string,
) =>
  api.get<AgentPolarRAGPublicResource[]>(
    `/api/agents/${encodeURIComponent(agentId)}/polarrag-bindings/${encodeURIComponent(bindingId)}/public-resources`,
  )

export const updateAgentPolarRAGPublicResources = (
  agentId: string,
  bindingId: string,
  publicKnowledgeResourceIds: string[] | null,
) =>
  api.put<AgentPolarRAGBinding>(
    `/api/agents/${encodeURIComponent(agentId)}/polarrag-bindings/${encodeURIComponent(bindingId)}/public-resources`,
    { public_knowledge_resource_ids: publicKnowledgeResourceIds },
  )

export const deleteAgentPolarRAGBinding = (
  agentId: string,
  bindingId: string,
) =>
  api.delete(
    `/api/agents/${encodeURIComponent(agentId)}/polarrag-bindings/${encodeURIComponent(bindingId)}`,
  )

export const listAgentUserAssignments = (agentId: string) =>
  api.get<AgentUserAssignment[]>(
    `/api/agents/${encodeURIComponent(agentId)}/user-assignments`,
  )

export const createAgentUserAssignment = (
  agentId: string,
  userId: string,
) =>
  api.post<AgentUserAssignment>(
    `/api/agents/${encodeURIComponent(agentId)}/user-assignments`,
    { user_id: userId },
  )

export const deleteAgentUserAssignment = (
  agentId: string,
  assignmentId: string,
) =>
  api.delete(
    `/api/agents/${encodeURIComponent(agentId)}/user-assignments/${encodeURIComponent(assignmentId)}`,
  )

export const forceRevokeAgentUserToken = (
  agentId: string,
  assignmentId: string,
) =>
  api.post<AgentUserAssignment>(
    `/api/agents/${encodeURIComponent(agentId)}/user-assignments/${encodeURIComponent(assignmentId)}/token/revoke`,
  )

export const listAgentGroupOptions = (agentId: string) =>
  api.get<AgentGroupOption[]>(
    `/api/agents/${encodeURIComponent(agentId)}/group-options`,
  )

export const listAgentGroupAssignments = (agentId: string) =>
  api.get<AgentGroupAssignment[]>(
    `/api/agents/${encodeURIComponent(agentId)}/group-assignments`,
  )

export const createAgentGroupAssignment = (
  agentId: string,
  group: AgentGroupOption,
) =>
  api.post<AgentGroupAssignment>(
    `/api/agents/${encodeURIComponent(agentId)}/group-assignments`,
    {
      group_kind: group.group_kind,
      department_id: group.department_id,
      identity_domain: group.identity_domain,
      provider: group.provider,
      identity_source_id: group.identity_source_id,
      principal_id: group.principal_id,
    },
  )

export const deleteAgentGroupAssignment = (
  agentId: string,
  assignmentId: string,
) =>
  api.delete(
    `/api/agents/${encodeURIComponent(agentId)}/group-assignments/${encodeURIComponent(assignmentId)}`,
  )
