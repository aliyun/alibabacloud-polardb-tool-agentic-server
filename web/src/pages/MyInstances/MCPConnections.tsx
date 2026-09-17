import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Input,
  Modal,
  Radio,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import { useTranslation } from 'react-i18next'
import { useFeatures } from '../../hooks/useFeatures'

import {
  getMyWorkspace,
  issueMyAgentToken,
  listMyAgentConnections,
  regenerateMyAgentToken,
  revealMyAgentToken,
  revokeMyAgentToken,
  selectMyDefaultAgent,
  type MyAgentConnection,
  type UserWorkspace,
} from '../../api/agentConnections'
import { getAPIErrorMessage } from '../../api/client'
import { buildMCPClientConfiguration } from '../AgentDetail/mcpConnection'
import { copyText } from '../../utils/clipboard'

const { Text, Title } = Typography

type ConfirmedAction = 'revoke'
type TokenAction = 'issue' | 'regenerate'
type CopyKind = 'token' | 'configuration'

interface MCPConnectionsProps {
  onSelectKnowledgeBases?: (connection: MyAgentConnection) => void
}

export default function MCPConnections({
  onSelectKnowledgeBases,
}: MCPConnectionsProps) {
  const { t } = useTranslation()
  const { knowledge } = useFeatures()
  const [connections, setConnections] = useState<MyAgentConnection[]>([])
  const [connectionTotal, setConnectionTotal] = useState(0)
  const [connectionPage, setConnectionPage] = useState(1)
  const [workspace, setWorkspace] = useState<UserWorkspace>()
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [expiresAt, setExpiresAt] = useState('')
  const [confirmation, setConfirmation] = useState<{
    action: ConfirmedAction
    connection: MyAgentConnection
  } | null>(null)
  const [tokenAction, setTokenAction] = useState<{
    action: TokenAction
    connection: MyAgentConnection
  } | null>(null)
  const [oneTimeToken, setOneTimeToken] = useState<{
    connection: MyAgentConnection
    token: string
  } | null>(null)

  const load = useCallback(async (page = connectionPage) => {
    setLoading(true)
    setError(null)
    try {
      const [connectionResponse, workspaceResponse] = await Promise.all([
        listMyAgentConnections({ offset: (page - 1) * 20, limit: 20 }),
        knowledge ? getMyWorkspace() : Promise.resolve({ data: undefined }),
      ])
      setConnections(connectionResponse.data.items)
      setConnectionTotal(connectionResponse.data.total)
      setConnectionPage(page)
      setWorkspace(workspaceResponse.data)
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, t('mcpConnections.loadFailed')),
      )
    } finally {
      setLoading(false)
    }
  }, [connectionPage, t, knowledge])

  const selectDefaultAgent = async (agentId: string) => {
    setBusyId(agentId)
    setError(null)
    try {
      setWorkspace((await selectMyDefaultAgent(agentId)).data)
      setNotice(t('mcpConnections.defaultAgentUpdated'))
    } catch (requestError) {
      setError(getAPIErrorMessage(
        requestError,
        t('mcpConnections.defaultAgentUpdateFailed'),
      ))
    } finally {
      setBusyId(null)
    }
  }

  useEffect(() => {
    void load()
  }, [load])

  const operate = async (
    connection: MyAgentConnection,
    action: TokenAction | ConfirmedAction,
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
        setOneTimeToken({ connection, token: oneTimeToken })
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

  const copy = async (connection: MyAgentConnection, kind: CopyKind) => {
    setBusyId(connection.agent_id)
    setError(null)
    setNotice(null)
    try {
      const response = await revealMyAgentToken(connection.agent_id)
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
    }
  }

  const copyOneTimeToken = async (kind: CopyKind) => {
    if (!oneTimeToken) return
    try {
      await copyText(
        kind === 'token'
          ? oneTimeToken.token
          : buildMCPClientConfiguration(
              oneTimeToken.connection.agent_name,
              `${window.location.origin.replace(/\/+$/, '')}/mcp`,
              oneTimeToken.token,
            ),
      )
      setNotice(
        kind === 'token'
          ? t('mcpConnections.tokenCopied')
          : t('mcpConnections.configurationCopied'),
      )
    } catch {
      setError(t('mcpConnections.clipboardBlocked'))
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
        {workspace && workspace.status !== 'ready' && (
          <Alert
            type={workspace.status === 'selection_required' ? 'warning' : 'error'}
            showIcon
            message={t(`mcpConnections.workspaceStates.${workspace.status}`)}
          />
        )}
        <Table
          rowKey="agent_id"
          loading={loading}
          dataSource={connections}
          pagination={{
            current: connectionPage,
            pageSize: 20,
            total: connectionTotal,
            showSizeChanger: false,
            onChange: (page) => void load(page),
          }}
          locale={{ emptyText: t('mcpConnections.empty') }}
          columns={[
            {
              title: t('mcpConnections.defaultAgent'),
              hidden: !knowledge,
              width: 96,
              align: 'center',
              render: (_value, row: MyAgentConnection) => {
                const available = workspace?.available_agents?.some(
                  (agent) => agent.id === row.agent_id,
                ) ?? false
                return (
                  <Radio
                    checked={workspace?.default_agent?.id === row.agent_id}
                    disabled={!available || busyId !== null}
                    aria-label={t('mcpConnections.selectDefaultAgent', {
                      agent: row.agent_name,
                    })}
                    onChange={() => void selectDefaultAgent(row.agent_id)}
                  />
                )
              },
            },
            { title: t('mcpConnections.agent'), dataIndex: 'agent_name' },
            {
              title: t('mcpConnections.polarragInstances'),
              hidden: !knowledge,
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
                const usable = row.agent_status === 'active'
                return (
                  <Space wrap>
                    {knowledge && <Button
                      size="small"
                      aria-label={t('mcpConnections.knowledgeBasesFor', {
                        agent: row.agent_name,
                      })}
                      disabled={!usable || row.polarrag_instances.length === 0}
                      onClick={() => onSelectKnowledgeBases?.(row)}
                    >
                      {t('mcpConnections.knowledgeBases')}
                    </Button>}
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
                        <Button
                          size="small"
                          disabled={
                            row.agent_status !== 'active' || busyId !== null
                          }
                          loading={busyId === row.agent_id}
                          onClick={() => void copy(row, 'token')}
                        >
                          {t('mcpConnections.copyToken')}
                        </Button>
                        <Button
                          size="small"
                          disabled={
                            row.agent_status !== 'active' || busyId !== null
                          }
                          loading={busyId === row.agent_id}
                          onClick={() => void copy(row, 'configuration')}
                        >
                          {t('mcpConnections.copyConfiguration')}
                        </Button>
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
          tokenAction?.action === 'regenerate'
            ? t('mcpConnections.regenerateTitle')
            : t('mcpConnections.issueTitle')
        }
        open={tokenAction !== null}
        okText={
          tokenAction?.action === 'regenerate'
            ? t('mcpConnections.confirmRegenerate')
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
          <Text
            type={
              tokenAction?.action === 'regenerate' ? 'danger' : 'secondary'
            }
          >
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
        title={t('mcpConnections.oneTimeCopyTitle')}
        open={oneTimeToken !== null}
        footer={(
          <Space>
            <Button onClick={() => void copyOneTimeToken('token')}>
              {t('mcpConnections.copyToken')}
            </Button>
            <Button
              type="primary"
              onClick={() => void copyOneTimeToken('configuration')}
            >
              {t('mcpConnections.copyConfiguration')}
            </Button>
            <Button onClick={() => setOneTimeToken(null)}>
              {t('mcpConnections.close')}
            </Button>
          </Space>
        )}
        onCancel={() => setOneTimeToken(null)}
        destroyOnHidden
      >
        <Text type="secondary">
          {t('mcpConnections.oneTimeCopyPrompt')}
        </Text>
      </Modal>

      <Modal
        title={
          t('mcpConnections.revokeTitle')
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
        {t('mcpConnections.revokeWarning')}
      </Modal>
    </section>
  )
}
