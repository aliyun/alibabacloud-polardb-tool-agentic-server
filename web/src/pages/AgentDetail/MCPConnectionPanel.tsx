import { useEffect, useState } from 'react'
import { Alert, Button, Descriptions, Space, Tag, Typography } from 'antd'
import { useTranslation } from 'react-i18next'

import type { AgentTokenStatus } from '../../api/agents'
import { buildMCPClientConfiguration } from './mcpConnection'
import { formatDateTime } from '../../i18n/format'
import { copyText } from '../../utils/clipboard'

const { Text, Title } = Typography

type CopyKind = 'token' | 'configuration'
type CopyResult = { status: 'success' | 'error'; message: string } | null

export interface MCPConnectionPanelProps {
  agentName: string
  headingId?: string
  mcpUrl: string
  tokenPrefix: string | null
  tokenStatus: AgentTokenStatus | null
  expiresAt: string | null
  lastUsedAt: string | null
  revealToken: () => Promise<string>
  onRegenerate: () => void
  onRevoke: () => void
}

function maskedToken(prefix: string | null): string | null {
  if (!prefix) return null
  return `${prefix.startsWith('pas_user_agent_') ? 'pas_user_agent_' : 'pas_agent_'}••••••••`
}

export default function MCPConnectionPanel({
  agentName,
  headingId = 'agent-mcp-connection-heading',
  mcpUrl,
  tokenPrefix,
  tokenStatus,
  expiresAt,
  lastUsedAt,
  revealToken,
  onRegenerate,
  onRevoke,
}: MCPConnectionPanelProps) {
  const { t, i18n } = useTranslation()
  const [copying, setCopying] = useState<CopyKind | null>(null)
  const [copyResult, setCopyResult] = useState<CopyResult>(null)
  const copyDisabled = tokenStatus !== 'active' || tokenPrefix === null
  const masked = maskedToken(tokenPrefix)

  useEffect(() => {
    setCopying(null)
    setCopyResult(null)
  }, [agentName, mcpUrl, tokenPrefix])

  const copy = async (copyKind: CopyKind) => {
    if (copying !== null || copyDisabled) return
    setCopying(copyKind)
    setCopyResult(null)
    try {
      const token = await revealToken()
      const content =
        copyKind === 'token'
          ? token
          : buildMCPClientConfiguration(agentName, mcpUrl, token)
      await copyText(content)
      setCopyResult({
        status: 'success',
        message:
          copyKind === 'token'
            ? t('components.mcpConnection.tokenCopied')
            : t('components.mcpConnection.configurationCopied'),
      })
    } catch {
      setCopyResult({
        status: 'error',
        message: t('components.mcpConnection.copyFailed'),
      })
    } finally {
      setCopying(null)
    }
  }

  return (
    <>
      <div>
        <Title id={headingId} level={4} style={{ marginBlock: 0 }}>
          {t('components.mcpConnection.title')}
        </Title>
        <Text type="secondary">
          {t('components.mcpConnection.description')}
        </Text>
      </div>

      <Descriptions column={1} size="small" style={{ marginTop: 16 }}>
        <Descriptions.Item label={t('components.mcpConnection.serverUrl')}>
          <Text code copyable style={{ wordBreak: 'break-all' }}>
            {mcpUrl}
          </Text>
        </Descriptions.Item>
        <Descriptions.Item label={t('components.mcpConnection.token')}>
          {masked ? (
            <Text code>{masked}</Text>
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

      <Space wrap style={{ marginTop: 16 }}>
        <Button
          disabled={copyDisabled || copying !== null}
          loading={copying === 'token'}
          onClick={() => void copy('token')}
        >
          {t('components.mcpConnection.copyToken')}
        </Button>
        <Button
          type="primary"
          disabled={copyDisabled || copying !== null}
          loading={copying === 'configuration'}
          onClick={() => void copy('configuration')}
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
