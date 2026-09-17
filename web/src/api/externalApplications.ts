import api from './client'
import type { Page, PageParams } from './pagination'

export type ExternalApplicationTarget = 'mcp' | 'api'
export type ExternalApplicationStatus = 'active' | 'disabled'
export type ExternalApplicationAgentPolicy =
  | 'workspace_default'
  | 'fixed'
  | 'caller_selectable'

export interface ExternalApplicationResource {
  target: ExternalApplicationTarget
  resource: string
  scope: string
}

export interface ExternalApplicationContext {
  provider_enabled: boolean
  provider_type: string
  token_endpoint: string
  compatibility_token_endpoint: string
  resources: ExternalApplicationResource[]
}

export interface ExternalApplication {
  client_id: string
  name: string
  provider_type: string
  targets: ExternalApplicationTarget[]
  agent_policy: ExternalApplicationAgentPolicy
  fixed_agent_id: string | null
  status: ExternalApplicationStatus
  secret_expires_at: string | null
  secret_created_at: string
  last_used_at: string | null
  created_at: string
  updated_at: string | null
  token_endpoint: string
  compatibility_token_endpoint: string
  resources: ExternalApplicationResource[]
}

export interface ExternalApplicationSecret extends ExternalApplication {
  client_secret: string
}

export interface CreateExternalApplicationInput {
  name: string
  targets: ExternalApplicationTarget[]
  agent_policy: ExternalApplicationAgentPolicy
  fixed_agent_id: string | null
  secret_expires_at: string | null
}

export interface TestExternalApplicationInput {
  subject_token: string
  resource: string | null
  agent_id: string | null
  identity_source_id?: string | null
  feishu_user_id?: string | null
  feishu_union_id?: string | null
}

export interface ExternalApplicationTestResult {
  authenticated: true
  expires_in: number | null
  scope: string
}

const basePath = '/api/v1/external-auth/clients'
const encoded = (value: string) => encodeURIComponent(value)

export const getExternalApplicationContext = () =>
  api.get<ExternalApplicationContext>(`${basePath}/context`)

export const listExternalApplications = (params: PageParams = {}) =>
  api.get<Page<ExternalApplication>>(basePath, { params })

export const createExternalApplication = (
  input: CreateExternalApplicationInput,
) => api.post<ExternalApplicationSecret>(basePath, input)

export const rotateExternalApplicationSecret = (
  clientId: string,
  secretExpiresAt: string | null,
) =>
  api.post<ExternalApplicationSecret>(
    `${basePath}/${encoded(clientId)}/rotate-secret`,
    { secret_expires_at: secretExpiresAt },
  )

export const updateExternalApplicationStatus = (
  clientId: string,
  status: ExternalApplicationStatus,
) =>
  api.put<ExternalApplication>(
    `${basePath}/${encoded(clientId)}/status`,
    { status },
  )

export const testExternalApplication = (
  clientId: string,
  input: TestExternalApplicationInput,
) =>
  api.post<ExternalApplicationTestResult>(
    `${basePath}/${encoded(clientId)}/test`,
    input,
  )
