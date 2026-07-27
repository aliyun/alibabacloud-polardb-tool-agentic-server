import { Select } from 'antd'

import type { ProvisioningBackend } from '../../api/provisioningBackends'

export interface BackendSelectorProps {
  backends: ProvisioningBackend[]
  instanceNames?: Record<string, string>
  value?: string
  onChange: (backendId: string) => void
  allowInactive?: boolean
  disabled?: boolean
  loading?: boolean
}

const INELIGIBLE_REASON = 'Not accepting new resources'

export default function BackendSelector({
  backends,
  instanceNames = {},
  value,
  onChange,
  allowInactive = false,
  disabled = false,
  loading = false,
}: BackendSelectorProps) {
  const options = backends.map((backend) => {
    const active = backend.status === 'active'
    const name =
      instanceNames[backend.instance_id] ?? `Instance ${backend.instance_id}`
    const eligibility = active ? 'Accepting new resources' : INELIGIBLE_REASON

    return {
      value: backend.id,
      disabled: !allowInactive && !active,
      label: `${name} · ${backend.status} · ${eligibility}`,
    }
  })

  return (
    <Select
      aria-label="Provisioning backend"
      value={value || undefined}
      onChange={onChange}
      options={options}
      optionFilterProp="label"
      showSearch
      disabled={disabled}
      loading={loading}
      placeholder="Select a provisioning backend"
      style={{ width: '100%' }}
    />
  )
}
