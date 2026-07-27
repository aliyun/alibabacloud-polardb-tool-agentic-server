import api from './client'

export type CredentialPurpose =
  | 'provisioning_admin'
  | 'direct_access'
  | 'resource_access'
export type CredentialCapability = 'readonly' | 'readwrite' | 'admin'
export type CredentialStatus = 'active' | 'revoked'

export interface InstanceCredential {
  id: string
  instance_id: string | null
  resource_id: string | null
  name: string
  purpose: CredentialPurpose
  capability: CredentialCapability
  database_name: string | null
  status: CredentialStatus
  version: number
  created_by_user_id: string | null
  created_at: string
  updated_at: string | null
}

export interface CreateCredentialInput {
  name: string
  purpose: 'provisioning_admin' | 'direct_access'
  capability: CredentialCapability
  username: string
  password: string
  database_name?: string | null
}

export interface UpdateCredentialInput {
  expected_version: number
  name: string
  capability: CredentialCapability
  username?: string
  password?: string
  database_name: string | null
}

export interface TestCredentialConnectionInput {
  purpose: 'provisioning_admin' | 'direct_access'
  capability: CredentialCapability
  credential_id?: string
  expected_version?: number
  username?: string
  password?: string
  database_name?: string | null
}

export interface CredentialRevealRequest {
  confirmed: true
}

export interface RevealedCredential {
  username: string
  password: string
  database_name: string | null
}

export const listInstanceCredentials = (instanceId: string) =>
  api.get<InstanceCredential[]>(
    `/api/instances/${encodeURIComponent(instanceId)}/credentials`,
  )

export const createInstanceCredential = (
  instanceId: string,
  input: CreateCredentialInput,
) =>
  api.post<InstanceCredential>(
    `/api/instances/${encodeURIComponent(instanceId)}/credentials`,
    input,
  )

export const testInstanceCredentialConnection = (
  instanceId: string,
  input: TestCredentialConnectionInput,
) =>
  api.post<{ ok: true }>(
    `/api/instances/${encodeURIComponent(instanceId)}/credentials/test-connection`,
    input,
  )

export const revealCredential = (
  credentialId: string,
  request: CredentialRevealRequest,
) =>
  api.post<RevealedCredential>(
    `/api/credentials/${encodeURIComponent(credentialId)}/reveal`,
    request,
  )

export const updateCredential = (
  credentialId: string,
  input: UpdateCredentialInput,
) =>
  api.put<InstanceCredential>(
    `/api/credentials/${encodeURIComponent(credentialId)}`,
    input,
  )

export const revokeCredential = (credentialId: string) =>
  api.post<InstanceCredential>(
    `/api/credentials/${encodeURIComponent(credentialId)}/revoke`,
  )
