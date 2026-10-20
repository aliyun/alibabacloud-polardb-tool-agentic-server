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
  knowledge_scope_mode?: AgentKnowledgeScopeMode
  external_knowledge_auto_bind_enabled?: boolean
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
  knowledge_scope_mode?: AgentKnowledgeScopeMode
  external_knowledge_auto_bind_enabled?: boolean
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
  assignment_id?: string | null
  inherited_source_names?: string[]
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
  public_excluded_resource_ids?: string[]
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
  identity_source_names?: string[]
}

export type { Page } from './pagination'

export interface AgentUserOption {
  availability?: 'active' | 'disabled' | 'unavailable'
  assignment_id?: string | null
  inherited_source_names?: string[]
  id: string
  display_name: string
  external_id: string
  status: string
  identity_source_names?: string[]
}

export type AgentKnowledgeScopeMode = 'LEGACY_ALL' | 'SCOPED'
export type AgentKnowledgeBindingOrigin = 'MANUAL' | 'EXTERNAL_SYNC'
export type AgentKnowledgeBindingSubjectType =
  | 'USER'
  | 'DEPARTMENT'
  | 'GROUP'
  | 'IDENTITY_SOURCE'

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
  kb_type?: 'PUBLIC' | 'PERSONAL'
  agent_bound?: boolean
  knowledge_resource_id: string
  space_id: string | null
  space_name: string | null
  kb_id: string | null
  name: string | null
  management_mode: 'NATIVE' | 'EXTERNAL_SYNC' | null
  identity_source_id: string | null
  identity_source_ids: string[]
  access_scope?: 'ALL_USERS' | 'SELECTED' | 'UNCONFIGURED'
  inheritance_sources?: string[]
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

export interface AgentKnowledgeBindingStatus {
  knowledge_resource_id: string
  status: 'BOUND' | 'PARTIAL' | 'UNBOUND'
  bound_subject_count: number
  subject_count: number
  bound_subject_indexes: number[]
  direct_bound_subject_indexes: number[]
  partial_subject_indexes?: number[]
  excluded_subject_indexes?: number[]
  inherited_subject_sources: Record<string, string[]>
}

export interface AgentKnowledgeAllUsersStatus {
  knowledge_resource_id: string
  select_all: boolean
  excluded_subject_count: number
  status: 'BOUND' | 'PARTIAL' | 'UNBOUND'
}

export interface AgentKnowledgeVisibility {
  instance_count: number
  statuses: Record<'PUBLIC' | 'PERSONAL', 'BOUND' | 'PARTIAL' | 'UNBOUND'>
}

export const getAgentKnowledgeVisibility = (agentId: string) =>
  api.get<AgentKnowledgeVisibility>(`/api/admin/agents/${encodeURIComponent(agentId)}/knowledge-visibility`)

export const bindAgentKnowledgeVisibility = (agentId: string, knowledgeResourceIds: string[]) =>
  api.post<AgentKnowledgeVisibility>(
    `/api/admin/agents/${encodeURIComponent(agentId)}/knowledge-visibility:bind`,
    { knowledge_resource_ids: knowledgeResourceIds },
  )

export const unbindAgentKnowledgeVisibility = (agentId: string, knowledgeResourceIds: string[]) =>
  api.post<AgentKnowledgeVisibility>(
    `/api/admin/agents/${encodeURIComponent(agentId)}/knowledge-visibility:unbind`,
    { knowledge_resource_ids: knowledgeResourceIds },
  )

export const updateAgentKnowledgeVisibility = (
  agentId: string, kbType: 'PUBLIC' | 'PERSONAL', operation: 'BIND' | 'UNBIND',
) => api.post<AgentKnowledgeVisibility>(
  `/api/admin/agents/${encodeURIComponent(agentId)}/knowledge-visibility`,
  { kb_type: kbType, operation },
)

export const getAgentKnowledgeAllUsersStatus = (agentId: string, knowledgeResourceIds: string[]) =>
  api.post<{ items: AgentKnowledgeAllUsersStatus[] }>(
    `/api/admin/agents/${encodeURIComponent(agentId)}/knowledge-all-users-status`,
    { knowledge_resource_ids: knowledgeResourceIds },
  )

export const updateAgentKnowledgeAllUsers = (
  agentId: string, knowledgeResourceIds: string[], operation: 'BIND' | 'UNBIND',
) => api.post<{ items: AgentKnowledgeAllUsersStatus[] }>(
  `/api/admin/agents/${encodeURIComponent(agentId)}/knowledge-all-users`,
  { knowledge_resource_ids: knowledgeResourceIds, operation },
)

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
  params: Pick<Page<unknown>, 'offset' | 'limit'> & {
    search?: string
    binding_status?: 'bound' | 'unbound' | 'all'
  },
) =>
  api.get<Page<AgentUserOption>>(
    `/api/agents/${encodeURIComponent(agentId)}/user-options`,
    { params },
  )

export interface AgentKnowledgeSubjectOption {
  availability?: 'active' | 'disabled' | 'unavailable'
  subject: AgentKnowledgeBindingSubject
  label: string
  identity_source: string | null
  status: AgentKnowledgeBindingStatus['status']
  inheritance_sources: string[]
}

export const listAgentKnowledgeSubjectOptions = (
  agentId: string,
  body: {
    knowledge_resource_ids: string[]
    category: 'users' | 'groups' | 'sources'
    binding_status: 'available' | 'bound'
    search?: string
    offset: number
    limit: number
  },
) => api.post<{ items: AgentKnowledgeSubjectOption[]; next_offset: number | null }>(
  `/api/admin/agents/${encodeURIComponent(agentId)}/knowledge-subject-options`, body,
)

