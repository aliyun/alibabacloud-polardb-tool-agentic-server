import api from './client'

export interface PoolStatus {
  target: number
  available: number
  pool_creating: number
  failed: number
  network_ready: boolean
}

export interface PoolInstance {
  id: string
  cluster_id: string
  status: string
  provisioning_step: string | null
  created_at: string | null
}

export const getPoolStatus = () => api.get<PoolStatus>('/api/pool/status')
export const getPoolInstances = () => api.get<PoolInstance[]>('/api/pool/instances')
export const removePoolInstance = (id: string) => api.delete(`/api/pool/instances/${id}`)
export const triggerReplenish = () => api.post('/api/pool/replenish')
