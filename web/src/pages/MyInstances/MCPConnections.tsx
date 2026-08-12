import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Input,
  Modal,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import { useTranslation } from 'react-i18next'

import {
  issueMyAgentToken,
  listMyAgentConnections,
  regenerateMyAgentToken,
  revealMyAgentToken,
  revokeMyAgentToken,
  type MyAgentConnection,
} from '../../api/agentConnections'
import { getAPIErrorMessage } from '../../api/client'
import { buildMCPClientConfiguration } from '../AgentDetail/mcpConnection'

const { Text, Title } = Typography

type ConfirmedAction = 'regenerate' | 'revoke'
type CopyKind = 'token' | 'configuration'
const CLIPBOARD_ERROR =
  'Browser blocked clipboard access. Open PAS over HTTPS and try again.'

async function copyText(text: string) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return
    } catch {
      // Fall through for browsers that block Clipboard API on HTTP.
    }
  }

  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.readOnly = true
  textarea.style.position = 'fixed'
  textarea.style.opacity = '0'
  textarea.style.pointerEvents = 'none'
  document.body.appendChild(textarea)
  textarea.select()
  try {
    if (!document.execCommand('copy')) throw new Error('Copy rejected')
  } finally {
    textarea.value = ''
    textarea.remove()
  }
}

