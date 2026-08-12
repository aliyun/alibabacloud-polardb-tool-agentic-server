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
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd'
import { useTranslation } from 'react-i18next'
import { CheckCircleOutlined, PlusOutlined } from '@ant-design/icons'
import { Link, useSearchParams } from 'react-router-dom'

import { getAPIErrorMessage } from '../../api/client'
import {
  listAdminInstances,
  registerAdminInstance,
  removeAdminInstance,
  testAdminInstanceConnection,
  type InstanceSummary,
  type RegisterInstanceInput,
} from '../../api/instances'
import {
  createPolarRAGInstance,
  type CreatePolarRAGInstanceInput,
} from '../../api/polarrag'
import PageContainer from '../../components/PageContainer'
import InstancesPanel from '../PolarRAG/InstancesPanel'

const { Text } = Typography

type RegisterFormInput = Omit<
  RegisterInstanceInput,
  'cluster_id' | 'engine' | 'topology'
> & {
  cluster_id?: string
  engine: 'polardb_mysql' | 'polarrag'
  topology?: RegisterInstanceInput['topology']
  scheme?: CreatePolarRAGInstanceInput['scheme']
  tls_verify?: boolean
  ca_bundle?: string | null
}

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
  const { t } = useTranslation()
  if (!instance.health) {
    return <Tag>{t('instances.provisioningDisabled')}</Tag>
  }
  return (
    <Space direction="vertical" size={0}>
      <Tag color={instance.health.healthy ? 'green' : 'red'}>
        {instance.health.healthy ? t('instances.healthy') : t('instances.unhealthy')}
      </Tag>
      {!instance.health.healthy && instance.health.error_code && (
        <Text type="secondary">{instance.health.error_code}</Text>
      )}
    </Space>
  )
}

