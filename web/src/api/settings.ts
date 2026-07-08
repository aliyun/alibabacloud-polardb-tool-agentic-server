import api from './client'

export interface SettingItem {
  key: string
  value: string
  description: string
  type: string
  required: boolean
}

export const getSettings = () => api.get<SettingItem[]>('/api/settings')
export const updateSetting = (key: string, value: string) => api.put(`/api/settings/${key}`, { value })
export const batchUpdateSettings = (settings: Record<string, string>) => api.post('/api/settings/batch', { settings })
export const testCredentials = () => api.post('/api/settings/test-credentials')
