import api from './client'
import type { Page } from './pagination'
export type AccountKind = 'personal' | 'service'
export interface Account {
  id: string
  name: string
  identity: string
  status: string
  authentication: string
  role: string
}
export interface Resource {
  id: string
  name: string
  kind: 'database' | 'knowledge'
  status: string
  type: string
  source: string
  enabled?: boolean
}
export interface Grant {
  id: string
  account_kind: AccountKind | 'group'
  account_id: string
  name: string
  resource_id: string
  resource_name: string
  source: string
  enabled: boolean
  permission: 'readonly' | 'readwrite' | null
  credential_id: string | null
}
export const listAccounts = (kind: AccountKind, search = '', offset = 0) =>
  api.get<Page<Account>>('/api/access/accounts', {
    params: { kind, search, offset, limit: 20 },
  })
export const listResources = (kind = 'database', search = '', offset = 0) =>
  api.get<Page<Resource>>('/api/access/resources', {
    params: { kind, search, offset, limit: 20 },
  })
export const listGrants = (params: {
  resource_id?: string
  account_kind?: AccountKind
  account_id?: string
  offset?: number
}) =>
  api.get<Page<Grant>>('/api/access/grants', {
    params: { ...params, limit: 20 },
  })
export interface PersonalToken {
  id: string
  status: string
  expires_at: string
  revoked_at: string | null
  token?: string
}
export const listPersonalTokens = () =>
  api.get<{ items: PersonalToken[]; mcp_url: string; oauth_ready: boolean }>(
    '/api/me/personal-tokens',
  )
export const issuePersonalToken = (expires_in_days: number) =>
  api.post<PersonalToken>('/api/me/personal-tokens', { expires_in_days })
export const revokePersonalToken = (id: string) =>
  api.delete(`/api/me/personal-tokens/${encodeURIComponent(id)}`)
export interface PersonalResource {
  status?: string
  db_instance_id?: string
  knowledge_resource_id?: string
  name: string
  permission?: string
  capabilities?: string[]
  knowledge_space_name?: string
  access_source?: string
}
export interface PersonalPage {
  database_instances: PersonalResource[]
  database_has_more: boolean
  database_next_cursor: string | null
  knowledge_resources: PersonalResource[]
  knowledge_resource_total: number
}
export const personalResources = (
  database_cursor?: string,
  knowledge_offset = 0,
) =>
  api.get<PersonalPage>('/api/me/resources', {
    params: { personal: true, database_cursor, knowledge_offset },
  })

export const saveSQLGrant = (
  kind: AccountKind,
  accountId: string,
  resourceId: string,
  credential_id: string,
  permission: string,
) =>
  api.put(
    `/api/access/accounts/${kind}/${encodeURIComponent(accountId)}/resources/${encodeURIComponent(resourceId)}`,
    { credential_id, permission },
  )
