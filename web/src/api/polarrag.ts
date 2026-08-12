import api from './client'

export type PolarRAGInstanceStatus =
  | 'pending'
  | 'active'
  | 'error'
  | 'capability_missing'
  | 'disabled'

export interface PolarRAGCapabilities {
  version: string | null
  search: boolean
  protected_document_info: boolean
  protected_context: boolean
  protected_document_search: boolean
  space_catalog: boolean
  knowledge_base_catalog: boolean
}

export interface PolarRAGInstance {
  id: string
  name: string
  scheme: 'http' | 'https'
  host: string
  port: number
  tls_verify: boolean
  status: PolarRAGInstanceStatus
  plugin_version: string | null
  capabilities: PolarRAGCapabilities | null
  last_checked_at: string | null
  last_error_code: string | null
  created_at: string
  updated_at: string | null
}

export interface CreatePolarRAGInstanceInput {
  name: string
  scheme: 'http' | 'https'
  host: string
  port: number
  username: string
  password: string
  tls_verify: boolean
  ca_bundle?: string | null
}

export interface UpdatePolarRAGInstanceInput {
  name?: string
  username?: string
  password?: string
  tls_verify?: boolean
  ca_bundle?: string | null
}

export interface PolarRAGSpace {
  space_id: string
  name: string
  identity_domain: string
  oss_bucket?: string | null
  oss_endpoint?: string | null
  oss_config_validated?: boolean
  oss_validated_at?: string | null
  oss_last_error_code?: string | null
  status: string
  enabled: boolean
  knowledge_space_id: string | null
  last_synced_at: string | null
  knowledge_resources: PolarRAGKnowledgeResource[]
}

export interface ConfigurePolarRAGOssInput {
  access_key_id: string
  access_key_secret: string
  object_prefix: string
}

export interface PolarRAGKnowledgeResource {
  knowledge_resource_id: string
  name: string
  kb_type: string
  binding_mode: 'domain' | 'owner' | null
  sync_status:
    | 'active'
    | 'upstream_disabled'
    | 'owner_unresolved'
    | 'unsupported_kb_type'
  enabled: boolean
}

export interface PolarRAGSyncResult {
  active: number
  disabled: number
  owner_unresolved: number
}

export interface UnclaimedPolarRAGKnowledgeBase {
  space_id: string
  space_name: string
  identity_domain: string
  kb_id: string
  name: string
  kb_type: string
  status: 'UNCLAIMED'
}

export interface PolarRAGOwnerCandidate {
  principal_assignment_id: string
  pas_user_id: string
  user_name: string
  identity_domain: string
  provider: EnterprisePrincipalProvider
  principal_id: string
}

export interface UnclaimedPolarRAGCatalog {
  items: UnclaimedPolarRAGKnowledgeBase[]
  owner_candidates: PolarRAGOwnerCandidate[]
}

export interface EnabledPolarRAGSpace {
  knowledge_space_id: string
  name: string
  identity_domain: string
  enabled: boolean
  sync: PolarRAGSyncResult
}

export type EnterprisePrincipalProvider = 'feishu' | 'sharepoint'
export type EnterprisePrincipalType = 'user' | 'group'
export type EnterprisePrincipalStatus = 'active' | 'disabled'

export interface EnterprisePrincipal {
  id: string
  pas_user_id: string
  identity_domain: string
  provider: EnterprisePrincipalProvider
  principal_type: EnterprisePrincipalType
  principal_id: string
  source: 'admin_managed' | 'remote_resolver'
  status: EnterprisePrincipalStatus
  valid_until: string | null
  created_at: string
  updated_at: string | null
}

export interface CreateEnterprisePrincipalInput {
  identity_domain: string
  provider: EnterprisePrincipalProvider
  principal_type: EnterprisePrincipalType
  principal_id: string
  valid_until: string | null
}

export interface UpdateEnterprisePrincipalInput {
  status?: EnterprisePrincipalStatus
  valid_until?: string | null
}

const encoded = (value: string) => encodeURIComponent(value)

