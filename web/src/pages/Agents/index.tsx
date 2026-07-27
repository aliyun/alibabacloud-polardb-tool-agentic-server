import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Descriptions,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Skeleton,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import { Link } from 'react-router-dom'

import {
  createAgent,
  listAgents,
  updateAgent,
  type Agent,
  type AgentCreated,
} from '../../api/agents'
import { getAPIErrorMessage } from '../../api/client'
import PageContainer from '../../components/PageContainer'

const { Text, Title } = Typography

interface CreateAgentForm {
  name: string
  description?: string
  max_active_resources?: number
}

export default function Agents() {
  const [agents, setAgents] = useState<Agent[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [createLoading, setCreateLoading] = useState(false)
  const [freshAgent, setFreshAgent] = useState<AgentCreated | null>(null)
  const [statusTarget, setStatusTarget] = useState<Agent | null>(null)
  const [statusLoading, setStatusLoading] = useState(false)
  const [reconnectNotice, setReconnectNotice] = useState(false)
  const [form] = Form.useForm<CreateAgentForm>()

  const loadAgents = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const response = await listAgents()
      setAgents(response.data)
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, 'Could not load Agents.'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadAgents()
    return () => setFreshAgent(null)
  }, [loadAgents])

  const handleCreate = async (values: CreateAgentForm) => {
    setCreateLoading(true)
    setError(null)
    try {
      const response = await createAgent({
        name: values.name.trim(),
        description: values.description?.trim() || null,
        max_active_resources: values.max_active_resources ?? null,
      })
      setFreshAgent(response.data)
      setCreateOpen(false)
      form.resetFields()
      await loadAgents()
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, 'Could not create Agent.'))
    } finally {
      setCreateLoading(false)
    }
  }

  const handleStatusChange = async () => {
    if (!statusTarget) return
    const nextStatus =
      statusTarget.status === 'active' ? 'disabled' : 'active'
    setStatusLoading(true)
    setError(null)
    try {
      const response = await updateAgent(statusTarget.id, {
        status: nextStatus,
      })
      setAgents((current) =>
        current.map((item) =>
          item.id === response.data.id ? response.data : item,
        ),
      )
      setStatusTarget(null)
      setReconnectNotice(true)
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, 'Could not change Agent status.'),
      )
    } finally {
      setStatusLoading(false)
    }
  }

  return (
    <PageContainer
      title="Agents"
      description="Issue dedicated MCP identities and manage their operational status."
      actions={
        <Button type="primary" onClick={() => setCreateOpen(true)}>
          Create Agent
        </Button>
      }
    >
      <Space direction="vertical" size={20} style={{ width: '100%' }}>
        {error && (
          <Alert
            type="error"
            showIcon
            role="alert"
            message={error}
            action={
              <Button size="small" onClick={() => void loadAgents()}>
                Retry
              </Button>
            }
          />
        )}
        {reconnectNotice && (
          <Alert
            type="info"
            showIcon
            closable
            role="status"
            message="Reconnect the MCP client"
            description="Existing MCP sessions may keep an older tool list. Reconnect after status or access changes."
            onClose={() => setReconnectNotice(false)}
          />
        )}
        {createOpen && (
          <section
            aria-labelledby="create-agent-heading"
            style={{
              background: 'var(--surface-tertiary)',
              borderRadius: 'var(--radius-md)',
              padding: 16,
            }}
          >
            <Title id="create-agent-heading" level={4} style={{ marginTop: 0 }}>
              Create Agent
            </Title>
            <Text type="secondary">
              Create one identity for one Agent. Its initial Token is shown
              after the identity is saved.
            </Text>
            <Form
              form={form}
              layout="vertical"
              onFinish={(values) => void handleCreate(values)}
              style={{ maxWidth: 640, marginTop: 16 }}
            >
              <Form.Item
                name="name"
                label="Name"
                rules={[
                  {
                    required: true,
                    whitespace: true,
                    message: 'Enter an Agent name.',
                  },
                  { max: 255 },
                ]}
              >
                <Input autoComplete="off" />
              </Form.Item>
              <Form.Item
                name="description"
                label="Description"
                rules={[{ max: 4096 }]}
              >
                <Input.TextArea rows={3} />
              </Form.Item>
              <Form.Item
                name="max_active_resources"
                label="Maximum active resources"
                extra="Leave blank to use the system limit."
                rules={[{ type: 'number', min: 1 }]}
              >
                <InputNumber min={1} precision={0} style={{ width: '100%' }} />
              </Form.Item>
              <Space>
                <Button
                  type="primary"
                  htmlType="submit"
                  loading={createLoading}
                >
                  Save Agent
                </Button>
                <Button
                  disabled={createLoading}
                  onClick={() => {
                    setCreateOpen(false)
                    form.resetFields()
                  }}
                >
                  Cancel
                </Button>
              </Space>
            </Form>
          </section>
        )}

        {loading ? (
          <Skeleton active paragraph={{ rows: 5 }} />
        ) : agents.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              <Space direction="vertical" size={4}>
                <Text strong>No Agents yet</Text>
                <Text type="secondary">
                  Create an Agent to issue a dedicated MCP identity and grant
                  narrowly scoped database access.
                </Text>
              </Space>
            }
          >
            <Button type="primary" onClick={() => setCreateOpen(true)}>
              Create Agent
            </Button>
          </Empty>
        ) : (
          <Table
            rowKey="id"
            dataSource={agents}
            pagination={false}
            scroll={{ x: 760 }}
            columns={[
              {
                title: 'Agent',
                dataIndex: 'name',
                render: (name: string, record: Agent) => (
                  <Space direction="vertical" size={0}>
                    <Link to={`/agents/${record.id}`}>{name}</Link>
                    {record.description && (
                      <Text type="secondary">{record.description}</Text>
                    )}
                  </Space>
                ),
              },
              {
                title: 'Status',
                dataIndex: 'status',
                width: 120,
                render: (status: Agent['status']) => (
                  <Tag color={status === 'active' ? 'success' : 'default'}>
                    {status === 'active' ? 'Active' : 'Disabled'}
                  </Tag>
                ),
              },
              {
                title: 'Resource limit',
                dataIndex: 'max_active_resources',
                width: 150,
                render: (value: number | null) => value ?? 'System default',
              },
              {
                title: 'Created',
                dataIndex: 'created_at',
                width: 180,
                render: (value: string) => new Date(value).toLocaleString(),
              },
              {
                title: 'Actions',
                key: 'actions',
                width: 130,
                render: (_: unknown, record: Agent) => (
                  <Button
                    size="small"
                    danger={record.status === 'active'}
                    aria-label={`${record.status === 'active' ? 'Disable' : 'Enable'} ${record.name}`}
                    onClick={() => setStatusTarget(record)}
                  >
                    {record.status === 'active' ? 'Disable' : 'Enable'}
                  </Button>
                ),
              },
            ]}
          />
        )}
      </Space>

      <Modal
        title={`${statusTarget?.status === 'active' ? 'Disable' : 'Enable'} Agent?`}
        open={statusTarget !== null}
        okText={`Confirm ${statusTarget?.status === 'active' ? 'disable' : 'enable'}`}
        okButtonProps={{ danger: statusTarget?.status === 'active' }}
        confirmLoading={statusLoading}
        onOk={() => void handleStatusChange()}
        onCancel={() => {
          if (!statusLoading) setStatusTarget(null)
        }}
        destroyOnHidden
      >
        <Space direction="vertical" size={12}>
          <Text>
            {statusTarget?.status === 'active'
              ? 'The Agent will no longer authenticate or start new operations.'
              : 'The Agent can authenticate again with its active Token.'}
          </Text>
          <Alert
            type="warning"
            showIcon
            message="Existing MCP sessions may keep an older tool list. Reconnect the client after this change."
          />
        </Space>
      </Modal>

      <Modal
        title="Agent created"
        open={freshAgent !== null}
        closable={false}
        maskClosable={false}
        footer={
          <Button
            type="primary"
            aria-label="Close token"
            onClick={() => setFreshAgent(null)}
          >
            Close
          </Button>
        }
        destroyOnHidden
      >
        {freshAgent && (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Alert
              type="warning"
              showIcon
              message="Store this Token in the intended MCP client."
              description="It is kept only in this page's memory and is cleared when this dialog closes."
            />
            <Title level={5}>MCP Token</Title>
            <Descriptions column={1} size="small">
              <Descriptions.Item label="Token">
                <Text code copyable style={{ wordBreak: 'break-all' }}>
                  {freshAgent.token}
                </Text>
              </Descriptions.Item>
            </Descriptions>
          </Space>
        )}
      </Modal>
    </PageContainer>
  )
}