export default function MCPConnections() {
  const { t } = useTranslation()
  const [connections, setConnections] = useState<MyAgentConnection[]>([])
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [password, setPassword] = useState('')
  const [copyRequest, setCopyRequest] = useState<{
    kind: CopyKind
    connection: MyAgentConnection
  } | null>(null)
  const [confirmation, setConfirmation] = useState<{
    action: ConfirmedAction
    connection: MyAgentConnection
  } | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setConnections((await listMyAgentConnections()).data)
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, 'Could not load MCP connections.'),
      )
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const operate = async (
    connection: MyAgentConnection,
    action: 'issue' | ConfirmedAction,
  ) => {
    setBusyId(connection.agent_id)
    setError(null)
    try {
      if (action === 'issue') {
        await issueMyAgentToken(connection.agent_id)
      } else if (action === 'regenerate') {
        await regenerateMyAgentToken(connection.agent_id)
      } else {
        await revokeMyAgentToken(connection.agent_id)
      }
      await load()
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, `Could not ${action} Agent Token.`),
      )
    } finally {
      setBusyId(null)
    }
  }

  const closeCopy = () => {
    setCopyRequest(null)
    setPassword('')
  }

  const copy = async () => {
    if (!copyRequest || !password) return
    const { connection, kind } = copyRequest
    setBusyId(connection.agent_id)
    setError(null)
    setNotice(null)
    try {
      const response = await revealMyAgentToken(connection.agent_id, password)
      const token = response.data.token
      if (!token) throw new Error('Agent Token is not active')
      try {
        await copyText(
          kind === 'token'
            ? token
            : buildMCPClientConfiguration(
                connection.agent_name,
                `${window.location.origin.replace(/\/+$/, '')}/mcp`,
                token,
              ),
        )
      } catch {
        setError(CLIPBOARD_ERROR)
        return
      }
      setNotice(
        kind === 'token'
          ? 'Agent Token copied.'
          : 'JSON configuration copied.',
      )
    } catch (requestError) {
      setError(
        getAPIErrorMessage(
          requestError,
          'Password verification failed or Token unavailable.',
        ),
      )
    } finally {
      setBusyId(null)
      closeCopy()
    }
  }

  return (
    <section aria-labelledby="mcp-connections-heading">
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <div>
          <Title id="mcp-connections-heading" level={4} style={{ margin: 0 }}>
            {t('mcpConnections.title')}
          </Title>
          <Text type="secondary">
            {t('mcpConnections.description')}
          </Text>
        </div>
        {error && <Alert type="error" showIcon role="alert" message={error} />}
        {notice && <Alert type="success" showIcon role="status" message={notice} />}
        <Table
          rowKey="agent_id"
          loading={loading}
          dataSource={connections}
          pagination={false}
          locale={{ emptyText: 'No Agents assigned to your account' }}
          columns={[
            { title: 'Agent', dataIndex: 'agent_name' },
            {
              title: 'PolarRAG instances',
              render: (_value, row: MyAgentConnection) =>
                row.polarrag_instances.length > 0
                  ? row.polarrag_instances.map((instance) => (
                      <Tag key={instance.id}>{instance.name}</Tag>
                    ))
                  : 'None bound',
            },
            {
              title: 'Agent status',
              dataIndex: 'agent_status',
              render: (value: string) => <Tag>{value}</Tag>,
            },
            {
              title: 'Token',
              render: (_value, row: MyAgentConnection) =>
                row.token ? (
                  <Space>
                    <Text code>{t('mcpConnections.maskedToken')}</Text>
                    <Tag>{row.token.status}</Tag>
                  </Space>
                ) : (
                  'Not issued'
                ),
            },
            {
              title: 'Actions',
              render: (_value, row: MyAgentConnection) => {
                const active = row.token?.status === 'active'
                const usable =
                  row.agent_status === 'active' &&
                  row.polarrag_instances.length > 0
                return (
                  <Space wrap>
                    {!active && (
                      <Button
                        type="primary"
                        size="small"
                        disabled={!usable}
                        loading={busyId === row.agent_id}
                        onClick={() => void operate(row, 'issue')}
                      >
                        {t('mcpConnections.issueToken')}
                      </Button>
                    )}
                    {active && (
                      <>
                        <Button
                          size="small"
                          disabled={row.agent_status !== 'active'}
                          onClick={() =>
                            setCopyRequest({ kind: 'token', connection: row })
                          }
                        >
                          {t('mcpConnections.copyToken')}
                        </Button>
                        <Button
                          size="small"
                          disabled={row.agent_status !== 'active'}
                          onClick={() =>
                            setCopyRequest({
                              kind: 'configuration',
                              connection: row,
                            })
                          }
                        >
                          {t('mcpConnections.copyConfiguration')}
                        </Button>
                        <Button
                          size="small"
                          disabled={!usable}
                          onClick={() =>
                            setConfirmation({
                              action: 'regenerate',
                              connection: row,
                            })
                          }
                        >
                          {t('mcpConnections.regenerate')}
                        </Button>
                        <Button
                          danger
                          size="small"
                          onClick={() =>
                            setConfirmation({ action: 'revoke', connection: row })
                          }
                        >
                          {t('mcpConnections.revoke')}
                        </Button>
                      </>
                    )}
                  </Space>
                )
              },
            },
          ]}
        />
      </Space>

      <Modal
        title={
          copyRequest?.kind === 'token'
            ? 'Copy user-specific Agent Token'
            : 'Copy MCP configuration'
        }
        open={copyRequest !== null}
        okText={t('mcpConnections.copy')}
        confirmLoading={
          copyRequest !== null && busyId === copyRequest.connection.agent_id
        }
        okButtonProps={{ disabled: password.length === 0 }}
        onCancel={closeCopy}
        onOk={() => void copy()}
        destroyOnHidden
      >
        <Text type="secondary">
          {t('mcpConnections.passwordPrompt')}
        </Text>
        <Input.Password
          aria-label={t('mcpConnections.currentPassword')}
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          onPressEnter={() => void copy()}
          style={{ marginTop: 16 }}
        />
      </Modal>

      <Modal
        title={
          confirmation?.action === 'regenerate'
            ? 'Regenerate Agent Token?'
            : 'Revoke Agent Token?'
        }
        open={confirmation !== null}
        okButtonProps={{ danger: true }}
        confirmLoading={
          confirmation !== null &&
          busyId === confirmation.connection.agent_id
        }
        onCancel={() => setConfirmation(null)}
        onOk={async () => {
          if (!confirmation) return
          const current = confirmation
          await operate(current.connection, current.action)
          setConfirmation(null)
        }}
        destroyOnHidden
      >
        {confirmation?.action === 'regenerate'
          ? 'The previous Token becomes invalid immediately.'
          : 'This Token stops authenticating immediately.'}
      </Modal>
    </section>
  )
}