export default function Instances() {
  const { t } = useTranslation()
  const [searchParams, setSearchParams] = useSearchParams()
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
  const [polarRAGRefreshKey, setPolarRAGRefreshKey] = useState(0)
  const [form] = Form.useForm<RegisterFormInput>()
  const selectedEngine = Form.useWatch('engine', form) ?? 'polardb_mysql'
  const activeTab =
    searchParams.get('type') === 'polarrag' ? 'polarrag' : 'database'

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const response = await listAdminInstances((page - 1) * pageSize, pageSize)
      setInstances(response.data.items)
      setTotal(response.data.total)
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('instances.loadFailed')))
    } finally {
      setLoading(false)
    }
  }, [page, pageSize, t])

  useEffect(() => {
    void load()
  }, [load])

  const openRegister = (
    engine: RegisterFormInput['engine'] = 'polardb_mysql',
  ) => {
    form.resetFields()
    form.setFieldsValue(
      engine === 'polarrag'
        ? {
            engine,
            topology: undefined,
            port: 9200,
            scheme: 'http',
            tls_verify: false,
          }
        : {
            engine,
            topology: 'single_tenant',
            port: 3306,
          },
    )
    setConnectionTestResult(null)
    setCreateOpen(true)
  }

  const handleRegister = async (values: RegisterFormInput) => {
    setCreateLoading(true)
    setError(null)
    try {
      if (values.engine === 'polarrag') {
        await createPolarRAGInstance({
          name: values.name.trim(),
          scheme: values.scheme ?? 'http',
          host: values.host.trim(),
          port: values.port,
          username: values.username.trim(),
          password: values.password,
          tls_verify: values.tls_verify ?? false,
          ca_bundle: values.ca_bundle?.trim() || null,
        })
        setSearchParams({ type: 'polarrag' })
        setPolarRAGRefreshKey((key) => key + 1)
      } else {
        await registerAdminInstance({
          cluster_id: values.cluster_id!.trim(),
          name: values.name.trim(),
          usage: values.usage?.trim() || undefined,
          engine: values.engine,
          topology: values.topology!,
          region: values.region?.trim() || undefined,
          host: values.host.trim(),
          port: values.port,
          username: values.username.trim(),
          password: values.password,
        })
        await load()
      }
      setCreateOpen(false)
      setConnectionTestResult(null)
      form.resetFields()
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, t('instances.registerFailed')),
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
        topology: values.topology!,
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
        message: getAPIErrorMessage(requestError, t('instances.testFailed')),
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
          t('instances.removeFailed'),
        ),
      )
    } finally {
      setRemoveLoading(false)
    }
  }

  return (
    <PageContainer
      title={t('instances.title')}
      description={t('instances.description')}
      actions={
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => openRegister()}
        >
          {t('instances.register')}
        </Button>
      }
    >
      <Tabs
        activeKey={activeTab}
        onChange={(key) =>
          setSearchParams(key === 'polarrag' ? { type: 'polarrag' } : {})
        }
        items={[
          {
            key: 'database',
            label: 'Database Instances',
            children: (
              <Space direction="vertical" size={20} style={{ width: '100%' }}>
        {error && (
          <Alert
            type="error"
            showIcon
            role="alert"
            message={error}
            action={
              <Button size="small" onClick={() => void load()}>
                {t('common.retry')}
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
                <Text strong>{t('instances.empty')}</Text>
                <Text type="secondary">
                  {t('instances.emptyDescription')}
                </Text>
              </Space>
            }
          >
            <Button
              type="primary"
              onClick={() => openRegister()}
            >
              {t('instances.register')}
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
                title: t('instances.instance'),
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
                title: t('instances.usage'),
                dataIndex: 'usage',
                render: (value: string | null) =>
                  value || <Text type="secondary">{t('instances.notSpecified')}</Text>,
              },
              {
                title: t('instances.engine'),
                dataIndex: 'engine',
                render: (value: string) => <Tag>{value}</Tag>,
              },
              {
                title: t('instances.topology'),
                dataIndex: 'topology',
                render: (value: string) => <Tag>{value}</Tag>,
              },
              {
                title: t('instances.allocationMode'),
                dataIndex: 'allocation_mode',
                render: (value: string) => <Tag>{value}</Tag>,
              },
              {
                title: t('instances.status'),
                dataIndex: 'status',
                render: (value: string) => (
                  <Tag color={statusColor(value)}>{value}</Tag>
                ),
              },
              {
                title: t('instances.provisioning'),
                render: (_: unknown, record: InstanceSummary) => (
                  <Provisioning instance={record} />
                ),
              },
              {
                title: t('instances.bindings'),
                render: (_: unknown, record: InstanceSummary) => (
                  <Space size={4} wrap>
                    <Tag>{t('instances.usersCount', { count: record.binding_counts.users })}</Tag>
                    <Tag>{t('instances.departmentsCount', { count: record.binding_counts.departments })}</Tag>
                    <Tag>{t('instances.agentsCount', { count: record.binding_counts.agents })}</Tag>
                  </Space>
                ),
              },
              {
                title: t('instances.actions'),
                fixed: 'right',
                render: (_: unknown, record: InstanceSummary) => (
                  <Button
                    size="small"
                    danger
                    aria-label={`${t('instances.remove')} ${record.name}`}
                    onClick={() => setRemoveTarget(record)}
                  >
                    {t('instances.remove')}
                  </Button>
                ),
              },
            ]}
          />
        )}
              </Space>
            ),
          },
          {
            key: 'polarrag',
            label: 'PolarRAG Instances',
            children: (
              <InstancesPanel
                key={polarRAGRefreshKey}
                onRegister={() => openRegister('polarrag')}
              />
            ),
          },
        ]}
      />

      <Modal
        title={t('instances.register')}
        width={760}
        open={createOpen}
        okText={t('instances.save')}
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
            scheme: 'http',
            tls_verify: false,
          }}
          onValuesChange={(changedValues) => {
            if ('engine' in changedValues) {
              const engine = changedValues.engine
              form.setFieldsValue(
                engine === 'polarrag'
                  ? {
                      cluster_id: undefined,
                      topology: undefined,
                      region: undefined,
                      usage: undefined,
                      port: 9200,
                      scheme: 'http',
                      tls_verify: false,
                    }
                  : {
                      topology: 'single_tenant',
                      port: 3306,
                      scheme: undefined,
                      tls_verify: undefined,
                      ca_bundle: undefined,
                    },
              )
            }
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
          <Row gutter={[16, 0]} role="group" aria-label={t('instances.identity')}>
            {selectedEngine === 'polardb_mysql' && (
              <Col xs={24} md={12}>
                <Form.Item
                  name="cluster_id"
                  label={t('instances.clusterId')}
                  rules={[{ required: true, whitespace: true }, { max: 255 }]}
                >
                  <Input placeholder="pc-xxx" autoComplete="off" />
                </Form.Item>
              </Col>
            )}
            <Col xs={24} md={selectedEngine === 'polarrag' ? 24 : 12}>
              <Form.Item
                name="name"
                label={t('instances.name')}
                rules={[{ required: true, whitespace: true }, { max: 255 }]}
              >
                <Input autoComplete="off" />
              </Form.Item>
            </Col>
          </Row>
          {selectedEngine === 'polardb_mysql' && (
            <Form.Item name="usage" label={t('instances.usage')} rules={[{ max: 1024 }]}>
              <Input.TextArea
                rows={2}
                showCount
                maxLength={1024}
                placeholder={t('instances.usagePlaceholder')}
              />
            </Form.Item>
          )}
          <Row
            gutter={[16, 0]}
            role="group"
            aria-label={t('instances.classification')}
          >
            <Col xs={24} md={12}>
              <Form.Item
                name="engine"
                label={t('instances.engine')}
                rules={[{ required: true }]}
              >
                <Select
                  options={[
                    { value: 'polardb_mysql', label: 'PolarDB for MySQL' },
                    { value: 'polarrag', label: 'PolarRAG' },
                  ]}
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              {selectedEngine === 'polardb_mysql' ? (
                <Form.Item
                  name="topology"
                  label={t('instances.topology')}
                  rules={[{ required: true }]}
                >
                  <Select
                    options={[
                      { value: 'single_tenant', label: 'Single tenant' },
                      { value: 'multitenant', label: 'Multi-tenant' },
                    ]}
                  />
                </Form.Item>
              ) : (
                <Form.Item
                  name="scheme"
                  label={t('instances.scheme')}
                  rules={[{ required: true }]}
                >
                  <Select
                    options={[
                      { value: 'https', label: 'HTTPS' },
                      { value: 'http', label: 'HTTP' },
                    ]}
                  />
                </Form.Item>
              )}
            </Col>
          </Row>
          <Row
            gutter={[16, 0]}
            role="group"
            aria-label={t('instances.location')}
          >
            {selectedEngine === 'polardb_mysql' && (
              <Col xs={24} md={12}>
                <Form.Item name="region" label={t('instances.region')} rules={[{ max: 64 }]}>
                  <Input placeholder="cn-hangzhou" autoComplete="off" />
                </Form.Item>
              </Col>
            )}
            <Col xs={24} md={12}>
              <Form.Item
                name="port"
                label={t('instances.port')}
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
          <Row role="group" aria-label={t('instances.endpoint')}>
            <Col span={24}>
              <Form.Item
                name="host"
                label={t('instances.host')}
                rules={[
                  { required: true, whitespace: true },
                  { max: 255 },
                ]}
              >
                <Input autoComplete="off" />
              </Form.Item>
            </Col>
          </Row>
          {selectedEngine === 'polarrag' && (
            <>
              <Form.Item
                name="tls_verify"
                label={t('instances.verifyTls')}
                valuePropName="checked"
              >
                <Switch
                  checkedChildren={<CheckCircleOutlined />}
                  unCheckedChildren="Off"
                />
              </Form.Item>
              <Form.Item name="ca_bundle" label={t('instances.caBundle')}>
                <Input.TextArea
                  rows={4}
                  placeholder={t('instances.pemChain')}
                  autoComplete="off"
                />
              </Form.Item>
            </>
          )}
          <Row
            gutter={[16, 0]}
            role="group"
            aria-label={t('instances.credentials')}
          >
            <Col xs={24} md={12}>
              <Form.Item
                name="username"
                label={t('instances.username')}
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
                label={t('instances.password')}
                rules={[{ required: true }, { max: 1024 }]}
              >
                <Input.Password autoComplete="new-password" />
              </Form.Item>
            </Col>
          </Row>
          {selectedEngine === 'polardb_mysql' ? (
            <>
              <Alert
                type="info"
                showIcon
                message={t('instances.mysqlPermissions')}
                description={t('instances.mysqlPermissionsDescription')}
              />
              <Button
                style={{ marginTop: 16 }}
                loading={connectionTestLoading}
                onClick={() => void handleTestConnection()}
              >
                {t('instances.testConnection')}
              </Button>
            </>
          ) : (
            <Alert
              type="info"
              showIcon
              message={t('instances.polarragCredentialTitle')}
              description={t('instances.polarragCredentialDescription')}
            />
          )}
          {selectedEngine === 'polardb_mysql' && connectionTestResult && (
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
                  ? t('instances.connectionSucceeded')
                  : connectionTestResult.message
              }
              style={{ marginTop: 12 }}
            />
          )}
        </Form>
      </Modal>

      <Modal
        title={t('instances.removeTitle', { name: removeTarget?.name ?? t('instances.instanceFallback') })}
        open={!!removeTarget}
        okText={t('instances.confirmRemove')}
        okButtonProps={{ danger: true }}
        confirmLoading={removeLoading}
        onOk={() => void handleRemove()}
        onCancel={() => {
          if (!removeLoading) setRemoveTarget(null)
        }}
      >
        <Text>
          {t('instances.removeDescription')}
        </Text>
      </Modal>
    </PageContainer>
  )
}
