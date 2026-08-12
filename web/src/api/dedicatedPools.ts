import api from './client'

export type DedicatedPoolStatus = 'active' | 'draining' | 'disabled'
export type DedicatedMemberStatus =
  | 'replenishing'
  | 'available'
  | 'allocated_preparing'
  | 'allocated'
  | 'cooling_down'
  | 'sanitizing'
  | 'quarantined'
  | 'deleting'
  | 'deleted'

export interface DedicatedPoolMember {
  id: string
  instance_id: string
  allocated_resource_id: string | null
  status: DedicatedMemberStatus
  readiness_status: 'fresh' | 'stale' | 'checking'
  preparation_step: string
  cloud_request_id: string | null
  last_ready_verified_at: string | null
  readiness_evidence_age_seconds: number | null
  delete_cooldown_duration_hours: number
  delete_cooldown_source: 'member' | 'pool' | 'global'
  failure_reason: string | null
  failure_detail: string | null
  failure_occurred_at: string | null
  failure_operation: string | null
  actions: {
    retry: boolean
    quarantine: boolean
    destroy: boolean
  }
}

export interface DedicatedPoolRouteUsage {
  agent_id: string
  agent_name: string
  binding_id: string
  enabled: boolean
  routing_order: number | null
  role: 'primary' | 'fallback' | 'paused'
}

export interface DedicatedPool {
  id: string
  name: string
  status: DedicatedPoolStatus
  target_size: number
  max_total_members: number
  max_member_purchases_per_hour: number
  max_create_requests_per_agent_per_hour: number
  max_delete_requests_per_agent_per_hour: number
  purchase_profile_id: string | null
  purchase_profile_revision: number | null
  purchase_profile_status: 'valid' | 'upgrade_required'
  storage_type: string | null
  region_id: string
  vpc_id: string
  vswitch_id: string
  zone_id: string | null
  security_ip_list: string | null
  reclaim_policy: 'destroy' | 'sanitize_and_reuse'
  lifecycle_admin_policy: 'pas_managed' | 'admin_provided'
  permission_template_revision_id: string
  delete_cooldown_duration_hours: number | null
  effective_delete_cooldown_duration_hours: number
  available_health_check_interval_seconds: number
  available_health_stale_after_seconds: number
  config_revision: number
  allocatable: number
  planning: number
  billable_total: number
  surplus: number
  supply_state:
    | 'not_started'
    | 'prewarming'
    | 'ready'
    | 'partially_ready'
    | 'capacity_limited'
    | 'error'
  supply_current: number
  supply_target: number
  blocking_reasons: string[]
  route_usage: DedicatedPoolRouteUsage[]
  members: DedicatedPoolMember[]
}

export interface DedicatedPurchaseProfile {
  profile_id: string
  revision: number
  default_storage_type: string
  supported_storage_types: string[]
  fixed_parameters: Record<string, string>
}

export interface DedicatedReadiness {
  worker: {
    configured: boolean
    active_worker_count: number
    last_heartbeat_at: string | null
  }
  aliyun_access: {
    configured: boolean
    validated: boolean
    credential_mode: 'direct_ak' | 'assume_role' | 'ecs_ram_role'
  }
  purchase_profile: {
    valid: boolean
    profile_id: string
    revision: number
    default_storage_type: string
    supported_storage_types: string[]
  }
  permission_template: {
    valid: boolean
    default_revision_id: string | null
  }
  simulation_mode: boolean
  preparation_mode: 'full' | 'openapi_only'
  blocking_reasons: string[]
}

export interface CreateDedicatedPoolInput {
  name: string
  target_size: number
  max_total_members: number
  max_member_purchases_per_hour: number
  max_create_requests_per_agent_per_hour: number
  max_delete_requests_per_agent_per_hour: number
  purchase_profile_id: string
  purchase_profile_revision: number
  storage_type: string
  region_id: string
  vpc_id: string
  vswitch_id: string
  zone_id: string
  security_ip_list?: string | null
  reclaim_policy: 'destroy' | 'sanitize_and_reuse'
  lifecycle_admin_policy: 'pas_managed' | 'admin_provided'
  permission_template_revision_id: string
  delete_cooldown_duration_hours?: number | null
  available_health_check_interval_seconds: number
  available_health_stale_after_seconds: number
}

export interface UpdateDedicatedPoolInput {
  expected_config_revision: number
  network_change_confirmed?: boolean
  name?: string
  target_size?: number
  max_total_members?: number
  max_member_purchases_per_hour?: number
  max_create_requests_per_agent_per_hour?: number
  max_delete_requests_per_agent_per_hour?: number
  storage_type?: string
  region_id?: string
  zone_id?: string
  vpc_id?: string
  vswitch_id?: string
  security_ip_list?: string | null
  reclaim_policy?: 'destroy' | 'sanitize_and_reuse'
  permission_template_revision_id?: string
  delete_cooldown_duration_hours?: number | null
  available_health_check_interval_seconds?: number
  available_health_stale_after_seconds?: number
}

export const listDedicatedPools = () =>
  api.get<DedicatedPool[]>('/api/dedicated-pools')

export const getDedicatedPool = (poolId: string) =>
  api.get<DedicatedPool>(
    `/api/dedicated-pools/${encodeURIComponent(poolId)}`,
  )

export const createDedicatedPool = (input: CreateDedicatedPoolInput) =>
  api.post<DedicatedPool>('/api/dedicated-pools', input)

export const updateDedicatedPool = (
  poolId: string,
  input: UpdateDedicatedPoolInput,
) =>
  api.patch<DedicatedPool>(
    `/api/dedicated-pools/${encodeURIComponent(poolId)}`,
    input,
  )

export const getDedicatedPurchaseProfile = () =>
  api.get<DedicatedPurchaseProfile>('/api/dedicated-pools/purchase-profile')

export const getDedicatedReadiness = () =>
  api.get<DedicatedReadiness>('/api/dedicated-pools/readiness')

export const upgradeDedicatedPurchaseProfile = (
  poolId: string,
  expectedConfigRevision: number,
) =>
  api.post<DedicatedPool>(
    `/api/dedicated-pools/${encodeURIComponent(poolId)}/purchase-profile/upgrade`,
    { expected_config_revision: expectedConfigRevision },
  )

export const drainDedicatedPool = (poolId: string) =>
  api.post<DedicatedPool>(
    `/api/dedicated-pools/${encodeURIComponent(poolId)}/drain`,
  )

export const updateDedicatedMember = (
  poolId: string,
  memberId: string,
  input: { delete_cooldown_duration_hours: number | null },
) =>
  api.patch<DedicatedPoolMember>(
    `/api/dedicated-pools/${encodeURIComponent(poolId)}/members/${encodeURIComponent(memberId)}`,
    input,
  )

export const runDedicatedMemberAction = (
  poolId: string,
  memberId: string,
  action: 'retry' | 'quarantine' | 'destroy',
) =>
  api.post<DedicatedPoolMember>(
    `/api/dedicated-pools/${encodeURIComponent(poolId)}/members/${encodeURIComponent(memberId)}/actions/${action}`,
  )
