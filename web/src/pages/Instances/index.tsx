import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Col,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Skeleton,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import { Link } from 'react-router-dom'

import { getAPIErrorMessage } from '../../api/client'
import {
  listAdminInstances,
  registerAdminInstance,
  removeAdminInstance,
  testAdminInstanceConnection,
  type InstanceSummary,
  type RegisterInstanceInput,
} from '../../api/instances'
import PageContainer from '../../components/PageContainer'

const { Text } = Typography

type ConnectionTestResult =
  | { status: 'success' }
  | { status: 'error'; message: string }
  | null

function statusColor(status: string) {
  if (status === 'active') return 'green'
  if (status === 'failed' || status === 'stopped') return 'red'
  return 'orange'
}

function Provisioning({ instance }: { instance: InstanceSummary }) {
  if (!instance.health) {
    return <Tag>Not enabled</Tag>
  }
  return (
    <Space direction="vertical" size={0}>
      <Tag color={instance.health.healthy ? 'green' : 'red'}>
        {instance.health.healthy ? 'Healthy' : 'Unhealthy'}
      </Tag>
      {!instance.health.healthy && instance.health.error_code && (
        <Text type="secondary">{instance.health.error_code}</Text>
      )}
    </Space>
  )
}

export default function Instances() {
  const [instances, setInstances] = useState<InstanceSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [error, setError] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [createLoading, setCreateLoading] = useState(false)
  const [connectionTestLoading, setConnectionTestLoading] = useState(false)
  const [connectionTestResult, setConnectionTestResult] =
    useState<ConnectionTestResult>(null)
  const [removeTarget, setRemoveTarget] = useState<InstanceSummary | null>(null)
  const [removeLoading, setRemoveLoading] = useState(false)
  const [form] = Form.useForm<RegisterInstanceInput>()

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const response = await listAdminInstances((page - 1) * pageSize, pageSize)
      setInstances(response.data.items)
      setTotal(response.data.total)
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, 'Could not load instances.'))
    } finally {
      setLoading(false)
    }
  }, [page, pageSize])

  useEffect(() => {
    void load()
  }, [load])

  const handleRegister = async (values: RegisterInstanceInput) => {
    setCreateLoading(true)
    setError(null)
    try {
      await registerAdminInstance({
        cluster_id: values.cluster_id.trim(),
        name: values.name.trim(),
        usage: values.usage?.trim() || undefined,
        engine: values.engine,
        topology: values.topology,
        region: values.region?.trim() || undefined,
        host: values.host.trim(),
        port: values.port,
        username: values.username.trim(),
        password: values.password,
      })
      setCreateOpen(false)
      setConnectionTestResult(null)
      form.resetFields()
      await load()
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, 'Could not register this instance.'),
      )
    } finally {
      setCreateLoading(false)
    }
  }

  const handleTestConnection = async () => {
    setConnectionTestLoading(true)
    setConnectionTestResult(null)
    try {
      const values = await form.validateFields([
        'topology',
        'host',
        'port',
        'username',
        'password',
      ])
      await testAdminInstanceConnection({
        topology: values.topology,
        host: values.host.trim(),
        port: values.port,
        username: values.username.trim(),
        password: values.password,
      })
      setConnectionTestResult({ status: 'success' })
    } catch (requestError) {
      if (
        requestError &&
        typeof requestError === 'object' &&
        'errorFields' in requestError
      ) {
        return
      }
      setConnectionTestResult({
        status: 'error',
        message: getAPIErrorMessage(requestError, 'Connection test failed.'),
      })
    } finally {
      setConnectionTestLoading(false)
    }
  }

  const handleRemove = async () => {
    if (!removeTarget) return
    setRemoveLoading(true)
    setError(null)
    try {
      await removeAdminInstance(removeTarget.id)
      setRemoveTarget(null)
      await load()
    } catch (requestError) {
      setError(
        getAPIErrorMessage(
          requestError,
          'Could not remove the instance. Remove active bindings and the provisioning backend first.',
        ),
      )
    } finally {
      setRemoveLoading(false)
    }
  }

  return (
    <PageContainer
      title="Instances"
      description="Register physical database instances and review their access and provisioning state."
      actions={
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => {
            form.resetFields()
            setConnectionTestResult(null)
            setCreateOpen(true)
          }}
        >
          Register Instance
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
              <Button size="small" onClick={() => void load()}>
                Retry
              </Button>
            }
          />
        )}

        {loading ? (
          <Skeleton active paragraph={{ rows: 6 }} />
        ) : instances.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              <Space direction="vertical" size={4}>
                <Text strong>No physical instances registered</Text>
                <Text type="secondary">
                  Register and verify an instance before granting access.
                </Text>
              </Space>
            }
          >
            <Button
              type="primary"
              onClick={() => {
                form.resetFields()
                setConnectionTestResult(null)
                setCreateOpen(true)
              }}
            >
              Register Instance
            </Button>
          </Empty>
        ) : (
          <Table
            dataSource={instances}
            pagination={{
              current: page,
              pageSize,
              total,
              showSizeChanger: true,
              onChange: (nextPage, nextPageSize) => {
                setPage(nextPageSize === pageSize ? nextPage : 1)
                setPageSize(nextPageSize)
              },
            }}
            rowKey="id"
            scroll={{ x: 1380 }}
            columns={[
              {
                title: 'Instance',
                dataIndex: 'name',
                fixed: 'left',
                render: (name: string, record: InstanceSummary) => (
                  <Space direction="vertical" size={0}>
                    <Link to={`/instances/${record.id}`}>{name}</Link>
                    <Text type="secondary">{record.cluster_id}</Text>
                  </Space>
                ),
              },
              {
                title: 'Usage',
                dataIndex: 'usage',
                render: (value: string | null) =>
                  value || <Text type="secondary">Not specified</Text>,
              },
              {
                title: 'Engine',
                dataIndex: 'engine',
                render: (value: string) => <Tag>{value}</Tag>,
              },
              {
                title: 'Topology',
                dataIndex: 'topology',
                render: (value: string) => <Tag>{value}</Tag>,
              },
              {
                title: 'Allocation mode',
                dataIndex: 'allocation_mode',
                render: (value: string) => <Tag>{value}</Tag>,
              },
              {
                title: 'Status',
                dataIndex: 'status',
                render: (value: string) => (
                  <Tag color={statusColor(value)}>{value}</Tag>
                ),
              },
              {
                title: 'Provisioning',
                render: (_: unknown, record: InstanceSummary) => (
                  <Provisioning instance={record} />
                ),
              },
              {
                title: 'Bindings',
                render: (_: unknown, record: InstanceSummary) => (
                  <Space size={4} wrap>
                    <Tag>{record.binding_counts.users} users</Tag>
                    <Tag>
                      {record.binding_counts.departments}{' '}
                      {record.binding_counts.departments === 1
                        ? 'department'
                        : 'departments'}
                    </Tag>
                    <Tag>{record.binding_counts.agents} agents</Tag>
                  </Space>
                ),
              },
              {
                title: 'Actions',
                fixed: 'right',
                render: (_: unknown, record: InstanceSummary) => (
                  <Button
                    size="small"
                    danger
                    aria-label={`Remove ${record.name}`}
                    onClick={() => setRemoveTarget(record)}
                  >
                    Remove
                  </Button>
                ),
              },
            ]}
          />
        )}
      </Space>

      <Modal
        title="Register Instance"
        width={760}
        open={createOpen}
        okText="Save Instance"
        confirmLoading={createLoading}
        onOk={() => form.submit()}
        onCancel={() => {
          if (!createLoading) {
            setCreateOpen(false)
            setConnectionTestResult(null)
            form.resetFields()
          }
        }}
        destroyOnHidden
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={{
            engine: 'polardb_mysql',
            topology: 'single_tenant',
            port: 3306,
          }}
          onValuesChange={(changedValues) => {
            if (
              ['topology', 'host', 'port', 'username', 'password'].some(
                (field) => field in changedValues,
              )
            ) {
              setConnectionTestResult(null)
            }
          }}
          onFinish={(values) => void handleRegister(values)}
        >
          <Row
            gutter={[16, 0]}
            role="group"
            aria-label="Instance identity"
          >
            <Col xs={24} md={12}>
              <Form.Item
                name="cluster_id"
                label="Cluster ID"
                rules={[{ required: true, whitespace: true }, { max: 255 }]}
              >
                <Input placeholder="pc-xxx" autoComplete="off" />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item
                name="name"
                label="Name"
                rules={[{ required: true, whitespace: true }, { max: 255 }]}
              >
                <Input autoComplete="off" />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item
            name="usage"
            label="Usage"
            rules={[{ max: 1024 }]}
          >
            <Input.TextArea
              rows={2}
              showCount
              maxLength={1024}
              placeholder="Describe what this instance is used for"
            />
          </Form.Item>
          <Row
            gutter={[16, 0]}
            role="group"
            aria-label="Instance classification"
          >
            <Col xs={24} md={12}>
              <Form.Item
                name="engine"
                label="Engine"
                rules={[{ required: true }]}
              >
                <Select
                  options={[
                    { value: 'polardb_mysql', label: 'PolarDB for MySQL' },
                  ]}
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item
                name="topology"
                label="Topology"
                rules={[{ required: true }]}
              >
                <Select
                  options={[
                    { value: 'single_tenant', label: 'Single tenant' },
                    { value: 'multitenant', label: 'Multi-tenant' },
                  ]}
                />
              </Form.Item>
            </Col>
          </Row>
          <Row
            gutter={[16, 0]}
            role="group"
            aria-label="Instance location"
          >
            <Col xs={24} md={12}>
              <Form.Item name="region" label="Region" rules={[{ max: 64 }]}>
                <Input placeholder="cn-hangzhou" autoComplete="off" />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item
                name="port"
                label="Port"
                rules={[{ required: true }]}
              >
                <InputNumber
                  min={1}
                  max={65535}
                  precision={0}
                  style={{ width: '100%' }}
                />
              </Form.Item>
            </Col>
          </Row>
          <Row role="group" aria-label="Instance endpoint">
            <Col span={24}>
              <Form.Item
                name="host"
                label="Host"
                rules={[
                  { required: true, whitespace: true },
                  { max: 255 },
                ]}
              >
                <Input autoComplete="off" />
              </Form.Item>
            </Col>
          </Row>
          <Row
            gutter={[16, 0]}
            role="group"
            aria-label="Instance credentials"
          >
            <Col xs={24} md={12}>
              <Form.Item
                name="username"
                label="Username"
                rules={[
                  { required: true, whitespace: true },
                  { max: 255 },
                ]}
              >
                <Input autoComplete="username" />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item
                name="password"
                label="Password"
                rules={[{ required: true }, { max: 1024 }]}
              >
                <Input.Password autoComplete="new-password" />
              </Form.Item>
            </Col>
          </Row>
          <Alert
            type="info"
            showIcon
            message="MySQL permissions apply"
            description="MCP forwards SQL using this account. Database access and permissions are enforced by the MySQL backend; this service does not bypass or elevate those permissions."
          />
          <Button
            style={{ marginTop: 16 }}
            loading={connectionTestLoading}
            onClick={() => void handleTestConnection()}
          >
            Test Connection
          </Button>
          {connectionTestResult && (
            <Alert
              type={
                connectionTestResult.status === 'success' ? 'success' : 'error'
              }
              showIcon
              role={
                connectionTestResult.status === 'success' ? 'status' : 'alert'
              }
              message={
                connectionTestResult.status === 'success'
                  ? 'Connection succeeded'
                  : connectionTestResult.message
              }
              style={{ marginTop: 12 }}
            />
          )}
        </Form>
      </Modal>

      <Modal
        title={`Remove ${removeTarget?.name ?? 'instance'}?`}
        open={!!removeTarget}
        okText="Confirm Remove"
        okButtonProps={{ danger: true }}
        confirmLoading={removeLoading}
        onOk={() => void handleRemove()}
        onCancel={() => {
          if (!removeLoading) setRemoveTarget(null)
        }}
      >
        <Text>
          This removes the physical instance registration. It does not delete
          the PolarDB cluster. Remove bindings, credentials, and provisioning
          configuration first.
        </Text>
      </Modal>
    </PageContainer>
  )
}
