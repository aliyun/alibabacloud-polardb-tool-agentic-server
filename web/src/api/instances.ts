import api from './client'

export const retryProvision = (instanceId: string) => api.post(`/api/instances/${instanceId}/retry-provision`)
export const deleteFailedInstance = (instanceId: string) => api.delete(`/api/instances/${instanceId}/failed`)
