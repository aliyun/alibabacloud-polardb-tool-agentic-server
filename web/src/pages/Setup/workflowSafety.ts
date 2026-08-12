export type SafeCredentialMode = 'direct_ak' | 'assume_role' | 'ecs_ram_role'

export type SafeDryRunGuidance =
  | 'adminPassword'
  | 'moduleConfig'
  | 'externalBaseUrl'
  | 'dependency'
  | 'endpoint'
  | 'external'

export interface SafeDryRunCheck {
  service: 'sts' | 'polardb' | 'ecs_metadata'
  endpoint: string
  status: 'REACHABLE' | 'WARNING' | 'PASSED' | 'SKIPPED'
  identityHint?: string
  expiresAt?: number
  requestId?: string
}

export interface SafeDryRunDetails {
  valid: boolean
  credentialMode?: SafeCredentialMode
  errorCode?: string
  requestId?: string
  checks: SafeDryRunCheck[]
  guidance?: SafeDryRunGuidance
}

export interface ActivationProof {
  savedRevision: number
  validatedRevision: number
  validationId: string
}

const externalErrorCodes = new Set([
  'OPENAPI_CONNECT_FAILURE',
  'OPENAPI_CREDENTIAL_INVALID',
  'OPENAPI_DNS_FAILURE',
  'OPENAPI_ECS_IMDSV2_UNAVAILABLE',
  'OPENAPI_ECS_METADATA_DISABLED',
  'OPENAPI_ECS_RAM_ROLE_NOT_ATTACHED',
  'OPENAPI_ENDPOINT_UNSUPPORTED',
  'OPENAPI_PERMISSION_DENIED',
  'OPENAPI_STS_ASSUME_ROLE_DENIED',
  'OPENAPI_STS_EXTERNAL_ID_MISMATCH',
  'OPENAPI_STS_ROLE_TRUST_REJECTED',
  'OPENAPI_STS_SOURCE_CREDENTIAL_INVALID',
  'OPENAPI_TEMPORARY_CREDENTIAL_EXPIRED',
  'OPENAPI_TLS_FAILURE',
])

const localErrorGuidance: Record<string, SafeDryRunGuidance> = {
  INVALID_ADMIN_PASSWORD: 'adminPassword',
  INVALID_MODULE_CONFIG: 'moduleConfig',
  EXTERNAL_BASE_URL_REQUIRED: 'externalBaseUrl',
  DEPENDENCY_NOT_ACTIVE: 'dependency',
  OPENAPI_ENDPOINT_UNSUPPORTED: 'endpoint',
}

const credentialModes = new Set<SafeCredentialMode>([
  'direct_ak',
  'assume_role',
  'ecs_ram_role',
])
const checkStatuses = new Set<SafeDryRunCheck['status']>([
  'REACHABLE',
  'WARNING',
  'PASSED',
  'SKIPPED',
])
const requestIdPattern = /^[A-Za-z0-9._:-]{1,128}$/
const roleIdentityHintPattern = /^(?:\*\*|[A-Za-z0-9._-]{2}\*\*\*)$/
const accessKeyIdentityHintPattern = /^(?:\*\*\*\*|[A-Za-z0-9_-]{4}\*\*\*\*[A-Za-z0-9_-]{4})$/
const maxEpochSeconds = 4_102_444_800
const regionPattern = /^[a-z]{2}(?:-[a-z0-9]+)+$/
const maxHostnameLength = 253
const maxDnsLabelLength = 63

function record(value: unknown): Record<string, unknown> | undefined {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
}

function integer(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isSafeInteger(value)
    ? value
    : undefined
}

function safeRequestId(value: unknown): string | undefined {
  return typeof value === 'string' && requestIdPattern.test(value)
    ? value
    : undefined
}

function safeMode(value: unknown): SafeCredentialMode | undefined {
  return typeof value === 'string' && credentialModes.has(value as SafeCredentialMode)
    ? value as SafeCredentialMode
    : undefined
}

function safeErrorCode(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  return externalErrorCodes.has(value) || value in localErrorGuidance
    ? value
    : undefined
}

function isValidRegionalEndpoint(
  value: string,
  servicePrefixes: readonly string[],
): boolean {
  if (value.length > maxHostnameLength) return false
  const labels = value.split('.')
  if (labels.length !== 4 || labels.some((label) => (
    label.length === 0 || label.length > maxDnsLabelLength
  ))) {
    return false
  }
  const [service, region, provider, tld] = labels
  return servicePrefixes.includes(service)
    && regionPattern.test(region)
    && provider === 'aliyuncs'
    && tld === 'com'
}

