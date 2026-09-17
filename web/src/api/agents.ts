import api from './client'
import type { Page, PageParams } from './pagination'

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
  oauth_redirect_uri?: string | null
  created_by: string | null
  created_at: string
  updated_at: string | null
  token_summary: AgentTokenSummary | null
}

export interface AgentInput {
  name: string
  description?: string | null
  max_active_resources?: number | null
  oauth_redirect_uri?: string | null
}

export interface AgentUpdate {
  name?: string
  description?: string | null
  status?: AgentStatus
  max_active_resources?: number | null
  oauth_redirect_uri?: string | null
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

export type { Page } from './pagination'

export interface AgentUserOption {
  id: string
  display_name: string
  external_id: string
  status: string
}

export type AgentKnowledgeScopeMode = 'LEGACY_ALL' | 'SCOPED'
export type AgentKnowledgeBindingOrigin = 'MANUAL' | 'EXTERNAL_SYNC'
export type AgentKnowledgeBindingSubjectType = 'USER' | 'DEPARTMENT' | 'GROUP'

export interface AgentKnowledgeBindingSubject {
  type: AgentKnowledgeBindingSubjectType
  user_id?: string | null
  department_id?: string | null
  identity_source_id?: string | null
  group_id?: string | null
  display_name?: string
  external_id?: string | null
}

export interface AgentKnowledgeResourceOption {
  knowledge_resource_id: string
  space_id: string | null
  space_name: string | null
  kb_id: string | null
  name: string | null
}

export interface AgentKnowledgeBinding {
  binding_id: string
  scope_id: string
  origin: AgentKnowledgeBindingOrigin
  identity_source_id: string | null
  external_scope_id: string | null
  subject: AgentKnowledgeBindingSubject & { display_name: string }
  knowledge_resources: AgentKnowledgeResourceOption[]
  created_at: string
  updated_at: string | null
}

export interface AgentKnowledgeBindingPage extends Page<AgentKnowledgeBinding> {
  knowledge_scope_mode: AgentKnowledgeScopeMode
}

export interface AgentKnowledgeBindingOperation {
  operation: 'BIND' | 'UNBIND'
  subjects: AgentKnowledgeBindingSubject[]
  targets: Array<
    | { knowledge_resource_id: string }
    | { space_id: string; kb_id: string }
  >
}

export interface AgentKnowledgeBindingBatchInput {
  operations: AgentKnowledgeBindingOperation[]
  activate_scoped_mode?: boolean
}

export interface BulkUserAssignmentStatus {
  status: 'running' | 'completed' | 'failed'
  created_count: number
  error: string | null
}

export const listAgents = (params: PageParams = {}) =>
  api.get<Page<Agent>>('/api/agents', { params })

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

export const revealAgentToken = (agentId: string) =>
  api.post<AgentToken>(
    `/api/agents/${encodeURIComponent(agentId)}/token/reveal`,
  )

export const revokeAgentToken = (agentId: string) =>
  api.post<AgentToken>(
    `/api/agents/${encodeURIComponent(agentId)}/token/revoke`,
  )

export const listAgentPolarRAGBindings = (
  agentId: string,
  params: PageParams = {},
) =>
  api.get<Page<AgentPolarRAGBinding>>(
    `/api/agents/${encodeURIComponent(agentId)}/polarrag-bindings`,
    { params },
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
  params: Pick<Page<unknown>, 'offset' | 'limit'> & { search?: string },
) =>
  api.get<Page<AgentPolarRAGPublicResource>>(
    `/api/agents/${encodeURIComponent(agentId)}/polarrag-bindings/${encodeURIComponent(bindingId)}/public-resources`,
    { params },
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

export const listAgentUserAssignments = (
  agentId: string,
  params: Pick<Page<unknown>, 'offset' | 'limit'> & { search?: string },
) =>
  api.get<Page<AgentUserAssignment>>(
    `/api/agents/${encodeURIComponent(agentId)}/user-assignments`,
    { params },
  )

export const listAgentUserOptions = (
  agentId: string,
  params: Pick<Page<unknown>, 'offset' | 'limit'> & { search?: string },
) =>
  api.get<Page<AgentUserOption>>(
    `/api/agents/${encodeURIComponent(agentId)}/user-options`,
    { params },
  )

export const createAgentUserAssignment = (
  agentId: string,
  userId: string,
) =>
  api.post<AgentUserAssignment>(
    `/api/agents/${encodeURIComponent(agentId)}/user-assignments`,
    { user_id: userId },
  )

export const createAllAgentUserAssignments = (agentId: string) =>
  api.post<BulkUserAssignmentStatus>(
    `/api/agents/${encodeURIComponent(agentId)}/user-assignments/bulk`,
  )

export const getAllAgentUserAssignmentStatus = (agentId: string) =>
  api.get<BulkUserAssignmentStatus>(
    `/api/agents/${encodeURIComponent(agentId)}/user-assignments/bulk/status`,
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

export const listAgentGroupOptions = (
  agentId: string,
  params: Pick<Page<unknown>, 'offset' | 'limit'> & { search?: string },
) =>
  api.get<Page<AgentGroupOption>>(
    `/api/agents/${encodeURIComponent(agentId)}/group-options`,
    { params },
  )

export const listAgentGroupAssignments = (
  agentId: string,
  params: Pick<Page<unknown>, 'offset' | 'limit'> & { search?: string },
) =>
  api.get<Page<AgentGroupAssignment>>(
    `/api/agents/${encodeURIComponent(agentId)}/group-assignments`,
    { params },
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

export const listAgentKnowledgeBindings = (
  agentId: string,
  params: PageParams & {
    origin?: AgentKnowledgeBindingOrigin
    subject_type?: AgentKnowledgeBindingSubjectType
  } = {},
) =>
  api.get<AgentKnowledgeBindingPage>(
    `/api/admin/agents/${encodeURIComponent(agentId)}/knowledge-bindings`,
    { params },
  )

export const listAgentKnowledgeResourceOptions = (
  agentId: string,
  params: PageParams = {},
) =>
  api.get<Page<AgentKnowledgeResourceOption>>(
    `/api/admin/agents/${encodeURIComponent(agentId)}/knowledge-resource-options`,
    { params },
  )

export const updateAgentKnowledgeBindingsBatch = (
  agentId: string,
  input: AgentKnowledgeBindingBatchInput,
) =>
  api.post<{
    agent_id: string
    operations_processed: number
    knowledge_scope_mode: AgentKnowledgeScopeMode
  }>(
    `/api/admin/agents/${encodeURIComponent(agentId)}/knowledge-bindings:batch`,
    input,
  )
