import api from './client'

export type ProvisioningBackendStatus = 'active' | 'draining' | 'disabled'

export interface ProvisioningBackend {
  id: string
  instance_id: string
  admin_credential_id: string
  status: ProvisioningBackendStatus
  priority: number
  max_active_resources: number
  resource_min_cpu: number
  resource_max_cpu: number
  ddl_concurrency: number
  config_revision: number
  healthy: boolean
  health_checked_at: string | null
  available_for_create: boolean
  created_at: string
  updated_at: string | null
}

export interface CreateProvisioningBackendInput {
  instance_id: string
  admin_credential_id: string
  priority: number
  max_active_resources: number
  resource_min_cpu: number
  resource_max_cpu: number
  ddl_concurrency: number
}

export interface UpdateProvisioningBackendInput {
  admin_credential_id?: string
  priority?: number
  max_active_resources?: number
  resource_min_cpu?: number
  resource_max_cpu?: number
  ddl_concurrency?: number
  status?: 'active'
}

export const listProvisioningBackends = () =>
  api.get<ProvisioningBackend[]>('/api/provisioning-backends')

export const createProvisioningBackend = (
  input: CreateProvisioningBackendInput,
) => api.post<ProvisioningBackend>('/api/provisioning-backends', input)

export const updateProvisioningBackend = (
  backendId: string,
  input: UpdateProvisioningBackendInput,
) =>
  api.put<ProvisioningBackend>(
    `/api/provisioning-backends/${encodeURIComponent(backendId)}`,
    input,
  )

export const drainProvisioningBackend = (backendId: string) =>
  api.post<ProvisioningBackend>(
    `/api/provisioning-backends/${encodeURIComponent(backendId)}/drain`,
  )

export const disableProvisioningBackend = (backendId: string) =>
  api.post<ProvisioningBackend>(
    `/api/provisioning-backends/${encodeURIComponent(backendId)}/disable`,
  )
