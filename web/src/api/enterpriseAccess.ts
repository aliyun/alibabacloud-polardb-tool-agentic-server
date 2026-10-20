import api from './client'
import type { PageParams } from './pagination'

export interface EnterpriseIdentitySourceCandidate {
  id: string
  name: string
  provider: 'feishu' | 'sharepoint' | 'dingtalk'
  status: string
  last_synced_at: string | null
  snapshot_usable?: boolean
}

export interface EnterpriseDirectoryUserCandidate {
  id: string
  pas_user_id: string | null
  external_user_id: string
  display_name: string
  email: string | null
  status: string
}

export interface EnterpriseDirectoryGroupCandidate {
  id: string
  external_group_id: string
  display_name: string
  principal_type: string
  status: string
}

export interface EnterpriseDirectoryCandidates {
  users: EnterpriseDirectoryUserCandidate[]
  groups: EnterpriseDirectoryGroupCandidate[]
  total: number | null
}

export interface EnterpriseSpaceCandidate {
  knowledge_space_id: string
  polarrag_instance_id: string
  name: string
  identity_domain: string
  is_bound?: boolean
}

export interface EnterpriseAccessSelection {
  identity_source_id: string
  all_synced_users: boolean
  directory_group_ids: string[]
  pas_user_ids: string[]
  polarrag_instance_ids: string[]
  knowledge_space_ids: string[]
  bind_all_knowledge_resources?: boolean | null
}

export interface EnterpriseAccessImpact {
  relation_type: string
  relation_id: string | null
  display_name: string
  scope: 'agent' | 'global'
}

export interface EnterpriseAccessPreview {
  selection: EnterpriseAccessSelection
  creates: EnterpriseAccessImpact[]
  reuses: EnterpriseAccessImpact[]
  global_changes: EnterpriseAccessImpact[]
  preview_hash: string
}

const encoded = (value: string) => encodeURIComponent(value)

export const listEnterpriseIdentitySources = () =>
  api.get<{ items: EnterpriseIdentitySourceCandidate[] }>(
    '/api/identity-sources',
  )

export const listEnterpriseIdentitySourceDirectory = (
  sourceId: string,
  entryType: 'users' | 'groups',
  options: { offset?: number; limit?: number; search?: string } = {},
) =>
  api.get<EnterpriseDirectoryCandidates>(
    `/api/identity-sources/${encoded(sourceId)}/directory`,
    {
      params: {
        entry_type: entryType,
        offset: options.offset ?? 0,
        limit: options.limit ?? 100,
        search: options.search,
      },
    },
  )

export const listEnterpriseIdentitySourceSpaces = () =>
  api.get<{
    items: EnterpriseSpaceCandidate[]
    total: number
    offset: number
    limit: number
  }>('/api/identity-sources/spaces')

export const listEnterpriseIdentitySourceSpacesPage = (
  params: PageParams & { identity_source_id?: string; agent_id?: string } = {},
) =>
  api.get<{
    items: EnterpriseSpaceCandidate[]
    total: number
    offset: number
    limit: number
    all_users_bound?: boolean
  }>('/api/identity-sources/spaces', { params })

export const unbindEnterpriseIdentitySourceSpace = (
  sourceId: string,
  knowledgeSpaceId: string,
) =>
  api.delete(
    `/api/identity-sources/${encoded(sourceId)}/spaces/${encoded(knowledgeSpaceId)}`,
  )

export const previewAgentEnterpriseAccess = (
  agentId: string,
  selection: EnterpriseAccessSelection,
) =>
  api.post<EnterpriseAccessPreview>(
    `/api/agents/${encoded(agentId)}/enterprise-access/preview`,
    selection,
  )

export const applyAgentEnterpriseAccess = (
  agentId: string,
  request: EnterpriseAccessSelection & { preview_hash: string },
) =>
  api.post<EnterpriseAccessPreview>(
    `/api/agents/${encoded(agentId)}/enterprise-access/apply`,
    request,
  )
