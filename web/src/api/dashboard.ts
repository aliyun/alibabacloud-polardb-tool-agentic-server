import api from './client'
import { listAllAdminInstances } from './instances'

export interface DashboardStats {
  total_users: number
  total_instances: number
  active_instances: number
  pool_available: number
  departments: number
  queries_today: number
}

export async function getDashboardStats(): Promise<DashboardStats> {
  const [usersResp, instances, poolResp, deptsResp] = await Promise.all([
    api.get('/api/users', { params: { limit: 1 } }),
    listAllAdminInstances(),
    api.get('/api/pool/status'),
    api.get('/api/departments'),
  ])

  return {
    total_users: usersResp.data.total,
    total_instances: instances.total,
    active_instances: instances.items.filter(
      (instance) => instance.status === 'active',
    ).length,
    pool_available: poolResp.data.available,
    departments: deptsResp.data.length,
    queries_today: 0,
  }
}
