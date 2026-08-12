import api from './client'

export type ModuleState =
  | 'NOT_CONFIGURED'
  | 'DRAFT'
  | 'VALIDATING'
  | 'VALIDATED'
  | 'ACTIVE'
  | 'ERROR'
  | 'DISABLED'
  | 'SKIPPED'

export interface ConfigModule {
  name: string
  revision: number
  workflow_state: ModuleState
  desired_state?: ModuleState | null
  last_error_code?: string | null
  draft?: Record<string, unknown> | null
  effective?: {
    revision: number
    state: ModuleState
    config: Record<string, unknown>
  } | null
  schema: {
    type?: string
    properties?: Record<string, JSONSchemaProperty>
    required?: string[]
  }
  ui_hints?: {
    secret_fields?: string[]
    docs?: { label: string; url: string; description?: string }[]
  }
  dependencies: string[]
  dependents: string[]
  configurable?: boolean
}

export interface JSONSchemaProperty {
  type?: string
  title?: string
  description?: string
  default?: unknown
  enum?: unknown[]
  minimum?: number
  maximum?: number
  minLength?: number
  maxLength?: number
  items?: { type?: string }
  anyOf?: JSONSchemaProperty[]
}

export interface SecretMarker {
  configured: true
  display_hint?: string
  updated_at?: string
}

export interface SecretClearAction {
  $secret_action: 'clear'
}

export type AliyunCredentialMode = 'direct_ak' | 'assume_role' | 'ecs_ram_role'

export interface AliyunCredentialTransition {
  previous_mode_action: 'clear' | 'retain'
  selected_mode_action: 'replace' | 'reuse_retained'
  reuse_direct_ak_as_assume_source: boolean
  delete_retained_modes: AliyunCredentialMode[]
}

export interface AliyunAccessMutation {
  credential_mode: AliyunCredentialMode
  region_id?: string
  openapi_network?: 'public' | 'vpc'
  direct_ak?: { access_key_id: string; access_key_secret: string }
  assume_role?: {
    source_access_key_id?: string
    source_access_key_secret?: string
    role_arn: string
    role_session_name?: string
    duration_seconds?: number
    external_id?: string | SecretClearAction
  }
  ecs_ram_role?: { role_name?: string | null; metadata_policy: 'v2_only' }
  transition: AliyunCredentialTransition
}

export interface ConfigResponse {
  config_version: number
  system_state: 'SETUP' | 'READY'
  module?: ConfigModule
  modules?: ConfigModule[]
  validation?: {
    status: string
    validation_id?: string
    expires_at?: string
    external_validation?: ExternalValidation
  }
  plan?: {
    valid: boolean
    message?: string | null
    error_code?: string | null
    confirmation_allowed?: boolean
    confirmation_error_code?: string | null
    request_id?: string | null
    writes: boolean
    dependencies?: string[]
    external_validation?: ExternalValidation
  }
}

export interface ExternalValidation {
  status: string
  checks: {
    service: string
    network: string
    endpoint: string
    status: string
    identity_hint?: string
    expires_at?: number
    request_id?: string
  }[]
}

export interface ConfigCommand {
  protocol_version?: 1
  action: string
  module?: string
  expected_revision?: number
  idempotency_key?: string
  validation_id?: string
  confirm_impact?: boolean
  config?: Record<string, unknown>
}

export async function discoverSystemState(): Promise<'SETUP' | 'READY'> {
  const response = await api.get('/readyz', {
    headers: { 'X-PAS-Setup-Discovery': '1' },
  })
  const mode = response.data?.mode
  if (mode !== 'SETUP' && mode !== 'READY') {
    throw new Error('Invalid server readiness response')
  }
  return mode
}

export async function executeConfig(
  command: ConfigCommand,
  bootstrapToken?: string,
): Promise<ConfigResponse> {
  const response = await api.post(
    '/api/config',
    { protocol_version: 1, ...command },
    {
      headers: bootstrapToken
        ? { Authorization: `Bootstrap ${bootstrapToken}` }
        : undefined,
      pasSkipAuthRedirect: true,
    },
  )
  return response.data
}
