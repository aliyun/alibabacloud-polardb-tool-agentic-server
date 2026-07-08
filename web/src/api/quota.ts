import api from './client'

export interface QuotaStatus {
  global: { limit: number | null; current: number }
  departments: { id: string; limit: number | null; current: number }[]
}

export const getQuotaStatus = () => api.get<QuotaStatus>('/api/quota/status')
export const updateGlobalQuota = (maxLimit: number) =>
  api.put('/api/quota/global', { max_limit: maxLimit })
