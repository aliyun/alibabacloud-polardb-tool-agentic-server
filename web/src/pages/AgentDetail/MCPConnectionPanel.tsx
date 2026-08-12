import { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Descriptions,
  Input,
  Modal,
  Space,
  Tag,
  Typography,
} from 'antd'
import { useTranslation } from 'react-i18next'

import type { AgentTokenStatus } from '../../api/agents'
import { buildMCPClientConfiguration } from './mcpConnection'
import { formatDateTime } from '../../i18n/format'

const { Text, Title } = Typography

type CopyKind = 'token' | 'configuration'
type CopyResult = { status: 'success' | 'error'; message: string } | null

export interface MCPConnectionPanelProps {
  agentName: string
  mcpUrl: string
  tokenPrefix: string | null
  tokenStatus: AgentTokenStatus | null
  expiresAt: string | null
  lastUsedAt: string | null
  revealToken: (password: string) => Promise<string>
  onRegenerate: () => void
  onRevoke: () => void
}

function maskedToken(prefix: string | null): string | null {
  if (!prefix) return null
  return `${prefix.startsWith('pas_user_agent_') ? 'pas_user_agent_' : 'pas_agent_'}••••••••`
}

export default function MCPConnectionPanel({
  agentName,
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
  const [copyKind, setCopyKind] = useState<CopyKind | null>(null)
  const [password, setPassword] = useState('')
  const [copying, setCopying] = useState(false)
  const [copyResult, setCopyResult] = useState<CopyResult>(null)
  const copyDisabled = tokenStatus !== 'active' || tokenPrefix === null
  const masked = maskedToken(tokenPrefix)

  useEffect(() => {
    setCopyKind(null)
    setPassword('')
    setCopyResult(null)
  }, [agentName, mcpUrl, tokenPrefix])

  const closeCopy = () => {
    setCopyKind(null)
    setPassword('')
  }

  const copy = async () => {
    if (!copyKind || !password || copyDisabled) return
    setCopying(true)
    setCopyResult(null)
    try {
      if (!navigator.clipboard) throw new Error('Clipboard unavailable')
      const token = await revealToken(password)
      const content =
        copyKind === 'token'
          ? token
          : buildMCPClientConfiguration(agentName, mcpUrl, token)
      await navigator.clipboard.writeText(content)
      setCopyResult({
        status: 'success',
        message:
          copyKind === 'token'
            ? 'Agent Token copied'
            : 'JSON configuration copied',
      })
    } catch {
      setCopyResult({
        status: 'error',
        message: 'Password verification failed or Token unavailable.',
      })
    } finally {
      setCopying(false)
      closeCopy()
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
          disabled={copyDisabled}
          onClick={() => setCopyKind('token')}
        >
          {t('components.mcpConnection.copyToken')}
        </Button>
        <Button
          type="primary"
          disabled={copyDisabled}
          onClick={() => setCopyKind('configuration')}
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

      <Modal
        title={copyKind === 'token'
          ? t('components.mcpConnection.copyTokenTitle')
          : t('components.mcpConnection.copyConfigurationTitle')}
        open={copyKind !== null}
        okText={t('components.mcpConnection.copy')}
        confirmLoading={copying}
        okButtonProps={{ disabled: password.length === 0 }}
        onCancel={closeCopy}
        onOk={() => void copy()}
        destroyOnHidden
      >
        <Text type="secondary">
          {t('components.mcpConnection.passwordPrompt')}
        </Text>
        <Input.Password
          aria-label={t('components.mcpConnection.currentPassword')}
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          onPressEnter={() => void copy()}
          style={{ marginTop: 16 }}
        />
      </Modal>
    </>
  )
}
