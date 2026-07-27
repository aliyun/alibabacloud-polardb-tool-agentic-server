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

import type { AgentTokenStatus } from '../../api/agents'
import { buildMCPClientConfiguration } from './mcpConnection'

const { Text, Title } = Typography

type CopyResult =
  | { status: 'success'; message: 'JSON configuration copied' }
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

function tokenStatusLabel(status: AgentTokenStatus | null): string {
  if (!status) return 'Missing'
  return `${status[0].toUpperCase()}${status.slice(1)}`
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
        message: 'JSON configuration copied',
      })
    } catch {
      setCopyResult({
        status: 'error',
        message:
          'Could not copy JSON configuration. Check browser clipboard permissions.',
      })
    }
  }

  return (
    <>
      <div>
        <Title id="agent-mcp-connection-heading" level={4} style={{ marginBlock: 0 }}>
          MCP connection
        </Title>
        <Text type="secondary">
          Connect an MCP client with this Agent identity and its authorized
          resources.
        </Text>
      </div>

      <Descriptions
        column={1}
        size="small"
        style={{ marginTop: 16 }}
      >
        <Descriptions.Item label="MCP server URL">
          <Text code copyable style={{ wordBreak: 'break-all' }}>
            {mcpUrl}
          </Text>
        </Descriptions.Item>
        <Descriptions.Item label="Agent Token">
          {loading ? (
            <Skeleton.Input active size="small" />
          ) : token ? (
            <Text code copyable style={{ wordBreak: 'break-all' }}>
              {token}
            </Text>
          ) : (
            <Text type="secondary">No active Token</Text>
          )}
        </Descriptions.Item>
        <Descriptions.Item label="Token status">
          <Tag
            color={
              tokenStatus === 'active'
                ? 'success'
                : tokenStatus === 'expired'
                  ? 'warning'
                  : 'default'
            }
          >
            {tokenStatusLabel(tokenStatus)}
          </Tag>
        </Descriptions.Item>
        <Descriptions.Item label="Expires">
          {expiresAt ? new Date(expiresAt).toLocaleString() : 'No expiration'}
        </Descriptions.Item>
        <Descriptions.Item label="Last used">
          {lastUsedAt ? new Date(lastUsedAt).toLocaleString() : 'Never'}
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
              Retry
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
          Copy JSON configuration
        </Button>
        <Button onClick={onRegenerate}>Regenerate Token</Button>
        {tokenStatus === 'active' && (
          <Button danger onClick={onRevoke}>
            Revoke Token
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
