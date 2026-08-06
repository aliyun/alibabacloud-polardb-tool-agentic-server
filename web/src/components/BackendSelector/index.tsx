import { Select } from 'antd'
import { useTranslation } from 'react-i18next'

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

export default function BackendSelector({
  backends,
  instanceNames = {},
  value,
  onChange,
  allowInactive = false,
  disabled = false,
  loading = false,
}: BackendSelectorProps) {
  const { t } = useTranslation()
  const options = backends.map((backend) => {
    const active = backend.status === 'active'
    const name =
      instanceNames[backend.instance_id] ?? t('components.backendSelector.instanceFallback', { id: backend.instance_id })
    const eligibility = active ? t('components.backendSelector.accepting') : t('components.backendSelector.notAccepting')

    return {
      value: backend.id,
      disabled: !allowInactive && !active,
      label: `${name} · ${backend.status} · ${eligibility}`,
    }
  })

  return (
    <Select
      aria-label={t('components.backendSelector.label')}
      value={value || undefined}
      onChange={onChange}
      options={options}
      optionFilterProp="label"
      showSearch
      disabled={disabled}
      loading={loading}
      placeholder={t('components.backendSelector.placeholder')}
      style={{ width: '100%' }}
    />
  )
}
