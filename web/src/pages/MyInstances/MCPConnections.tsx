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
type TokenAction = 'issue' | 'regenerate'
type CopyKind = 'token' | 'configuration'

interface MCPConnectionsProps {
  onSelectKnowledgeBases?: (connection: MyAgentConnection) => void
}

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

export default function MCPConnections({
  onSelectKnowledgeBases,
}: MCPConnectionsProps) {
  const { t } = useTranslation()
  const [connections, setConnections] = useState<MyAgentConnection[]>([])
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [password, setPassword] = useState('')
  const [expiresAt, setExpiresAt] = useState('')
  const [copyRequest, setCopyRequest] = useState<{
    kind: CopyKind
    connection: MyAgentConnection
  } | null>(null)
  const [confirmation, setConfirmation] = useState<{
    action: ConfirmedAction
    connection: MyAgentConnection
  } | null>(null)
  const [tokenAction, setTokenAction] = useState<{
    action: TokenAction
    connection: MyAgentConnection
  } | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setConnections((await listMyAgentConnections()).data)
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, t('mcpConnections.loadFailed')),
      )
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => {
    void load()
  }, [load])

  const operate = async (
    connection: MyAgentConnection,
    action: 'issue' | ConfirmedAction,
    expiration?: string,
  ) => {
    setBusyId(connection.agent_id)
    setError(null)
    try {
      let oneTimeToken: string | null = null
      if (action === 'issue') {
        oneTimeToken = (
          await issueMyAgentToken(connection.agent_id, expiration)
        ).data.token
      } else if (action === 'regenerate') {
        oneTimeToken = (
          await regenerateMyAgentToken(connection.agent_id, expiration)
        ).data.token
      } else {
        await revokeMyAgentToken(connection.agent_id)
      }
      if (oneTimeToken) {
        try {
          await copyText(oneTimeToken)
          setNotice(t('mcpConnections.oneTimeTokenCopied'))
        } catch {
          setError(t('mcpConnections.oneTimeClipboardBlocked'))
        }
      }
      await load()
    } catch (requestError) {
      setError(
        getAPIErrorMessage(
          requestError,
          t('mcpConnections.operationFailed', { action }),
        ),
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
      if (!token) throw new Error(t('mcpConnections.tokenInactive'))
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
        setError(t('mcpConnections.clipboardBlocked'))
        return
      }
      setNotice(
        kind === 'token'
          ? t('mcpConnections.tokenCopied')
          : t('mcpConnections.configurationCopied'),
      )
    } catch (requestError) {
      setError(
        getAPIErrorMessage(
          requestError,
          t('mcpConnections.verificationFailed'),
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
          locale={{ emptyText: t('mcpConnections.empty') }}
          columns={[
            { title: t('mcpConnections.agent'), dataIndex: 'agent_name' },
            {
              title: t('mcpConnections.polarragInstances'),
              render: (_value, row: MyAgentConnection) =>
                row.polarrag_instances.length > 0
                  ? row.polarrag_instances.map((instance) => (
                      <Tag key={instance.id}>{instance.name}</Tag>
                    ))
                  : t('mcpConnections.noneBound'),
            },
            {
              title: t('mcpConnections.agentStatus'),
              dataIndex: 'agent_status',
              render: (value: string) => <Tag>{value}</Tag>,
            },
            {
              title: t('mcpConnections.token'),
              render: (_value, row: MyAgentConnection) =>
                row.token ? (
                  <Space direction="vertical" size={0}>
                    <Space>
                      <Text code>{t('mcpConnections.maskedToken')}</Text>
                      <Tag>{row.token.status}</Tag>
                    </Space>
                    <Text type="secondary">
                      {row.token.expires_at
                        ? `${t('mcpConnections.expires')}: ${new Date(
                            row.token.expires_at,
                          ).toLocaleString()}`
                        : t('mcpConnections.neverExpires')}
                    </Text>
                  </Space>
                ) : (
                  t('mcpConnections.notIssued')
                ),
            },
            {
              title: t('mcpConnections.actions'),
              render: (_value, row: MyAgentConnection) => {
                const active = row.token?.status === 'active'
                const usable =
                  row.agent_status === 'active' &&
                  row.polarrag_instances.length > 0
                return (
                  <Space wrap>
                    <Button
                      size="small"
                      aria-label={t('mcpConnections.knowledgeBasesFor', {
                        agent: row.agent_name,
                      })}
                      disabled={!usable}
                      onClick={() => onSelectKnowledgeBases?.(row)}
                    >
                      {t('mcpConnections.knowledgeBases')}
                    </Button>
                    {!active && (
                      <Button
                        type="primary"
                        size="small"
                        disabled={!usable}
                        loading={busyId === row.agent_id}
                        onClick={() => {
                          setExpiresAt('')
                          setTokenAction({ action: 'issue', connection: row })
                        }}
                      >
                        {t('mcpConnections.issueToken')}
                      </Button>
                    )}
                    {active && (
                      <>
                        {row.password_reveal_available ? (
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
                          </>
                        ) : (
                          <Text type="secondary">
                            {t('mcpConnections.ssoOneTimeOnly')}
                          </Text>
                        )}
                        <Button
                          size="small"
                          disabled={!usable}
                          onClick={() => {
                            setExpiresAt('')
                            setTokenAction({
                              action: 'regenerate',
                              connection: row,
                            })
                          }}
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
            ? t('mcpConnections.copyTokenTitle')
            : t('mcpConnections.copyConfigurationTitle')
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
          tokenAction?.action === 'regenerate'
            ? t('mcpConnections.regenerateTitle')
            : t('mcpConnections.issueTitle')
        }
        open={tokenAction !== null}
        okText={
          tokenAction?.action === 'regenerate'
            ? t('mcpConnections.regenerate')
            : t('mcpConnections.issue')
        }
        confirmLoading={
          tokenAction !== null &&
          busyId === tokenAction.connection.agent_id
        }
        onCancel={() => {
          setTokenAction(null)
          setExpiresAt('')
        }}
        onOk={async () => {
          if (!tokenAction) return
          const current = tokenAction
          const expiration = expiresAt
            ? new Date(expiresAt).toISOString()
            : undefined
          await operate(current.connection, current.action, expiration)
          setTokenAction(null)
          setExpiresAt('')
        }}
        destroyOnHidden
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Text type="secondary">
            {tokenAction?.action === 'regenerate'
              ? t('mcpConnections.regenerateWarning')
              : t('mcpConnections.expirationHint')}
          </Text>
          <Input
            aria-label={t('mcpConnections.expiration')}
            type="datetime-local"
            value={expiresAt}
            onChange={(event) => setExpiresAt(event.target.value)}
          />
        </Space>
      </Modal>

      <Modal
        title={
          confirmation?.action === 'regenerate'
            ? t('mcpConnections.regenerateTitle')
            : t('mcpConnections.revokeTitle')
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
          ? t('mcpConnections.regenerateWarning')
          : t('mcpConnections.revokeWarning')}
      </Modal>
    </section>
  )
}