export const listPolarRAGInstances = () =>
  api.get<{ items: PolarRAGInstance[] }>('/api/polarrag/instances')

export const createPolarRAGInstance = (
  input: CreatePolarRAGInstanceInput,
) => api.post<PolarRAGInstance>('/api/polarrag/instances', input)

export const updatePolarRAGInstance = (
  instanceId: string,
  input: UpdatePolarRAGInstanceInput,
) =>
  api.patch<PolarRAGInstance>(
    `/api/polarrag/instances/${encoded(instanceId)}`,
    input,
  )

export const checkPolarRAGInstance = (instanceId: string) =>
  api.post<PolarRAGInstance>(
    `/api/polarrag/instances/${encoded(instanceId)}/check`,
  )

export const disablePolarRAGInstance = (instanceId: string) =>
  api.delete(`/api/polarrag/instances/${encoded(instanceId)}`)

export const listPolarRAGSpaces = (instanceId: string) =>
  api.get<{ items: PolarRAGSpace[] }>(
    `/api/polarrag/instances/${encoded(instanceId)}/spaces`,
  )

export const enablePolarRAGSpace = (
  instanceId: string,
  spaceId: string,
) =>
  api.post<EnabledPolarRAGSpace>(
    `/api/polarrag/instances/${encoded(instanceId)}/spaces/enable`,
    { space_id: spaceId },
  )

export const disablePolarRAGSpace = (
  instanceId: string,
  spaceId: string,
) =>
  api.delete(
    `/api/polarrag/instances/${encoded(instanceId)}/spaces/${encoded(spaceId)}`,
  )

export const syncPolarRAGSpace = (
  instanceId: string,
  spaceId: string,
) =>
  api.post<PolarRAGSyncResult>(
    `/api/polarrag/instances/${encoded(instanceId)}/spaces/${encoded(spaceId)}/sync`,
  )

export const configurePolarRAGSpaceOss = (
  knowledgeSpaceId: string,
  input: ConfigurePolarRAGOssInput,
) =>
  api.put<{
    knowledge_space_id: string
    bucket: string
    endpoint: string
    object_prefix: string
    validated: boolean
    validated_at: string
  }>(
    `/api/polarrag/spaces/${encoded(knowledgeSpaceId)}/oss-config`,
    input,
  )

export const listUnclaimedPolarRAGKnowledgeBases = (instanceId: string) =>
  api.get<UnclaimedPolarRAGCatalog>(
    `/api/polarrag/instances/${encoded(instanceId)}/unclaimed-knowledge-bases`,
  )

export const claimPolarRAGKnowledgeBase = (
  instanceId: string,
  spaceId: string,
  kbId: string,
  principalAssignmentId: string,
) =>
  api.post<{
    kb_id: string
    status: 'ACTIVE'
    sync: PolarRAGSyncResult
  }>(
    `/api/polarrag/instances/${encoded(instanceId)}/spaces/${encoded(spaceId)}/knowledge-bases/${encoded(kbId)}/claim`,
    { principal_assignment_id: principalAssignmentId },
  )

export const listEnterprisePrincipals = (userId: string) =>
  api.get<{ items: EnterprisePrincipal[] }>(
    `/api/polarrag/users/${encoded(userId)}/principals`,
  )

export const createEnterprisePrincipal = (
  userId: string,
  input: CreateEnterprisePrincipalInput,
) =>
  api.post<EnterprisePrincipal>(
    `/api/polarrag/users/${encoded(userId)}/principals`,
    input,
  )

export const updateEnterprisePrincipal = (
  userId: string,
  principalId: string,
  input: UpdateEnterprisePrincipalInput,
) =>
  api.patch<EnterprisePrincipal>(
    `/api/polarrag/users/${encoded(userId)}/principals/${encoded(principalId)}`,
    input,
  )

export const deleteEnterprisePrincipal = (
  userId: string,
  principalId: string,
) =>
  api.delete(
    `/api/polarrag/users/${encoded(userId)}/principals/${encoded(principalId)}`,
  )
