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
import { useTranslation } from 'react-i18next'

import {
  createAgent,
  listAgents,
  updateAgent,
  type Agent,
  type AgentCreated,
} from '../../api/agents'
import { getAPIErrorMessage } from '../../api/client'
import PageContainer from '../../components/PageContainer'
import { formatDateTime } from '../../i18n/format'

const { Text, Title } = Typography

interface CreateAgentForm {
  name: string
  description?: string
  max_active_resources?: number
}

export default function Agents() {
  const { t, i18n } = useTranslation()
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
      setError(getAPIErrorMessage(requestError, t('agents.loadFailed')))
    } finally {
      setLoading(false)
    }
  }, [t])

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
      setError(getAPIErrorMessage(requestError, t('agents.createFailed')))
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
        getAPIErrorMessage(requestError, t('agents.statusFailed')),
      )
    } finally {
      setStatusLoading(false)
    }
  }

  return (
    <PageContainer
      title={t('agents.title')}
      description={t('agents.description')}
      actions={
        <Button type="primary" onClick={() => setCreateOpen(true)}>
          {t('agents.create')}
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
                {t('common.retry')}
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
            message={t('agents.reconnect')}
            description={t('agents.reconnectDescription')}
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
              {t('agents.create')}
            </Title>
            <Text type="secondary">
              {t('agents.createDescription')}
            </Text>
            <Form
              form={form}
              layout="vertical"
              onFinish={(values) => void handleCreate(values)}
              style={{ maxWidth: 640, marginTop: 16 }}
            >
              <Form.Item
                name="name"
                label={t('agents.name')}
                rules={[
                  {
                    required: true,
                    whitespace: true,
                    message: t('agents.nameRequired'),
                  },
                  { max: 255 },
                ]}
              >
                <Input autoComplete="off" />
              </Form.Item>
              <Form.Item
                name="description"
                label={t('agents.descriptionLabel')}
                rules={[{ max: 4096 }]}
              >
                <Input.TextArea rows={3} />
              </Form.Item>
              <Form.Item
                name="max_active_resources"
                label={t('agents.maxResources')}
                extra={t('agents.systemLimitHint')}
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
                  {t('agents.save')}
                </Button>
                <Button
                  disabled={createLoading}
                  onClick={() => {
                    setCreateOpen(false)
                    form.resetFields()
                  }}
                >
                  {t('common.cancel')}
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
                <Text strong>{t('agents.empty')}</Text>
                <Text type="secondary">
                  {t('agents.emptyDescription')}
                </Text>
              </Space>
            }
          >
            <Button type="primary" onClick={() => setCreateOpen(true)}>
              {t('agents.create')}
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
                title: t('agents.agent'),
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
                title: t('agents.status'),
                dataIndex: 'status',
                width: 120,
                render: (status: Agent['status']) => (
                  <Tag color={status === 'active' ? 'success' : 'default'}>
                    {status === 'active' ? t('agents.active') : t('agents.disabled')}
                  </Tag>
                ),
              },
              {
                title: t('agents.resourceLimit'),
                dataIndex: 'max_active_resources',
                width: 150,
                render: (value: number | null) => value ?? t('agents.systemDefault'),
              },
              {
                title: t('agents.created'),
                dataIndex: 'created_at',
                width: 180,
                render: (value: string) => formatDateTime(value, i18n.resolvedLanguage ?? i18n.language),
              },
              {
                title: t('agents.actions'),
                key: 'actions',
                width: 130,
                render: (_: unknown, record: Agent) => (
                  <Button
                    size="small"
                    danger={record.status === 'active'}
                    aria-label={`${record.status === 'active' ? t('agents.disable') : t('agents.enable')} ${record.name}`}
                    onClick={() => setStatusTarget(record)}
                  >
                    {record.status === 'active' ? t('agents.disable') : t('agents.enable')}
                  </Button>
                ),
              },
            ]}
          />
        )}
      </Space>

      <Modal
        title={t('agents.statusTitle', { action: statusTarget?.status === 'active' ? t('agents.disable') : t('agents.enable') })}
        open={statusTarget !== null}
        okText={t('agents.confirmStatus', { action: statusTarget?.status === 'active' ? t('agents.disable').toLowerCase() : t('agents.enable').toLowerCase() })}
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
              ? t('agents.disableDescription')
              : t('agents.enableDescription')}
          </Text>
          <Alert
            type="warning"
            showIcon
            message={t('agents.sessionWarning')}
          />
        </Space>
      </Modal>

      <Modal
        title={t('agents.createdTitle')}
        open={freshAgent !== null}
        closable={false}
        maskClosable={false}
        footer={
          <Button
            type="primary"
            aria-label={t('agents.closeToken')}
            onClick={() => setFreshAgent(null)}
          >
            {t('agents.close')}
          </Button>
        }
        destroyOnHidden
      >
        {freshAgent && (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Alert
              type="warning"
              showIcon
              message={t('agents.storeToken')}
              description={t('agents.tokenMemory')}
            />
            <Title level={5}>{t('agents.mcpToken')}</Title>
            <Descriptions column={1} size="small">
              <Descriptions.Item label={t('agents.token')}>
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
