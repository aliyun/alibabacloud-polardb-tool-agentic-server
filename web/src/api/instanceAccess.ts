import api from './client'
import { listAllAdminInstances } from './instances'

export type { InstanceSummary } from './instances'

export type Permission = 'readonly' | 'readwrite'
export type DirectBindingCapability =
  | 'db_instance:list'
  | 'db_instance:describe'
  | 'db_instance:credentials:read'
export type SqlCapability = 'sql:read' | 'sql:write'
export type BindingCapability = DirectBindingCapability | SqlCapability
export type ProvisioningCapability = 'db_instance:create'
export type AgentInstanceAccessCapability =
  | BindingCapability
  | ProvisioningCapability
export type BindingOrigin = 'system' | 'admin'
export type CreateAvailability =
  | 'available'
  | 'backend_required'
  | 'backend_inactive'
  | 'backend_unhealthy'
  | 'instance_ineligible'

export interface AgentInstanceAccessInput {
  instance_id: string
  credential_id: string | null
  permission: Permission | null
  direct_enabled: boolean | null
  capabilities: AgentInstanceAccessCapability[]
}

export interface AgentInstanceAccess extends AgentInstanceAccessInput {
  agent_id: string
  direct_binding_id: string | null
  provisioning_binding_id: string | null
  provisioning_backend_id: string | null
  create_availability: CreateAvailability
}

export type AgentInstanceAccessUpdate = Omit<
  AgentInstanceAccessInput,
  'instance_id'
>

export interface DirectBindingInput {
  instance_id: string
  credential_id: string
  permission: Permission
  capabilities: BindingCapability[]
  enabled: boolean
}

export type DirectBindingUpdate = Omit<DirectBindingInput, 'instance_id'>

export interface DirectBinding
  extends Omit<DirectBindingInput, 'capabilities'> {
  id: string
  agent_id: string
  capabilities: BindingCapability[]
  created_by_user_id: string
  created_at: string
  updated_at: string | null
}

export interface ProvisioningBindingInput {
  backend_id: string
  enabled: boolean
}

export interface ProvisioningBinding {
  id: string
  agent_id: string
  backend_id: string
  enabled: boolean
  backend_status: 'active' | 'draining' | 'disabled'
  allow_create: boolean
  created_by_user_id: string
  created_at: string
  updated_at: string | null
}

export interface InstanceAccessInput {
  credential_id: string
  permission: Permission
  capabilities: BindingCapability[]
  enabled: boolean
}

export interface InstanceAccess {
  id: string
  user_id: string
  instance_id: string
  credential_id: string | null
  permission: Permission
  capabilities: BindingCapability[]
  enabled: boolean
  origin: BindingOrigin
  created_at: string
  updated_at: string | null
}

export interface AgentResource {
  id: string
  backend_id: string
  client_token: string
  name: string | null
  engine: string
  status: string
  created_at: string
  updated_at: string | null
}

export const listInstances = () => listAllAdminInstances()

export const listAgentInstanceAccess = (agentId: string) =>
  api.get<AgentInstanceAccess[]>(
    `/api/agents/${encodeURIComponent(agentId)}/instance-bindings`,
  )

export const createAgentInstanceAccess = (
  agentId: string,
  input: AgentInstanceAccessInput,
) =>
  api.post<AgentInstanceAccess>(
    `/api/agents/${encodeURIComponent(agentId)}/instance-bindings`,
    input,
  )

export const updateAgentInstanceAccess = (
  agentId: string,
  instanceId: string,
  input: AgentInstanceAccessUpdate,
) =>
  api.put<AgentInstanceAccess>(
    `/api/agents/${encodeURIComponent(agentId)}/instance-bindings/${encodeURIComponent(instanceId)}`,
    input,
  )

export const deleteAgentInstanceAccess = (
  agentId: string,
  instanceId: string,
) =>
  api.delete(
    `/api/agents/${encodeURIComponent(agentId)}/instance-bindings/${encodeURIComponent(instanceId)}`,
  )

export const listDirectBindings = (agentId: string) =>
  api.get<DirectBinding[]>(
    `/api/agents/${encodeURIComponent(agentId)}/instance-bindings`,
  )

export const createDirectBinding = (
  agentId: string,
  input: DirectBindingInput,
) =>
  api.post<DirectBinding>(
    `/api/agents/${encodeURIComponent(agentId)}/instance-bindings`,
    input,
  )

export const updateDirectBinding = (
  agentId: string,
  bindingId: string,
  input: DirectBindingUpdate,
) =>
  api.put<DirectBinding>(
    `/api/agents/${encodeURIComponent(agentId)}/instance-bindings/${encodeURIComponent(bindingId)}`,
    input,
  )

export const deleteDirectBinding = (agentId: string, bindingId: string) =>
  api.delete(
    `/api/agents/${encodeURIComponent(agentId)}/instance-bindings/${encodeURIComponent(bindingId)}`,
  )

export const listProvisioningBindings = (agentId: string) =>
  api.get<ProvisioningBinding[]>(
    `/api/agents/${encodeURIComponent(agentId)}/provisioning-bindings`,
  )

export const createProvisioningBinding = (
  agentId: string,
  input: ProvisioningBindingInput,
) =>
  api.post<ProvisioningBinding>(
    `/api/agents/${encodeURIComponent(agentId)}/provisioning-bindings`,
    input,
  )

export const updateProvisioningBinding = (
  agentId: string,
  bindingId: string,
  enabled: boolean,
) =>
  api.put<ProvisioningBinding>(
    `/api/agents/${encodeURIComponent(agentId)}/provisioning-bindings/${encodeURIComponent(bindingId)}`,
    { enabled },
  )

export const deleteProvisioningBinding = (
  agentId: string,
  bindingId: string,
) =>
  api.delete(
    `/api/agents/${encodeURIComponent(agentId)}/provisioning-bindings/${encodeURIComponent(bindingId)}`,
  )

export const listAgentResources = (agentId: string) =>
  api.get<AgentResource[]>(
    `/api/agents/${encodeURIComponent(agentId)}/resources`,
  )

export const getUserInstanceAccess = (userId: string, instanceId: string) =>
  api.get<InstanceAccess>(
    `/api/users/${encodeURIComponent(userId)}/instance-access/${encodeURIComponent(instanceId)}`,
  )

export const updateUserInstanceAccess = (
  userId: string,
  instanceId: string,
  input: InstanceAccessInput,
) =>
  api.put<InstanceAccess>(
    `/api/users/${encodeURIComponent(userId)}/instance-access/${encodeURIComponent(instanceId)}`,
    input,
  )
