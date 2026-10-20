import api from './client'

export type OpenAPINetwork = 'public' | 'vpc'
export interface EndpointRegion {
  region_id: string
  label: { 'zh-CN': string; en: string }
  polardb: Record<OpenAPINetwork, string | null>
  sts: Record<OpenAPINetwork, string | null>
}
export interface EndpointCatalog {
  reviewed_at: string
  sources: Record<'polardb' | 'sts', string>
  regions: EndpointRegion[]
  defaults?: { region_id: string; openapi_network: OpenAPINetwork } | null
}

export async function getEndpointCatalog(): Promise<EndpointCatalog> {
  const response = await api.get('/api/config/aliyun-endpoints')
  if (!Array.isArray(response.data?.regions)) throw new Error('Endpoint catalog unavailable')
  return response.data
}
