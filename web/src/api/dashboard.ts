import api from './client'
import { listDedicatedPools } from './dedicatedPools'
import { listAllAdminInstances } from './instances'
import { listPolarRAGInstances } from './polarrag'

export interface DashboardStats {
  total_users: number
  total_instances: number
  active_instances: number
  dedicated_allocatable: number
  departments: number
  queries_today: number
}

export interface MemberDashboardStats {
  database_instances: number
  knowledge_resources: number
}

export type DashboardViewStats = DashboardStats | MemberDashboardStats

export async function getDashboardStats(
  isAdmin = true,
): Promise<DashboardViewStats> {
  if (!isAdmin) {
    const response = await api.get('/api/me/resources')
    return {
      database_instances: response.data.database_instances.length,
      knowledge_resources: response.data.knowledge_resources.length,
    }
  }

  const createdFrom = new Date()
  createdFrom.setHours(0, 0, 0, 0)
  const [
    usersResp,
    instances,
    polarRAGResp,
    poolsResp,
    deptsResp,
    sqlAuditResp,
    polarRAGAuditResp,
  ] =
    await Promise.all([
      api.get('/api/users', { params: { limit: 1 } }),
      listAllAdminInstances(),
      listPolarRAGInstances(),
      listDedicatedPools(),
      api.get('/api/departments'),
      api.get('/api/audit-logs', {
        params: {
          category: 'sql',
          created_from: createdFrom.toISOString(),
          offset: 0,
          limit: 1,
        },
      }),
      api.get('/api/audit-logs', {
        params: {
          category: 'polarrag',
          created_from: createdFrom.toISOString(),
          offset: 0,
          limit: 1,
        },
      }),
    ])
  const polarRAGInstances = polarRAGResp.data.items

  return {
    total_users: usersResp.data.total,
    total_instances: instances.total + polarRAGInstances.length,
    active_instances: instances.items.filter(
      (instance) => instance.status === 'active',
    ).length + polarRAGInstances.filter(
      (instance) => instance.status === 'active',
    ).length,
    dedicated_allocatable: poolsResp.data.reduce(
      (total, pool) => total + pool.allocatable,
      0,
    ),
    departments: deptsResp.data.length,
    queries_today: sqlAuditResp.data.total + polarRAGAuditResp.data.total,
  }
}
