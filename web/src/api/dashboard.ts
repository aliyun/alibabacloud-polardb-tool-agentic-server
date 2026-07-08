import api from './client'

export interface DashboardStats {
  total_users: number
  total_instances: number
  active_instances: number
  pool_available: number
  departments: number
  queries_today: number
}

export async function getDashboardStats(): Promise<DashboardStats> {
  const [usersResp, instancesResp, poolResp, deptsResp] = await Promise.allSettled([
    api.get('/api/users', { params: { limit: 1 } }),
    api.get('/api/instances'),
    api.get('/api/pool/status'),
    api.get('/api/departments'),
  ])

  return {
    total_users: usersResp.status === 'fulfilled' ? (usersResp.value.data.total ?? 0) : 0,
    total_instances: instancesResp.status === 'fulfilled' ? (instancesResp.value.data?.length ?? 0) : 0,
    active_instances: instancesResp.status === 'fulfilled'
      ? (instancesResp.value.data?.filter((i: { status: string }) => i.status === 'active').length ?? 0)
      : 0,
    pool_available: poolResp.status === 'fulfilled' ? (poolResp.value.data.available ?? 0) : 0,
    departments: deptsResp.status === 'fulfilled' ? (deptsResp.value.data?.length ?? 0) : 0,
    queries_today: 0,
  }
}
