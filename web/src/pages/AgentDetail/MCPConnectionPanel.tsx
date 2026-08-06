import { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Descriptions,
  Skeleton,
  Space,
  Tag,
  Typography,
} from 'antd'
import { useTranslation } from 'react-i18next'

import type { AgentTokenStatus } from '../../api/agents'
import { buildMCPClientConfiguration } from './mcpConnection'
import { formatDateTime } from '../../i18n/format'

const { Text, Title } = Typography

type CopyResult =
  | { status: 'success'; message: string }
  | { status: 'error'; message: string }
  | null

export interface MCPConnectionPanelProps {
  agentName: string
  mcpUrl: string
  token: string | null
  loading: boolean
  error: string | null
  tokenStatus: AgentTokenStatus | null
  expiresAt: string | null
  lastUsedAt: string | null
  onRetry: () => void
  onRegenerate: () => void
  onRevoke: () => void
}

export default function MCPConnectionPanel({
  agentName,
  mcpUrl,
  token,
  loading,
  error,
  tokenStatus,
  expiresAt,
  lastUsedAt,
  onRetry,
  onRegenerate,
  onRevoke,
}: MCPConnectionPanelProps) {
  const { t, i18n } = useTranslation()
  const [copyResult, setCopyResult] = useState<CopyResult>(null)
  const copyDisabled =
    loading || !!error || tokenStatus !== 'active' || token === null

  useEffect(() => {
    setCopyResult(null)
  }, [agentName, mcpUrl, token])

  const copyConfiguration = async () => {
    if (copyDisabled || !token) return
    try {
      if (!navigator.clipboard) throw new Error('Clipboard unavailable')
      await navigator.clipboard.writeText(
        buildMCPClientConfiguration(agentName, mcpUrl, token),
      )
      setCopyResult({
        status: 'success',
        message: t('components.mcpConnection.copied'),
      })
    } catch {
      setCopyResult({
        status: 'error',
        message: t('components.mcpConnection.copyFailed'),
      })
    }
  }

  return (
    <>
      <div>
        <Title id="agent-mcp-connection-heading" level={4} style={{ marginBlock: 0 }}>
          {t('components.mcpConnection.title')}
        </Title>
        <Text type="secondary">
          {t('components.mcpConnection.description')}
        </Text>
      </div>

      <Descriptions
        column={1}
        size="small"
        style={{ marginTop: 16 }}
      >
        <Descriptions.Item label={t('components.mcpConnection.serverUrl')}>
          <Text code copyable style={{ wordBreak: 'break-all' }}>
            {mcpUrl}
          </Text>
        </Descriptions.Item>
        <Descriptions.Item label={t('components.mcpConnection.token')}>
          {loading ? (
            <Skeleton.Input active size="small" />
          ) : token ? (
            <Text code copyable style={{ wordBreak: 'break-all' }}>
              {token}
            </Text>
          ) : (
            <Text type="secondary">{t('components.mcpConnection.noToken')}</Text>
          )}
        </Descriptions.Item>
        <Descriptions.Item label={t('components.mcpConnection.tokenStatus')}>
          <Tag
            color={
              tokenStatus === 'active'
                ? 'success'
                : tokenStatus === 'expired'
                  ? 'warning'
                  : 'default'
            }
          >
            {t(`components.mcpConnection.${tokenStatus ?? 'missing'}`)}
          </Tag>
        </Descriptions.Item>
        <Descriptions.Item label={t('components.mcpConnection.expires')}>
          {expiresAt ? formatDateTime(expiresAt, i18n.resolvedLanguage ?? i18n.language) : t('components.mcpConnection.noExpiration')}
        </Descriptions.Item>
        <Descriptions.Item label={t('components.mcpConnection.lastUsed')}>
          {lastUsedAt ? formatDateTime(lastUsedAt, i18n.resolvedLanguage ?? i18n.language) : t('components.mcpConnection.never')}
        </Descriptions.Item>
      </Descriptions>

      {error && (
        <Alert
          type="error"
          showIcon
          role="alert"
          message={error}
          style={{ marginTop: 12 }}
          action={
            <Button size="small" onClick={onRetry}>
              {t('common.retry')}
            </Button>
          }
        />
      )}

      <Space wrap style={{ marginTop: 16 }}>
        <Button
          type="primary"
          disabled={copyDisabled}
          onClick={() => void copyConfiguration()}
        >
          {t('components.mcpConnection.copyConfiguration')}
        </Button>
        <Button onClick={onRegenerate}>{t('components.mcpConnection.regenerate')}</Button>
        {tokenStatus === 'active' && (
          <Button danger onClick={onRevoke}>
            {t('components.mcpConnection.revoke')}
          </Button>
        )}
      </Space>

      {copyResult && (
        <Alert
          type={copyResult.status === 'success' ? 'success' : 'error'}
          showIcon
          role={copyResult.status === 'success' ? 'status' : 'alert'}
          message={copyResult.message}
          style={{ marginTop: 12 }}
        />
      )}
    </>
  )
}