export interface AgentEnterpriseUserOption {
  availability?: 'active' | 'disabled' | 'unavailable'
  id: string
  display_name: string
  external_user_id: string
  email: string | null
  identity_source_id: string
  identity_source_name: string
}

export const listAgentEnterpriseUserOptions = (
  agentId: string,
  params: Pick<Page<unknown>, 'offset' | 'limit'> & {
    identity_source_ids: string
    binding_status: 'bound' | 'unbound'
    search?: string
  },
) =>
  api.get<Page<AgentEnterpriseUserOption>>(
    `/api/agents/${encodeURIComponent(agentId)}/enterprise-user-options`,
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
  params: Pick<Page<unknown>, 'offset' | 'limit'> & {
    search?: string
    category?: 'identity_source' | 'group'
    identity_source_ids?: string
    binding_status?: 'bound' | 'unbound' | 'all'
  },
) =>
  api.get<Page<AgentGroupOption>>(
    `/api/agents/${encodeURIComponent(agentId)}/group-options`,
    { params },
  )

export const listAgentGroupAssignments = (
  agentId: string,
  params: Pick<Page<unknown>, 'offset' | 'limit'> & {
    search?: string
    category?: 'identity_source' | 'group'
    identity_source_ids?: string
  },
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
  params: PageParams & {
    management_mode?: 'NATIVE' | 'EXTERNAL_SYNC'
    binding_status?: 'bound' | 'unbound'
  } = {},
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

export interface AgentAllSubjectAssignments {
  status: 'BOUND' | 'PARTIAL' | 'UNBOUND'
  eligible_source_count: number
  eligible_user_count: number
  assignment_count: number
}

export const getAgentAllSubjectAssignments = (agentId: string) =>
  api.get<AgentAllSubjectAssignments>(`/api/agents/${encodeURIComponent(agentId)}/all-subject-assignments`)

export const updateAgentAllSubjectAssignments = (agentId: string, operation: 'BIND' | 'UNBIND') =>
  api.post<AgentAllSubjectAssignments>(`/api/agents/${encodeURIComponent(agentId)}/all-subject-assignments`, { operation })

export const getAgentKnowledgeBindingStatus = (
  agentId: string,
  subjects: AgentKnowledgeBindingSubject[],
  knowledgeResourceIds: string[],
) =>
  api.post<{ items: AgentKnowledgeBindingStatus[] }>(
    `/api/admin/agents/${encodeURIComponent(agentId)}/knowledge-binding-status`,
    {
      subjects,
      knowledge_resource_ids: knowledgeResourceIds,
    },
  )

export interface AgentExternalKnowledgeAutoBind {
  agent_id: string
  enabled: boolean
  knowledge_scope_mode: AgentKnowledgeScopeMode
}

export const getAgentExternalKnowledgeAutoBind = (agentId: string) =>
  api.get<AgentExternalKnowledgeAutoBind>(
    `/api/admin/agents/${encodeURIComponent(agentId)}/external-knowledge-auto-bind`,
  )

export const updateAgentExternalKnowledgeAutoBind = (agentId: string, enabled: boolean) =>
  api.put<{ agent_id: string; enabled: boolean }>(
    `/api/admin/agents/${encodeURIComponent(agentId)}/external-knowledge-auto-bind`,
    { enabled },
  )

export const reconcileAgentExternalKnowledge = (agentId: string) =>
  api.post<{ agent_id: string; snapshots_processed: number; snapshots_skipped: number; bindings_projected: number }>(
    `/api/admin/agents/${encodeURIComponent(agentId)}/external-knowledge-bindings/reconcile`,
  )

export const switchAgentKnowledgeScopeMode = (
  agentId: string, autoBind: boolean, mode: AgentKnowledgeScopeMode,
  resetAllUsers: boolean, expectedMode: AgentKnowledgeScopeMode,
) => api.put<{ agent_id: string; mode: AgentKnowledgeScopeMode; enabled: boolean; removed_all_users_policies: number }>(
  `/api/admin/agents/${encodeURIComponent(agentId)}/knowledge-scope-mode`,
  { mode, expected_mode: expectedMode, external_knowledge_auto_bind_enabled: autoBind, reset_all_users_policies: resetAllUsers },
)

export interface KnowledgePermissionRefreshJob {
  job_id: string
  agent_id: string
  state: 'QUEUED' | 'RUNNING' | 'SUCCEEDED' | 'PARTIAL' | 'FAILED'
  resources_total: number
  resources_completed: number
  resources_unprocessed?: number
  resources_skipped?: number
  error?: string | null
}

export const refreshAgentKnowledgePermissions = (agentId: string, resourceId?: string | string[]) => {
  const ids = typeof resourceId === 'string' ? [resourceId] : resourceId
  return api.post<KnowledgePermissionRefreshJob>(
    `/api/admin/agents/${encodeURIComponent(agentId)}/knowledge-permissions/refresh`,
    ids ? { knowledge_resource_ids: ids, ...(ids.length === 1 ? { wait_timeout_seconds: 10 } : {}) } : {},
    { validateStatus: (status) => status === 200 || status === 202 || status === 502 || status === 409 || status === 413 },
  )
}

export const getAgentKnowledgePermissionRefresh = (agentId: string, jobId: string) =>
  api.get<KnowledgePermissionRefreshJob>(
    `/api/admin/agents/${encodeURIComponent(agentId)}/knowledge-permissions/refresh/${encodeURIComponent(jobId)}`,
  )
