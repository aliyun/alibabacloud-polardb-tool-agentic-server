import api from './client'

export type InstanceEngine = 'polardb_mysql'
export type InstanceTopology = 'single_tenant' | 'multitenant'
export type AllocationMode = 'auto_provisioned' | 'pooled' | 'registered'

export interface InstanceHealth {
  healthy: boolean
  checked_at: string
  consecutive_failures: number
  error_code: string | null
}

export interface InstanceBindingCounts {
  users: number
  departments: number
  agents: number
}

export interface InstanceSummary {
  id: string
  cluster_id: string
  name: string
  usage: string | null
  engine: InstanceEngine
  topology: InstanceTopology
  allocation_mode: AllocationMode
  region: string | null
  host: string | null
  port: number | null
  status: string
  owner_user_id: string | null
  health: InstanceHealth | null
  binding_counts: InstanceBindingCounts
}

export interface InstanceListResponse {
  items: InstanceSummary[]
  total: number
  offset: number
  limit: number
}

export interface RegisterInstanceInput {
  cluster_id: string
  name: string
  usage?: string
  engine: InstanceEngine
  topology: InstanceTopology
  region?: string
  host: string
  port: number
  username: string
  password: string
}

export interface TestInstanceConnectionInput {
  topology: InstanceTopology
  host: string
  port: number
  username: string
  password: string
}

export interface UpdateInstanceInput {
  name?: string
  usage?: string | null
  region?: string
  host?: string
  port?: number
  test_credential_id?: string
}

export const listAdminInstances = (offset = 0, limit = 50) =>
  api.get<InstanceListResponse>('/api/instances', {
    params: { offset, limit },
  })

export async function listAllAdminInstances(
  pageSize = 200,
): Promise<InstanceListResponse> {
  const items: InstanceSummary[] = []
  const seenIds = new Set<string>()
  let offset = 0
  let expectedTotal: number | null = null
  while (true) {
    const response = await listAdminInstances(offset, pageSize)
    const page = response.data
    if (page.offset !== offset) {
      throw new Error(
        `Instance inventory offset mismatch: requested ${offset}, received ${page.offset}`,
      )
    }
    if (expectedTotal === null) {
      expectedTotal = page.total
    } else if (page.total !== expectedTotal) {
      throw new Error(
        `Instance inventory total changed from ${expectedTotal} to ${page.total}`,
      )
    }
    if (
      page.limit < 1 ||
      page.items.length > page.limit ||
      page.items.length > pageSize
    ) {
      throw new Error('Instance inventory page exceeded its requested limit')
    }

    const uniqueBefore = seenIds.size
    for (const item of page.items) {
      if (seenIds.has(item.id)) {
        throw new Error(
          `Instance inventory contains duplicate instance id ${item.id}`,
        )
      }
      seenIds.add(item.id)
      items.push(item)
    }
    if (page.items.length > 0 && seenIds.size === uniqueBefore) {
      throw new Error('Instance inventory page made no unique progress')
    }
    if (seenIds.size > expectedTotal) {
      throw new Error('Instance inventory returned more unique rows than total')
    }
    if (seenIds.size === expectedTotal) {
      return { items, total: expectedTotal, offset: 0, limit: pageSize }
    }
    if (page.items.length === 0) {
      throw new Error(
        'Instance inventory stopped before all results were returned',
      )
    }
    offset += page.items.length
  }
}

export const getAdminInstance = (instanceId: string) =>
  api.get<InstanceSummary>(
    `/api/instances/${encodeURIComponent(instanceId)}`,
  )

export const updateAdminInstance = (
  instanceId: string,
  input: UpdateInstanceInput,
) =>
  api.put<InstanceSummary>(
    `/api/instances/${encodeURIComponent(instanceId)}`,
    input,
  )

export const registerAdminInstance = (input: RegisterInstanceInput) =>
  api.post<InstanceSummary>('/api/instances', input)

export const testAdminInstanceConnection = (
  input: TestInstanceConnectionInput,
) => api.post<{ ok: true }>('/api/instances/test-connection', input)

export const testStoredInstanceConnection = (
  instanceId: string,
  input: { host: string; port: number; credential_id: string },
) =>
  api.post<{ ok: true }>(
    `/api/instances/${encodeURIComponent(instanceId)}/test-connection`,
    input,
  )

export const removeAdminInstance = (instanceId: string) =>
  api.delete(`/api/instances/${encodeURIComponent(instanceId)}`)

export const retryProvision = (instanceId: string) => api.post(`/api/instances/${instanceId}/retry-provision`)
export const deleteFailedInstance = (instanceId: string) => api.delete(`/api/instances/${instanceId}/failed`)