function safeEndpoint(service: SafeDryRunCheck['service'], value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  if (
    service === 'sts'
    && isValidRegionalEndpoint(value, ['sts', 'sts-vpc'])
  ) return value
  if (
    service === 'polardb'
    && (
      value === 'polardb.aliyuncs.com'
      || isValidRegionalEndpoint(value, ['polardb', 'polardb-vpc'])
    )
  ) return value
  if (service === 'ecs_metadata' && value === 'http://100.100.100.200') return value
  return undefined
}

function safeIdentityHint(
  service: SafeDryRunCheck['service'],
  value: unknown,
): string | undefined {
  if (typeof value !== 'string') return undefined
  const pattern = service === 'polardb'
    ? accessKeyIdentityHintPattern
    : roleIdentityHintPattern
  return pattern.test(value) ? value : undefined
}

function normalizeCheck(value: unknown): SafeDryRunCheck | undefined {
  const source = record(value)
  if (!source || typeof source.service !== 'string') return undefined
  if (source.service !== 'sts' && source.service !== 'polardb' && source.service !== 'ecs_metadata') {
    return undefined
  }
  if (typeof source.status !== 'string' || !checkStatuses.has(source.status as SafeDryRunCheck['status'])) {
    return undefined
  }
  const endpoint = safeEndpoint(source.service, source.endpoint)
  if (!endpoint) return undefined

  const check: SafeDryRunCheck = {
    service: source.service,
    endpoint,
    status: source.status as SafeDryRunCheck['status'],
  }
  const identityHint = safeIdentityHint(source.service, source.identity_hint)
  if (identityHint) check.identityHint = identityHint
  const expiresAt = integer(source.expires_at)
  if (expiresAt !== undefined && expiresAt >= 0 && expiresAt <= maxEpochSeconds) {
    check.expiresAt = expiresAt
  }
  const requestId = safeRequestId(source.request_id)
  if (requestId) check.requestId = requestId
  return check
}

export function normalizeDryRunDetails(
  rawPlan: unknown,
  submittedMode: unknown,
): SafeDryRunDetails {
  const plan = record(rawPlan)
  const errorCode = safeErrorCode(plan?.error_code)
  const externalValidation = record(plan?.external_validation)
  const rawChecks = Array.isArray(externalValidation?.checks)
    ? externalValidation.checks
    : []
  const checks = rawChecks
    .map(normalizeCheck)
    .filter((check): check is SafeDryRunCheck => check !== undefined)
  const guidance = errorCode
    ? (localErrorGuidance[errorCode] ?? (externalErrorCodes.has(errorCode) ? 'external' : undefined))
    : undefined

  return {
    valid: plan?.valid === true,
    credentialMode: safeMode(submittedMode),
    ...(errorCode ? { errorCode } : {}),
    ...(safeRequestId(plan?.request_id) ? { requestId: safeRequestId(plan?.request_id) } : {}),
    checks,
    ...(guidance ? { guidance } : {}),
  }
}

export function isConfirmableExternalFailure(
  moduleName: unknown,
  rawPlan: unknown,
): boolean {
  const plan = record(rawPlan)
  const errorCode = safeErrorCode(plan?.error_code)
  return moduleName === 'aliyun_access'
    && plan?.valid === false
    && plan.confirmation_allowed === true
    && errorCode !== undefined
    && externalErrorCodes.has(errorCode)
    && errorCode !== 'OPENAPI_ENDPOINT_UNSUPPORTED'
}

export function parseActivationProof(
  savedResponse: unknown,
  validatedResponse: unknown,
): ActivationProof | undefined {
  const savedRevision = getResponseRevision(savedResponse)
  const validated = record(validatedResponse)
  const validatedModule = record(validated?.module)
  const validation = record(validated?.validation)
  const validatedRevision = integer(validatedModule?.revision)
  const validationId = validation?.validation_id

  if (
    savedRevision === undefined
    || validatedRevision === undefined
    || validation?.status !== 'PASSED'
    || typeof validationId !== 'string'
    || validationId.length === 0
  ) {
    return undefined
  }
  return { savedRevision, validatedRevision, validationId }
}

export function getResponseRevision(response: unknown): number | undefined {
  return integer(record(record(response)?.module)?.revision)
}
