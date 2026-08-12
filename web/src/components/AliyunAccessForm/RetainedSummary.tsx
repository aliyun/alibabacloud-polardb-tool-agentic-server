import { Space, Typography } from 'antd'
import type { TFunction } from 'i18next'

import type { AliyunCredentialMode, SecretMarker } from '../../api/configuration'

interface Props {
  mode: AliyunCredentialMode
  block: Record<string, unknown>
  locale: string
  t: TFunction
}

function marker(value: unknown): SecretMarker | undefined {
  return typeof value === 'object' && value !== null && (value as SecretMarker).configured === true
    ? value as SecretMarker
    : undefined
}

export function RetainedSummary({ mode, block, locale, t }: Props) {
  if (mode === 'ecs_ram_role') {
    const role = typeof block.role_name === 'string' && block.role_name
      ? block.role_name
      : t('components.aliyunAccess.ecsRoleDiscovered')
    return <Typography.Text type="secondary">{t('components.aliyunAccess.retainedEcsSummary', { role })}</Typography.Text>
  }

  const identifier = marker(mode === 'direct_ak' ? block.access_key_id : block.source_access_key_id)
  const secret = marker(mode === 'direct_ak' ? block.access_key_secret : block.source_access_key_secret)
  const updatedAt = identifier?.updated_at ?? secret?.updated_at
  return (
    <Space size={8} wrap>
      <Typography.Text type="secondary">{identifier?.display_hint ?? t('components.aliyunAccess.secretConfigured')}</Typography.Text>
      {updatedAt && <Typography.Text type="secondary">{t('components.aliyunAccess.updatedAt', { value: new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(updatedAt)) })}</Typography.Text>}
    </Space>
  )
}
