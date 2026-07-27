import { Select } from 'antd'

import type { InstanceSummary } from '../../api/instanceAccess'

export interface InstanceEligibility {
  eligible: boolean
  reason?: string
}

export interface InstancePickerProps {
  instances: InstanceSummary[]
  value?: string
  onChange: (instanceId: string) => void
  getEligibility?: (instance: InstanceSummary) => InstanceEligibility
  disabled?: boolean
  loading?: boolean
  placeholder?: string
  ariaLabel?: string
}

export default function InstancePicker({
  instances,
  value,
  onChange,
  getEligibility,
  disabled = false,
  loading = false,
  placeholder = 'Select a database instance',
  ariaLabel = 'Database instance',
}: InstancePickerProps) {
  const options = instances.map((instance) => {
    const eligibility = getEligibility?.(instance) ?? { eligible: true }
    const context = [
      instance.cluster_id,
      instance.engine,
      instance.topology,
      instance.status,
      eligibility.reason,
    ]
      .filter(Boolean)
      .join(' · ')

    return {
      value: instance.id,
      label: `${instance.name} · ${context}`,
      disabled: !eligibility.eligible,
    }
  })

  return (
    <Select
      aria-label={ariaLabel}
      value={value || undefined}
      onChange={onChange}
      options={options}
      optionFilterProp="label"
      showSearch
      disabled={disabled}
      loading={loading}
      placeholder={placeholder}
      style={{ width: '100%' }}
    />
  )
}
