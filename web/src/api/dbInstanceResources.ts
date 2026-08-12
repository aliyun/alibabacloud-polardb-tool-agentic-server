import api from './client'

export interface DBInstanceResourceAdmin {
  id: string
  owner_agent_id: string
  backend_id: string
  allocated_instance_id: string | null
  name: string | null
  provisioning_mode: 'dedicated' | 'multitenant'
  status: string
  provisioning_step: string
  cleanup_step: string
  delete_step: string
  delete_requested_at: string | null
  disconnected_at: string | null
  cooldown_until: string | null
  delete_cooldown_duration_hours: number | null
  reclaim_policy: string | null
  failure_reason: string | null
  restore_failure_reason: string | null
  actions: { restore: boolean }
}

export const listDBInstanceResources = () =>
  api.get<DBInstanceResourceAdmin[]>('/api/db-instance-resources')

export const restoreDBInstanceResource = (resourceId: string) =>
  api.post<DBInstanceResourceAdmin>(
    `/api/db-instance-resources/${encodeURIComponent(resourceId)}/restore`,
  )
