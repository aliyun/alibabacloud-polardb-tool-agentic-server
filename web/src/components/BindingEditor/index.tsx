import { useEffect, useRef } from 'react'
import { Alert, Select, Space, Switch, Typography } from 'antd'

import type { InstanceCredential } from '../../api/credentials'
import type {
  AgentInstanceAccessCapability,
  AgentInstanceAccessInput,
  DirectBindingInput,
  InstanceSummary,
  Permission,
  SqlCapability,
} from '../../api/instanceAccess'
import type { ProvisioningBackend } from '../../api/provisioningBackends'
import CapabilityEditor from '../CapabilityEditor'
import InstancePicker from '../InstancePicker'

const { Text } = Typography
const SQL_CAPABILITIES: SqlCapability[] = ['sql:read', 'sql:write']

function capabilitiesForPermission(
  capabilities: AgentInstanceAccessCapability[],
  permission: Permission,
): AgentInstanceAccessCapability[] {
  const sqlProxyEnabled = SQL_CAPABILITIES.some((capability) =>
    capabilities.includes(capability),
  )
  const nonSql = capabilities.filter(
    (capability) => !SQL_CAPABILITIES.includes(capability as SqlCapability),
  )
  if (!sqlProxyEnabled) return nonSql
  return [
    ...nonSql,
    'sql:read',
    ...(permission === 'readwrite'
      ? (['sql:write'] as const)
      : []),
  ]
}

type BindingValue = DirectBindingInput | AgentInstanceAccessInput

export interface BindingEditorProps<T extends BindingValue = DirectBindingInput> {
  value: T
  onChange: (binding: T) => void
  onValidityChange: (valid: boolean) => void
  instances: InstanceSummary[]
  credentials: InstanceCredential[]
  provisioningBackends?: ProvisioningBackend[]
  mode?: 'create' | 'edit'
  disabled?: boolean
}

