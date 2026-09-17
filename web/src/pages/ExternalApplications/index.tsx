import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Checkbox,
  DatePicker,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
  type TableColumnsType,
} from 'antd'
import {
  KeyOutlined,
  PlusOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'

import { listAgents, type Agent } from '../../api/agents'
import { getAPIErrorMessage } from '../../api/client'
import {
  listEnterpriseIdentitySources,
  type EnterpriseIdentitySourceCandidate,
} from '../../api/enterpriseAccess'
import {
  createExternalApplication,
  getExternalApplicationContext,
  listExternalApplications,
  rotateExternalApplicationSecret,
  testExternalApplication,
  updateExternalApplicationStatus,
  type CreateExternalApplicationInput,
  type ExternalApplication,
  type ExternalApplicationAgentPolicy,
  type ExternalApplicationContext,
  type ExternalApplicationSecret,
  type ExternalApplicationTarget,
} from '../../api/externalApplications'
import PageContainer from '../../components/PageContainer'
import { formatDateTime } from '../../i18n/format'
import './index.css'

const { Paragraph, Text, Title } = Typography

interface DateValue {
  toISOString(): string
}

interface CreateFormValues {
  name: string
  targets: ExternalApplicationTarget[]
  agent_policy: ExternalApplicationAgentPolicy
  fixed_agent_id?: string
  secret_expires_at?: DateValue
}

interface TestFormValues {
  subject_token: string
  resource?: string
  agent_id?: string
  identity_source_id?: string
  feishu_user_id?: string
  feishu_union_id?: string
}

type Translate = ReturnType<typeof useTranslation>['t']

function policyLabel(
  policy: ExternalApplicationAgentPolicy,
  t: Translate,
) {
  return t(`externalApplications.policies.${policy}`)
}

function targetLabel(
  target: ExternalApplicationTarget,
  t: Translate,
) {
  return target === 'mcp'
    ? t('externalApplications.targetMcp')
    : t('externalApplications.targetApi')
}

function buildCurl(
  application: ExternalApplication,
  clientSecret = '${PAS_CLIENT_SECRET}',
) {
  const resource =
    application.resources.length === 1
      ? application.resources[0].resource
      : '${PAS_RESOURCE}'
  const scope =
    application.resources.length === 1
      ? application.resources[0].scope
      : '${PAS_SCOPE}'
  const agentLine =
    application.agent_policy === 'caller_selectable'
      ? " \\\n  --data-urlencode 'agent_id=${PAS_AGENT_ID}'"
      : ''
  const feishuContextLines = application.provider_type === 'feishu'
    ? " \\\n  --data-urlencode 'identity_source_id=${PAS_IDENTITY_SOURCE_ID}' \\\n  --data-urlencode 'feishu_user_id=${FEISHU_USER_ID}' \\\n  --data-urlencode 'feishu_union_id=${FEISHU_UNION_ID}'"
    : ''
  const subjectToken = application.provider_type === 'feishu'
    ? '${FEISHU_USER_ACCESS_TOKEN}'
    : '${EXTERNAL_ACCESS_TOKEN}'
  return `curl --request POST '${application.token_endpoint}' \\
  --user '${application.client_id}:${clientSecret}' \\
  --header 'Content-Type: application/x-www-form-urlencoded' \\
  --data-urlencode 'grant_type=urn:ietf:params:oauth:grant-type:token-exchange' \\
  --data-urlencode 'subject_token=${subjectToken}' \\
  --data-urlencode 'subject_token_type=urn:ietf:params:oauth:token-type:access_token' \\
  --data-urlencode 'resource=${resource}' \\
  --data-urlencode 'scope=${scope}'${agentLine}${feishuContextLines}`
}

export default function ExternalApplications() {
  const { t, i18n } = useTranslation()
  const navigate = useNavigate()
  const [context, setContext] =
    useState<ExternalApplicationContext | null>(null)
  const [applications, setApplications] = useState<ExternalApplication[]>([])
  const [applicationTotal, setApplicationTotal] = useState(0)
  const [applicationPage, setApplicationPage] = useState(1)
  const [applicationSearch, setApplicationSearch] = useState('')
  const [agents, setAgents] = useState<Agent[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [createLoading, setCreateLoading] = useState(false)
  const [selected, setSelected] = useState<ExternalApplication | null>(null)
  const [secret, setSecret] = useState<ExternalApplicationSecret | null>(null)
  const [statusTarget, setStatusTarget] =
    useState<ExternalApplication | null>(null)
  const [statusLoading, setStatusLoading] = useState(false)
  const [rotateTarget, setRotateTarget] =
    useState<ExternalApplication | null>(null)
  const [rotateLoading, setRotateLoading] = useState(false)
  const [testTarget, setTestTarget] =
    useState<ExternalApplication | null>(null)
  const [testLoading, setTestLoading] = useState(false)
  const [identitySources, setIdentitySources] = useState<
    EnterpriseIdentitySourceCandidate[]
  >([])
  const [identitySourcesLoading, setIdentitySourcesLoading] = useState(false)
  const [createForm] = Form.useForm<CreateFormValues>()
  const [rotateForm] =
    Form.useForm<{ secret_expires_at?: DateValue }>()
  const [testForm] = Form.useForm<TestFormValues>()
  const createPolicy = Form.useWatch('agent_policy', createForm)
  const testResource = Form.useWatch('resource', testForm)

  const load = useCallback(async (
    nextPage = applicationPage,
    nextSearch = applicationSearch,
  ) => {
    setLoading(true)
    setError(null)
    try {
      const [contextResponse, applicationsResponse, agentsResponse] =
        await Promise.all([
          getExternalApplicationContext(),
          listExternalApplications({
            offset: (nextPage - 1) * 20,
            limit: 20,
            ...(nextSearch ? { search: nextSearch } : {}),
          }),
          listAgents({ limit: 100 }),
        ])
      setContext(contextResponse.data)
      setApplications(applicationsResponse.data.items)
      setApplicationTotal(applicationsResponse.data.total)
      setApplicationPage(nextPage)
      setAgents(
        agentsResponse.data.items.filter((agent) => agent.status === 'active'),
      )
    } catch (requestError) {
      setError(
        getAPIErrorMessage(
          requestError,
          t('externalApplications.loadFailed'),
        ),
      )
    } finally {
      setLoading(false)
    }
  }, [applicationPage, applicationSearch, t])

  useEffect(() => {
    void load()
  }, [load])

  const agentOptions = useMemo(
    () => agents.map((agent) => ({ label: agent.name, value: agent.id })),
    [agents],
  )
  const feishuIdentitySourceOptions = useMemo(
    () => identitySources.map((source) => ({
      value: source.id,
      label: `${source.name} (${source.id})`,
    })),
    [identitySources],
  )

  const handleCreate = async (values: CreateFormValues) => {
    setCreateLoading(true)
    setError(null)
    const input: CreateExternalApplicationInput = {
      name: values.name.trim(),
      targets: values.targets,
      agent_policy: values.agent_policy,
      fixed_agent_id:
        values.agent_policy === 'fixed'
          ? values.fixed_agent_id ?? null
          : null,
      secret_expires_at: values.secret_expires_at?.toISOString() ?? null,
    }
    try {
      const response = await createExternalApplication(input)
      setApplications((current) => [response.data, ...current])
      setCreateOpen(false)
      createForm.resetFields()
      setSecret(response.data)
    } catch (requestError) {
      setError(
        getAPIErrorMessage(
          requestError,
          t('externalApplications.createFailed'),
        ),
      )
    } finally {
      setCreateLoading(false)
    }
  }

  const handleStatus = async () => {
    if (!statusTarget) return
    const status = statusTarget.status === 'active' ? 'disabled' : 'active'
    setStatusLoading(true)
    try {
      const response = await updateExternalApplicationStatus(
        statusTarget.client_id,
        status,
      )
      setApplications((current) =>
        current.map((application) =>
          application.client_id === response.data.client_id
            ? response.data
            : application,
        ),
      )
      setSelected((current) =>
        current?.client_id === response.data.client_id
          ? response.data
          : current,
      )
      setStatusTarget(null)
      message.success(t('externalApplications.statusUpdated'))
    } catch (requestError) {
      setError(
        getAPIErrorMessage(
          requestError,
          t('externalApplications.statusFailed'),
        ),
      )
    } finally {
      setStatusLoading(false)
    }
  }

  const handleRotate = async (
    values: { secret_expires_at?: DateValue },
  ) => {
    if (!rotateTarget) return
    setRotateLoading(true)
    try {
      const response = await rotateExternalApplicationSecret(
        rotateTarget.client_id,
        values.secret_expires_at?.toISOString() ?? null,
      )
      setApplications((current) =>
        current.map((application) =>
          application.client_id === response.data.client_id
            ? response.data
            : application,
        ),
      )
      setSelected((current) =>
        current?.client_id === response.data.client_id
          ? response.data
          : current,
      )
      setRotateTarget(null)
      rotateForm.resetFields()
      setSecret(response.data)
    } catch (requestError) {
      setError(
        getAPIErrorMessage(
          requestError,
          t('externalApplications.rotateFailed'),
        ),
      )
    } finally {
      setRotateLoading(false)
    }
  }

  const handleTest = async (values: TestFormValues) => {
    if (!testTarget) return
    setTestLoading(true)
    try {
      const response = await testExternalApplication(testTarget.client_id, {
        subject_token: values.subject_token.trim(),
        resource: values.resource
          || (testTarget.resources.length === 1
            ? testTarget.resources[0].resource
            : null),
        agent_id: values.agent_id || null,
        identity_source_id: values.identity_source_id || null,
        feishu_user_id: values.feishu_user_id || null,
        feishu_union_id: values.feishu_union_id || null,
      })
      message.success(
        t('externalApplications.testSucceeded', {
          scope: response.data.scope,
        }),
      )
      setTestTarget(null)
      testForm.resetFields()
      await load()
    } catch (requestError) {
      setError(
        getAPIErrorMessage(
          requestError,
          t('externalApplications.testFailed'),
        ),
      )
    } finally {
      setTestLoading(false)
    }
  }

  const openTest = async (application: ExternalApplication) => {
    testForm.resetFields()
    testForm.setFieldValue(
      'resource',
      application.resources.length === 1
        ? application.resources[0].resource
        : undefined,
    )
    setTestTarget(application)
    if (application.provider_type !== 'feishu') return
    setIdentitySourcesLoading(true)
    try {
      const { data } = await listEnterpriseIdentitySources()
      setIdentitySources(
        data.items.filter(
          (source) => source.provider === 'feishu' && source.status === 'active',
        ),
      )
    } catch (requestError) {
      setIdentitySources([])
      setError(
        getAPIErrorMessage(
          requestError,
          t('externalApplications.identitySourcesLoadFailed'),
        ),
      )
    } finally {
      setIdentitySourcesLoading(false)
    }
  }

  const columns: TableColumnsType<ExternalApplication> = [
    {
      title: t('externalApplications.application'),
      dataIndex: 'name',
      render: (name: string, application) => (
        <Button type="link" onClick={() => setSelected(application)}>
          {name}
        </Button>
      ),
    },
    {
      title: t('externalApplications.targets'),
      dataIndex: 'targets',
      render: (targets: ExternalApplicationTarget[]) => (
        <Space size={4}>
          {targets.map((target) => (
            <Tag key={target}>{targetLabel(target, t)}</Tag>
          ))}
        </Space>
      ),
    },
    {
      title: t('externalApplications.agentPolicy'),
      dataIndex: 'agent_policy',
      render: (policy: ExternalApplicationAgentPolicy) =>
        policyLabel(policy, t),
    },
    {
      title: t('externalApplications.status'),
      dataIndex: 'status',
      render: (status: ExternalApplication['status']) => (
        <Tag color={status === 'active' ? 'success' : 'default'}>
          {status === 'active'
            ? t('common.enabled')
            : t('common.disabled')}
        </Tag>
      ),
    },
    {
      title: t('externalApplications.lastUsed'),
      dataIndex: 'last_used_at',
      render: (value: string | null) =>
        value
          ? formatDateTime(value, i18n.language)
          : t('externalApplications.never'),
    },
    {
      title: t('externalApplications.actions'),
      key: 'actions',
      render: (_, application) => (
        <Space wrap>
          <Button size="small" onClick={() => setSelected(application)}>
            {t('externalApplications.details')}
          </Button>
          <Button
            size="small"
            icon={<SafetyCertificateOutlined />}
            disabled={application.status !== 'active'}
            onClick={() => void openTest(application)}
          >
            {t('externalApplications.test')}
          </Button>
        </Space>
      ),
    },
  ]

  return (
    <PageContainer
      title={t('externalApplications.title')}
      description={t('externalApplications.description')}
      actions={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void load()}>
            {t('externalApplications.refresh')}
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            disabled={!context?.provider_enabled}
            onClick={() => setCreateOpen(true)}
          >
            {t('externalApplications.create')}
          </Button>
        </Space>
      }
    >
      <Space direction="vertical" size={20} style={{ width: '100%' }}>
        {error && (
          <Alert
            type="error"
            showIcon
            closable
            message={error}
            onClose={() => setError(null)}
          />
        )}
        {context && !context.provider_enabled && (
          <Alert
            type="warning"
            showIcon
            message={t('externalApplications.providerRequired')}
            description={t('externalApplications.providerRequiredDescription')}
            action={
              <Button
                size="small"
                onClick={() => navigate(
                  '/settings/configuration?module=user_sso'
                  + '&section=external-token-trust',
                )}
              >
                {t('externalApplications.configureProvider')}
              </Button>
            }
          />
        )}
        {context?.provider_enabled && (
          <Alert
            type="success"
            showIcon
            message={t('externalApplications.providerReady', {
              provider: context.provider_type,
            })}
            description={t('externalApplications.providerReadyDescription')}
          />
        )}
        <Input.Search
          allowClear
          placeholder={t('common.search')}
          onSearch={(value) => {
            const nextSearch = value.trim()
            setApplicationSearch(nextSearch)
            void load(1, nextSearch)
          }}
        />
        <Table
          rowKey="client_id"
          loading={loading}
          dataSource={applications}
          columns={columns}
          pagination={{
            current: applicationPage,
            pageSize: 20,
            total: applicationTotal,
            showSizeChanger: false,
            onChange: (nextPage) => void load(nextPage),
          }}
          scroll={{ x: 920 }}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={t('externalApplications.empty')}
              >
                <Button
                  type="primary"
                  disabled={!context?.provider_enabled}
                  onClick={() => setCreateOpen(true)}
                >
                  {t('externalApplications.create')}
                </Button>
              </Empty>
            ),
          }}
        />
      </Space>

      <Modal
        title={t('externalApplications.createTitle')}
        open={createOpen}
        onCancel={() => {
          setCreateOpen(false)
          createForm.resetFields()
        }}
        onOk={() => createForm.submit()}
        confirmLoading={createLoading}
        destroyOnHidden
      >
        <Form<CreateFormValues>
          form={createForm}
          layout="vertical"
          initialValues={{
            targets: ['mcp'],
            agent_policy: 'workspace_default',
          }}
          onFinish={(values) => void handleCreate(values)}
        >
          <Form.Item
            name="name"
            label={t('externalApplications.name')}
            rules={[
              {
                required: true,
                whitespace: true,
                message: t('externalApplications.nameRequired'),
              },
            ]}
          >
            <Input autoComplete="off" />
          </Form.Item>
          <Form.Item
            name="targets"
            label={t('externalApplications.targets')}
            extra={t('externalApplications.targetsHelp')}
            rules={[
              {
                required: true,
                message: t('externalApplications.targetsRequired'),
              },
            ]}
          >
            <Checkbox.Group
              options={[
                {
                  label: t('externalApplications.targetMcp'),
                  value: 'mcp',
                },
                {
                  label: t('externalApplications.targetApi'),
                  value: 'api',
                },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="agent_policy"
            label={t('externalApplications.agentPolicy')}
          >
            <Select
              options={[
                'workspace_default',
                'fixed',
                'caller_selectable',
              ].map((value) => ({
                value,
                label: policyLabel(
                  value as ExternalApplicationAgentPolicy,
                  t,
                ),
              }))}
            />
          </Form.Item>
          {createPolicy === 'fixed' && (
            <Form.Item
              name="fixed_agent_id"
              label={t('externalApplications.fixedAgent')}
              rules={[
                {
                  required: true,
                  message: t('externalApplications.fixedAgentRequired'),
                },
              ]}
            >
              <Select
                showSearch
                optionFilterProp="label"
                options={agentOptions}
              />
            </Form.Item>
          )}
          <Form.Item
            name="secret_expires_at"
            label={t('externalApplications.secretExpiration')}
            extra={t('externalApplications.secretExpirationHelp')}
          >
            <DatePicker
              showTime
              style={{ width: '100%' }}
              disabledDate={(date) => date.valueOf() < Date.now()}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t('externalApplications.credentialsTitle')}
        open={secret !== null}
        closable={false}
        maskClosable={false}
        onOk={() => setSecret(null)}
        cancelButtonProps={{ style: { display: 'none' } }}
        okText={t('externalApplications.credentialsStored')}
        destroyOnHidden
        width={760}
      >
        {secret && (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Alert
              type="warning"
              showIcon
              message={t('externalApplications.secretOnce')}
            />
            <Descriptions column={1} bordered size="small">
              <Descriptions.Item label={t('externalApplications.clientId')}>
                <Text code copyable>{secret.client_id}</Text>
              </Descriptions.Item>
              <Descriptions.Item
                label={t('externalApplications.clientSecret')}
              >
                <Text code copyable style={{ wordBreak: 'break-all' }}>
                  {secret.client_secret}
                </Text>
              </Descriptions.Item>
            </Descriptions>
            <Title level={5}>{t('externalApplications.requestExample')}</Title>
            <Paragraph
              copyable={{ text: buildCurl(secret, secret.client_secret) }}
            >
              <pre className="external-application-code">
                {buildCurl(secret, secret.client_secret)}
              </pre>
            </Paragraph>
          </Space>
        )}
      </Modal>

      <Drawer
        title={selected?.name}
        open={selected !== null}
        onClose={() => setSelected(null)}
        width={720}
        extra={
          selected && (
            <Space>
              <Button
                icon={<KeyOutlined />}
                onClick={() => setRotateTarget(selected)}
              >
                {t('externalApplications.rotateSecret')}
              </Button>
              <Button
                danger={selected.status === 'active'}
                onClick={() => setStatusTarget(selected)}
              >
                {selected.status === 'active'
                  ? t('externalApplications.disable')
                  : t('externalApplications.enable')}
              </Button>
            </Space>
          )
        }
      >
        {selected && (
          <Space direction="vertical" size={20} style={{ width: '100%' }}>
            <Descriptions column={1} bordered size="small">
              <Descriptions.Item label={t('externalApplications.clientId')}>
                <Text code copyable>{selected.client_id}</Text>
              </Descriptions.Item>
              <Descriptions.Item label={t('externalApplications.provider')}>
                {selected.provider_type}
              </Descriptions.Item>
              <Descriptions.Item
                label={t('externalApplications.agentPolicy')}
              >
                {policyLabel(selected.agent_policy, t)}
              </Descriptions.Item>
              {selected.fixed_agent_id && (
                <Descriptions.Item
                  label={t('externalApplications.fixedAgent')}
                >
                  {agents.find((agent) => agent.id === selected.fixed_agent_id)
                    ?.name ?? selected.fixed_agent_id}
                </Descriptions.Item>
              )}
              <Descriptions.Item
                label={t('externalApplications.secretExpiration')}
              >
                {selected.secret_expires_at
                  ? formatDateTime(selected.secret_expires_at, i18n.language)
                  : t('externalApplications.noExpiration')}
              </Descriptions.Item>
            </Descriptions>
            <Title level={5}>{t('externalApplications.endpoints')}</Title>
            <Descriptions column={1} bordered size="small">
              <Descriptions.Item
                label={t('externalApplications.standardEndpoint')}
              >
                <Text code copyable>{selected.token_endpoint}</Text>
              </Descriptions.Item>
              <Descriptions.Item
                label={t('externalApplications.compatibilityEndpoint')}
              >
                <Text code copyable>
                  {selected.compatibility_token_endpoint}
                </Text>
              </Descriptions.Item>
            </Descriptions>
            <Title level={5}>{t('externalApplications.resources')}</Title>
            <Descriptions column={1} bordered size="small">
              {selected.resources.map((profile) => (
                <Descriptions.Item
                  key={profile.target}
                  label={targetLabel(profile.target, t)}
                >
                  <Space direction="vertical" size={4}>
                    <Text code copyable>{profile.resource}</Text>
                    <Text type="secondary">
                      {t('externalApplications.scopeValue', {
                        scope: profile.scope,
                      })}
                    </Text>
                  </Space>
                </Descriptions.Item>
              ))}
            </Descriptions>
            <Title level={5}>{t('externalApplications.requestExample')}</Title>
            <Paragraph copyable={{ text: buildCurl(selected) }}>
              <pre className="external-application-code">
                {buildCurl(selected)}
              </pre>
            </Paragraph>
          </Space>
        )}
      </Drawer>

      <Modal
        title={t('externalApplications.rotateTitle')}
        open={rotateTarget !== null}
        onCancel={() => {
          setRotateTarget(null)
          rotateForm.resetFields()
        }}
        onOk={() => rotateForm.submit()}
        confirmLoading={rotateLoading}
        destroyOnHidden
      >
        <Alert
          type="warning"
          showIcon
          message={t('externalApplications.rotateWarning')}
          style={{ marginBottom: 16 }}
        />
        <Form
          form={rotateForm}
          layout="vertical"
          onFinish={(values) => void handleRotate(values)}
        >
          <Form.Item
            name="secret_expires_at"
            label={t('externalApplications.secretExpiration')}
          >
            <DatePicker
              showTime
              style={{ width: '100%' }}
              disabledDate={(date) => date.valueOf() < Date.now()}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t('externalApplications.statusTitle')}
        open={statusTarget !== null}
        onCancel={() => setStatusTarget(null)}
        onOk={() => void handleStatus()}
        confirmLoading={statusLoading}
      >
        <Text>
          {statusTarget?.status === 'active'
            ? t('externalApplications.disableWarning')
            : t('externalApplications.enableDescription')}
        </Text>
      </Modal>

      <Modal
        title={t('externalApplications.testTitle')}
        open={testTarget !== null}
        onCancel={() => {
          setTestTarget(null)
          testForm.resetFields()
        }}
        onOk={() => testForm.submit()}
        confirmLoading={testLoading}
        destroyOnHidden
      >
        <Alert
          type="info"
          showIcon
          message={t('externalApplications.testDescription')}
          style={{ marginBottom: 16 }}
        />
        <Form
          form={testForm}
          layout="vertical"
          onFinish={(values) => void handleTest(values)}
        >
          <Form.Item
            name="subject_token"
            label={t('externalApplications.subjectToken')}
            rules={[
              {
                required: true,
                whitespace: true,
                message: t('externalApplications.subjectTokenRequired'),
              },
            ]}
          >
            <Input.Password autoComplete="off" />
          </Form.Item>
          {testTarget?.provider_type === 'feishu' && (
            <>
              <Form.Item
                name="identity_source_id"
                label={t('externalApplications.feishuIdentitySource')}
                rules={[
                  {
                    required: true,
                    message: t('externalApplications.feishuIdentitySourceRequired'),
                  },
                ]}
              >
                <Select
                  loading={identitySourcesLoading}
                  options={feishuIdentitySourceOptions}
                  placeholder={t('externalApplications.feishuIdentitySourcePlaceholder')}
                />
              </Form.Item>
              <Form.Item
                name="feishu_user_id"
                label={t('externalApplications.feishuUserId')}
                rules={[
                  {
                    required: true,
                    whitespace: true,
                    message: t('externalApplications.feishuUserIdRequired'),
                  },
                ]}
              >
                <Input autoComplete="off" />
              </Form.Item>
              <Form.Item
                name="feishu_union_id"
                label={t('externalApplications.feishuUnionId')}
                rules={[
                  {
                    required: true,
                    whitespace: true,
                    message: t('externalApplications.feishuUnionIdRequired'),
                  },
                ]}
              >
                <Input autoComplete="off" />
              </Form.Item>
            </>
          )}
          {testTarget && testTarget.resources.length > 1 && (
            <Form.Item
              name="resource"
              label={t('externalApplications.resource')}
              rules={[
                {
                  required: true,
                  message: t('externalApplications.resourceRequired'),
                },
              ]}
            >
              <Select
                options={testTarget.resources.map((profile) => ({
                  value: profile.resource,
                  label: targetLabel(profile.target, t),
                }))}
              />
            </Form.Item>
          )}
          {testTarget?.agent_policy === 'caller_selectable' && (
            <Form.Item
              name="agent_id"
              label={t('externalApplications.agent')}
              extra={t('externalApplications.agentOptional')}
            >
              <Select
                allowClear
                showSearch
                optionFilterProp="label"
                options={agentOptions}
              />
            </Form.Item>
          )}
          {testResource && (
            <Text type="secondary">
              {t('externalApplications.testingResource', {
                resource: testResource,
              })}
            </Text>
          )}
        </Form>
      </Modal>
    </PageContainer>
  )
}
