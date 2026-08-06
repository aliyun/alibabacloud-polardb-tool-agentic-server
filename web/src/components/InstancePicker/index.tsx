import { Select } from 'antd'
import { useTranslation } from 'react-i18next'

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
  placeholder,
  ariaLabel,
}: InstancePickerProps) {
  const { t } = useTranslation()
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
      aria-label={ariaLabel ?? t('components.instancePicker.label')}
      value={value || undefined}
      onChange={onChange}
      options={options}
      optionFilterProp="label"
      showSearch
      disabled={disabled}
      loading={loading}
      placeholder={placeholder ?? t('components.instancePicker.placeholder')}
      style={{ width: '100%' }}
    />
  )
}