export default function BindingEditor<T extends BindingValue = DirectBindingInput>({
  value,
  onChange,
  onValidityChange,
  instances,
  credentials,
  provisioningBackends = [],
  mode = 'create',
  disabled = false,
}: BindingEditorProps<T>) {
  const aggregate = 'direct_enabled' in value
  const directCapabilities = value.capabilities.filter(
    (capability) => capability !== 'db_instance:create',
  )
  const directAccessRequested = directCapabilities.length > 0
  const eligibleCredentials = credentials.filter(
    (credential) =>
      credential.instance_id === value.instance_id &&
      credential.purpose === 'direct_access' &&
      credential.capability !== 'admin' &&
      credential.status === 'active',
  )
  const selectedCredential = value.credential_id
    ? eligibleCredentials.find(({ id }) => id === value.credential_id)
    : undefined
  let validationError: string | null = null
  if (!value.instance_id) {
    validationError = 'Select a database instance.'
  } else if (
    value.capabilities.length === 0 &&
    !(aggregate && mode === 'edit')
  ) {
    validationError = 'Select at least one capability.'
  } else if (directAccessRequested && !selectedCredential) {
    validationError =
      'Select an active direct-access credential for this instance.'
  } else if (
    directAccessRequested &&
    value.permission === 'readwrite' &&
    selectedCredential?.capability !== 'readwrite'
  ) {
    validationError =
      'Read and write permission exceeds the selected credential capability.'
  }
  const valid = validationError === null
  const validityCallbackRef = useRef(onValidityChange)
  const lastReportedValidityRef = useRef<boolean | undefined>(undefined)

  useEffect(() => {
    validityCallbackRef.current = onValidityChange
  }, [onValidityChange])

  useEffect(() => {
    if (lastReportedValidityRef.current === valid) return
    lastReportedValidityRef.current = valid
    validityCallbackRef.current(valid)
  }, [valid])

  const update = (changes: Partial<T>) => {
    onChange({ ...value, ...changes })
  }

  const handleInstanceChange = (instanceId: string) => {
    const currentCredentialIsEligible = credentials.some(
      (credential) =>
        credential.id === value.credential_id &&
        credential.instance_id === instanceId &&
        credential.purpose === 'direct_access' &&
        credential.capability !== 'admin' &&
        credential.status === 'active',
    )
    const permission =
      currentCredentialIsEligible && value.permission
        ? value.permission
        : 'readonly'
    update({
      instance_id: instanceId,
      credential_id: currentCredentialIsEligible
        ? value.credential_id
        : aggregate
          ? null
          : '',
      permission,
      capabilities: capabilitiesForPermission(
        value.capabilities,
        permission,
      ),
    } as Partial<T>)
  }

  const handleCredentialChange = (credentialId: string) => {
    const credential = credentials.find(({ id }) => id === credentialId)
    const permission =
      credential?.capability === 'readonly'
        ? 'readonly'
        : (value.permission ?? 'readonly')
    const firstAggregateCredential = aggregate && !value.credential_id
    const capabilities = firstAggregateCredential
      ? [
          ...value.capabilities.filter(
            (capability) =>
              !SQL_CAPABILITIES.includes(capability as SqlCapability),
          ),
          ...SQL_CAPABILITIES.filter(
            (capability) =>
              capability === 'sql:read' || permission === 'readwrite',
          ),
        ]
      : capabilitiesForPermission(value.capabilities, permission)
    update({
      credential_id: credentialId,
      permission,
      ...(firstAggregateCredential ? { direct_enabled: true } : {}),
      capabilities,
    } as Partial<T>)
  }

  const permissionOptions: Array<{ value: Permission; label: string }> = [
    { value: 'readonly', label: 'Read only' },
    {
      value: 'readwrite',
      label: 'Read and write',
    },
  ]
  if (selectedCredential?.capability !== 'readwrite') {
    permissionOptions[1] = {
      ...permissionOptions[1],
      label: 'Read and write · credential does not allow this',
    }
  }
  const selectedInstance = instances.find(
    ({ id }) => id === value.instance_id,
  )
  const provisioningBackend = provisioningBackends.find(
    ({ instance_id }) => instance_id === value.instance_id,
  )
  const showDirectFields = !aggregate || directAccessRequested

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <div>
        <Text strong>Instance</Text>
        <InstancePicker
          instances={instances}
          value={value.instance_id}
          onChange={handleInstanceChange}
          getEligibility={(instance) =>
            instance.status === 'active' || instance.status === 'stopped'
              ? { eligible: true }
              : { eligible: false, reason: 'not available for binding' }
          }
          disabled={disabled || mode === 'edit'}
        />
      </div>

      {showDirectFields && (
        <>
          <div>
            <Text strong>Credential</Text>
            <Select
              aria-label="Direct access credential"
              value={value.credential_id || undefined}
              onChange={handleCredentialChange}
              disabled={disabled || !value.instance_id}
              placeholder={
                value.instance_id
                  ? 'Select an active direct-access credential'
                  : 'Select an instance first'
              }
              options={eligibleCredentials.map((credential) => ({
                value: credential.id,
                label: `${credential.name} · ${credential.capability}`,
              }))}
              style={{ width: '100%' }}
            />
          </div>

          <div>
            <Text strong>Permission</Text>
            <Select
              aria-label="Binding permission"
              value={value.permission ?? undefined}
              onChange={(permission) =>
                update({
                  permission,
                  capabilities: capabilitiesForPermission(
                    value.capabilities,
                    permission,
                  ),
                } as Partial<T>)
              }
              disabled={disabled}
              options={permissionOptions.map((option, index) => ({
                ...option,
                disabled:
                  index === 1 &&
                  selectedCredential?.capability !== 'readwrite',
              }))}
              style={{ width: '100%' }}
            />
          </div>
        </>
      )}

      <div>
        <Text strong>Capabilities</Text>
        <CapabilityEditor
          permission={value.permission ?? undefined}
          value={value.capabilities}
          onChange={(capabilities) => {
            const hasDirectCapabilities = capabilities.some(
              (capability) => capability !== 'db_instance:create',
            )
            update({
              capabilities,
              ...(aggregate && !hasDirectCapabilities
                ? {
                    credential_id: null,
                    permission: null,
                    direct_enabled: null,
                  }
                : {}),
            } as Partial<T>)
          }}
          disabled={disabled}
          instance={selectedInstance}
          provisioningBackend={provisioningBackend}
        />
      </div>

      {!aggregate && (
        <Space>
          <Switch
            aria-label="Binding enabled"
            checked={value.enabled}
            onChange={(enabled) =>
              onChange({ ...value, enabled } as T)
            }
            disabled={disabled}
          />
          <Text>Binding enabled</Text>
        </Space>
      )}

      {validationError && (
        <Alert
          type="error"
          showIcon
          role="alert"
          message="Binding cannot be saved"
          description={validationError}
        />
      )}
    </Space>
  )
}
