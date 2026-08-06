import {
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import {
  Alert,
  Button,
  Descriptions,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Skeleton,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
} from 'antd'

import { getAPIErrorMessage } from '../../api/client'
import {
  createInstanceCredential,
  listInstanceCredentials,
  revealCredential,
  revokeCredential,
  testInstanceCredentialConnection,
  updateCredential,
  type CreateCredentialInput,
  type InstanceCredential,
} from '../../api/credentials'
import {
  getAdminInstance,
  testStoredInstanceConnection,
  updateAdminInstance,
  type InstanceSummary,
  type UpdateInstanceInput,
} from '../../api/instances'
import {
  createProvisioningBackend,
  disableProvisioningBackend,
  drainProvisioningBackend,
  listProvisioningBackends,
  updateProvisioningBackend,
  type CreateProvisioningBackendInput,
  type ProvisioningBackend,
} from '../../api/provisioningBackends'
import CredentialReveal from '../../components/CredentialReveal'
import PageContainer from '../../components/PageContainer'

const { Text, Title } = Typography

interface RouteScope {
  instanceId: string
  generation: number
  mounted: boolean
}

type Confirmation =
  | { kind: 'revoke'; credential: InstanceCredential }
  | { kind: 'drain'; backend: ProvisioningBackend }
  | { kind: 'disable'; backend: ProvisioningBackend }
  | null

interface CredentialFormValues extends CreateCredentialInput {
  include_database: boolean
}

type ConnectionTestResult =
  | { status: 'success' }
  | { status: 'error'; message: string }
  | null

interface InstanceFormValues {
  cluster_id: string
  name: string
  usage?: string
  region?: string
  host: string
  port: number
  test_credential_id?: string
}

function statusColor(status: string) {
  if (status === 'active') return 'green'
  if (status === 'disabled' || status === 'revoked') return 'red'
  return 'orange'
}

export default function InstanceDetail() {
  const { t } = useTranslation()
  const { id = '' } = useParams<{ id: string }>()
  const scopeRef = useRef<RouteScope>({
    instanceId: id,
    generation: 0,
    mounted: true,
  })
  const [instance, setInstance] = useState<InstanceSummary | null>(null)
  const [credentials, setCredentials] = useState<InstanceCredential[]>([])
  const [backend, setBackend] = useState<ProvisioningBackend | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [instanceFormOpen, setInstanceFormOpen] = useState(false)
  const [endpointChanged, setEndpointChanged] = useState(false)
  const [endpointTestResult, setEndpointTestResult] =
    useState<ConnectionTestResult>(null)
  const [endpointTestLoading, setEndpointTestLoading] = useState(false)
  const [credentialFormOpen, setCredentialFormOpen] = useState(false)
  const [editingCredential, setEditingCredential] =
    useState<InstanceCredential | null>(null)
  const [credentialTestResult, setCredentialTestResult] =
    useState<ConnectionTestResult>(null)
  const [credentialTestLoading, setCredentialTestLoading] = useState(false)
  const [backendFormOpen, setBackendFormOpen] = useState(false)
  const [credentialPurpose, setCredentialPurpose] =
    useState<CreateCredentialInput['purpose']>('direct_access')
  const [confirmation, setConfirmation] = useState<Confirmation>(null)
  const [instanceForm] = Form.useForm<InstanceFormValues>()
  const [credentialForm] = Form.useForm<CredentialFormValues>()
  const [backendForm] = Form.useForm<CreateProvisioningBackendInput>()

  const isCurrent = useCallback((scope: RouteScope) => {
    const current = scopeRef.current
    return (
      current.mounted &&
      current.instanceId === scope.instanceId &&
      current.generation === scope.generation
    )
  }, [])

  const load = useCallback(
    async (scope: RouteScope) => {
      try {
        const [instanceResponse, credentialResponse, backendsResponse] =
          await Promise.all([
            getAdminInstance(scope.instanceId),
            listInstanceCredentials(scope.instanceId),
            listProvisioningBackends(),
          ])
        if (!isCurrent(scope)) return
        setInstance(instanceResponse.data)
        setCredentials(credentialResponse.data)
        setBackend(
          backendsResponse.data.find(
            (item) => item.instance_id === scope.instanceId,
          ) ?? null,
        )
      } catch (requestError) {
        if (!isCurrent(scope)) return
        setError(
          getAPIErrorMessage(
            requestError,
            t('instanceDetail.loadFailed'),
          ),
        )
      } finally {
        if (isCurrent(scope)) setLoading(false)
      }
    },
    [isCurrent, t],
  )

  useLayoutEffect(() => {
    const scope = {
      instanceId: id,
      generation: scopeRef.current.generation + 1,
      mounted: true,
    }
    scopeRef.current = scope
    setInstance(null)
    setCredentials([])
    setBackend(null)
    setLoading(true)
    setBusy(false)
    setError(null)
    setNotice(null)
    setInstanceFormOpen(false)
    setCredentialFormOpen(false)
    setEditingCredential(null)
    setCredentialTestResult(null)
    setCredentialPurpose('direct_access')
    setBackendFormOpen(false)
    setConfirmation(null)
    void load(scope)
    return () => {
      if (scopeRef.current.generation === scope.generation) {
        scopeRef.current = { ...scope, mounted: false }
      }
    }
  }, [id, load])

  const activeProvisioningCredentials = useMemo(
    () =>
      credentials.filter(
        (item) =>
          item.status === 'active' &&
          item.purpose === 'provisioning_admin' &&
          item.capability === 'admin',
      ),
    [credentials],
  )

  const openInstanceForm = () => {
    if (!instance) return
    instanceForm.setFieldsValue({
      cluster_id: instance.cluster_id,
      name: instance.name,
      usage: instance.usage ?? undefined,
      region: instance.region ?? undefined,
      host: instance.host ?? '',
      port: instance.port ?? 3306,
      test_credential_id: backend?.admin_credential_id,
    })
    setEndpointChanged(false)
    setEndpointTestResult(null)
    setInstanceFormOpen(true)
  }

  const handleInstanceSave = async (values: InstanceFormValues) => {
    const scope = { ...scopeRef.current }
    const input: UpdateInstanceInput = {
      name: values.name.trim(),
      usage: values.usage?.trim() || null,
      host: values.host.trim(),
      port: values.port,
      ...(values.region?.trim()
        ? { region: values.region.trim() }
        : {}),
      ...(endpointChanged && values.test_credential_id
        ? { test_credential_id: values.test_credential_id }
        : {}),
    }
    setBusy(true)
    setError(null)
    try {
      const response = await updateAdminInstance(scope.instanceId, input)
      if (!isCurrent(scope)) return
      setInstance(response.data)
      setInstanceFormOpen(false)
      setNotice(t('instanceDetail.updated'))
    } catch (requestError) {
      if (!isCurrent(scope)) return
      setError(
        getAPIErrorMessage(
          requestError,
          t('instanceDetail.updateFailed'),
        ),
      )
    } finally {
      if (isCurrent(scope)) setBusy(false)
    }
  }

  const handleEndpointTest = async () => {
    const scope = { ...scopeRef.current }
    setEndpointTestLoading(true)
    setEndpointTestResult(null)
    try {
      const values = await instanceForm.validateFields([
        'host',
        'port',
        'test_credential_id',
      ])
      await testStoredInstanceConnection(scope.instanceId, {
        host: values.host.trim(),
        port: values.port,
        credential_id: values.test_credential_id!,
      })
      if (isCurrent(scope)) setEndpointTestResult({ status: 'success' })
    } catch (requestError) {
      if (
        requestError &&
        typeof requestError === 'object' &&
        'errorFields' in requestError
      ) {
        return
      }
      if (isCurrent(scope)) {
        setEndpointTestResult({
          status: 'error',
          message: getAPIErrorMessage(
            requestError,
            t('instanceDetail.connectionFailed'),
          ),
        })
      }
    } finally {
      if (isCurrent(scope)) setEndpointTestLoading(false)
    }
  }

  const openBackendForm = () => {
    backendForm.setFieldsValue(
      backend
        ? {
            instance_id: backend.instance_id,
            admin_credential_id: backend.admin_credential_id,
            priority: backend.priority,
            max_active_resources: backend.max_active_resources,
            resource_min_cpu: backend.resource_min_cpu,
            resource_max_cpu: backend.resource_max_cpu,
            ddl_concurrency: backend.ddl_concurrency,
          }
        : {
            instance_id: id,
            priority: 0,
            max_active_resources: 20,
            resource_min_cpu: 1,
            resource_max_cpu: 4,
            ddl_concurrency: 2,
          },
    )
    setBackendFormOpen(true)
  }

  const handleCredentialCreate = async (values: CredentialFormValues) => {
    const scope = { ...scopeRef.current }
    setBusy(true)
    setError(null)
    try {
      const databaseName = values.include_database
        ? values.database_name?.trim() || null
        : null
      const response = editingCredential
        ? await updateCredential(editingCredential.id, {
            expected_version: editingCredential.version,
            name: values.name.trim(),
            capability: values.capability,
            ...(values.username.trim()
              ? { username: values.username.trim() }
              : {}),
            ...(values.password ? { password: values.password } : {}),
            database_name: databaseName,
          })
        : await createInstanceCredential(scope.instanceId, {
            name: values.name.trim(),
            purpose: values.purpose,
            capability: values.capability,
            username: values.username,
            password: values.password,
            database_name: databaseName,
          })
      if (!isCurrent(scope)) return
      setCredentials((current) =>
        editingCredential
          ? current.map((item) =>
              item.id === response.data.id ? response.data : item,
            )
          : [...current, response.data],
      )
      setCredentialFormOpen(false)
      setEditingCredential(null)
      setCredentialTestResult(null)
      credentialForm.resetFields()
      setNotice(
        t('instanceDetail.credentialSaved'),
      )
    } catch (requestError) {
      if (!isCurrent(scope)) return
      setError(
        getAPIErrorMessage(requestError, t('instanceDetail.credentialCreateFailed')),
      )
    } finally {
      if (isCurrent(scope)) setBusy(false)
    }
  }

  const handleCredentialTest = async () => {
    const scope = { ...scopeRef.current }
    setCredentialTestLoading(true)
    setCredentialTestResult(null)
    try {
      const values = await credentialForm.validateFields([
        'purpose',
        'capability',
        'username',
        'password',
        'include_database',
        'database_name',
      ])
      await testInstanceCredentialConnection(scope.instanceId, {
        purpose: values.purpose,
        capability: values.capability,
        ...(editingCredential
          ? {
              credential_id: editingCredential.id,
              expected_version: editingCredential.version,
              ...(values.username.trim()
                ? { username: values.username.trim() }
                : {}),
              ...(values.password ? { password: values.password } : {}),
            }
          : {
              username: values.username,
              password: values.password,
            }),
        database_name: values.include_database
          ? values.database_name?.trim() || null
          : null,
      })
      if (isCurrent(scope)) {
        setCredentialTestResult({ status: 'success' })
      }
    } catch (requestError) {
      if (
        requestError &&
        typeof requestError === 'object' &&
        'errorFields' in requestError
      ) {
        return
      }
      if (isCurrent(scope)) {
        setCredentialTestResult({
          status: 'error',
          message: getAPIErrorMessage(
            requestError,
            t('instanceDetail.connectionFailed'),
          ),
        })
      }
    } finally {
      if (isCurrent(scope)) setCredentialTestLoading(false)
    }
  }

  const handleBackendSave = async (
    values: CreateProvisioningBackendInput,
  ) => {
    const scope = { ...scopeRef.current }
    const currentBackend = backend
    setBusy(true)
    setError(null)
    try {
      const response = currentBackend
        ? await updateProvisioningBackend(currentBackend.id, {
            admin_credential_id: values.admin_credential_id,
            priority: values.priority,
            max_active_resources: values.max_active_resources,
            resource_min_cpu: values.resource_min_cpu,
            resource_max_cpu: values.resource_max_cpu,
            ddl_concurrency: values.ddl_concurrency,
          })
        : await createProvisioningBackend({
            ...values,
            instance_id: scope.instanceId,
          })
      if (!isCurrent(scope)) return
      setBackend(response.data)
      setBackendFormOpen(false)
      setNotice(
        currentBackend
          ? t('instanceDetail.backendUpdated')
          : t('instanceDetail.backendActivated'),
      )
    } catch (requestError) {
      if (!isCurrent(scope)) return
      setError(
        getAPIErrorMessage(
          requestError,
          t('instanceDetail.backendSaveFailed'),
        ),
      )
    } finally {
      if (isCurrent(scope)) setBusy(false)
    }
  }

  const performConfirmation = async () => {
    if (!confirmation) return
    const scope = { ...scopeRef.current }
    const action = confirmation
    setBusy(true)
    setError(null)
    try {
      if (action.kind === 'revoke') {
        const response = await revokeCredential(action.credential.id)
        if (!isCurrent(scope)) return
        setCredentials((current) =>
          current.map((item) =>
            item.id === response.data.id ? response.data : item,
          ),
        )
        setNotice(t('instanceDetail.credentialRevoked'))
      } else if (action.kind === 'drain') {
        const response = await drainProvisioningBackend(action.backend.id)
        if (!isCurrent(scope)) return
        setBackend(response.data)
        setNotice(
          t('instanceDetail.backendDraining'),
        )
      } else {
        const response = await disableProvisioningBackend(action.backend.id)
        if (!isCurrent(scope)) return
        setBackend(response.data)
        setNotice(
          t('instanceDetail.backendDisabled'),
        )
      }
      setConfirmation(null)
    } catch (requestError) {
      if (!isCurrent(scope)) return
      setError(
        getAPIErrorMessage(requestError, t('instanceDetail.operationFailed')),
      )
    } finally {
      if (isCurrent(scope)) setBusy(false)
    }
  }

  const reactivateBackend = async () => {
    if (!backend) return
    const scope = { ...scopeRef.current }
    const currentBackend = backend
    setBusy(true)
    setError(null)
    try {
      const response = await updateProvisioningBackend(currentBackend.id, {
        status: 'active',
      })
      if (!isCurrent(scope)) return
      setBackend(response.data)
      setNotice(t('instanceDetail.backendReactivated'))
    } catch (requestError) {
      if (!isCurrent(scope)) return
      setError(
        getAPIErrorMessage(
          requestError,
          t('instanceDetail.reactivateFailed'),
        ),
      )
    } finally {
      if (isCurrent(scope)) setBusy(false)
    }
  }

  if (loading) {
    return (
      <PageContainer title={t('instances.instance')} description={t('instanceDetail.loading')}>
        <Skeleton active paragraph={{ rows: 9 }} />
      </PageContainer>
    )
  }

  if (!instance) {
    return (
      <PageContainer title={t('instances.instance')} description={t('instanceDetail.unavailable')}>
        <Alert
          type="error"
          showIcon
          role="alert"
          message={error ?? t('instanceDetail.notFound')}
          action={
            <Button
              size="small"
              onClick={() => {
                const scope = { ...scopeRef.current, mounted: true }
                scopeRef.current = scope
                setLoading(true)
                void load(scope)
              }}
            >
              {t('common.retry')}
            </Button>
          }
        />
      </PageContainer>
    )
  }

  const canProvision =
    instance.engine === 'polardb_mysql' &&
    instance.topology === 'multitenant'

  return (
    <PageContainer
      title={instance.name}
      description={`${t('instanceDetail.physicalInstance')} ${instance.cluster_id}`}
    >
      <Space direction="vertical" size={28} style={{ width: '100%' }}>
        {error && (
          <Alert type="error" showIcon role="alert" message={error} closable />
        )}
        {notice && (
          <Alert
            type="info"
            showIcon
            role="status"
            message={notice}
            closable
            onClose={() => setNotice(null)}
          />
        )}

        <section aria-labelledby="instance-overview-heading">
          <Space
            align="start"
            style={{ width: '100%', justifyContent: 'space-between' }}
            wrap
          >
            <Title id="instance-overview-heading" level={4}>
              {t('instanceDetail.physicalInstance')}
            </Title>
            {instance.allocation_mode === 'registered' && (
              <Button onClick={openInstanceForm}>{t('instanceDetail.editInstance')}</Button>
            )}
          </Space>
          <Descriptions bordered column={{ xs: 1, sm: 2 }} size="small">
            <Descriptions.Item label={t('instances.engine')}>
              <Tag>{instance.engine}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label={t('instances.topology')}>
              <Tag>{instance.topology}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label={t('instances.allocationMode')}>
              <Tag>{instance.allocation_mode}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label={t('instances.status')}>
              <Tag color={statusColor(instance.status)}>{instance.status}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label={t('instances.usage')} span={2}>
              {instance.usage || t('instances.notSpecified')}
            </Descriptions.Item>
            <Descriptions.Item label={t('instances.region')}>
              {instance.region || t('instanceDetail.notSet')}
            </Descriptions.Item>
            <Descriptions.Item label={t('instances.endpoint')}>
              {instance.host
                ? `${instance.host}:${instance.port ?? 'default'}`
                : t('instanceDetail.notSet')}
            </Descriptions.Item>
            <Descriptions.Item label={t('instances.bindings')}>
              {t('instances.usersCount', { count: instance.binding_counts.users })} ·{' '}
              {t('instances.departmentsCount', { count: instance.binding_counts.departments })} ·{' '}
              {t('instances.agentsCount', { count: instance.binding_counts.agents })}
            </Descriptions.Item>
          </Descriptions>
        </section>

        <Modal
          title={t('instanceDetail.editInstance')}
          open={instanceFormOpen}
          onCancel={() => {
            if (!busy) setInstanceFormOpen(false)
          }}
          footer={null}
          destroyOnHidden
        >
          <Alert
            type="info"
            showIcon
            message={t('instanceDetail.identityCredentialsSeparate')}
            description={t('instanceDetail.identityCredentialsDescription')}
            style={{ marginBottom: 16 }}
          />
          <Form
            form={instanceForm}
            layout="vertical"
            onFinish={(values) => void handleInstanceSave(values)}
            onValuesChange={(changed) => {
              if ('host' in changed || 'port' in changed) {
                const host = instanceForm.getFieldValue('host')?.trim()
                const port = instanceForm.getFieldValue('port')
                setEndpointChanged(
                  host !== instance.host || port !== instance.port,
                )
                setEndpointTestResult(null)
              }
              if ('test_credential_id' in changed) {
                setEndpointTestResult(null)
              }
            }}
          >
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
                columnGap: 16,
              }}
            >
              <Form.Item name="cluster_id" label={t('instances.clusterId')}>
                <Input disabled />
              </Form.Item>
              <Form.Item
                name="name"
                label={t('instanceDetail.instanceName')}
                rules={[
                  { required: true, whitespace: true },
                  { max: 255 },
                ]}
              >
                <Input />
              </Form.Item>
              <Form.Item
                name="region"
                label={t('instances.region')}
                rules={[
                  ...(instance.region
                    ? [{ required: true, whitespace: true }]
                    : []),
                  { max: 64 },
                ]}
              >
                <Input placeholder="cn-hangzhou" />
              </Form.Item>
              <Form.Item
                name="port"
                label={t('instances.port')}
                rules={[
                  { required: true, type: 'number', min: 1, max: 65535 },
                ]}
              >
                <InputNumber
                  min={1}
                  max={65535}
                  precision={0}
                  style={{ width: '100%' }}
                />
              </Form.Item>
            </div>
            <Form.Item
              name="host"
              label={t('instances.host')}
              rules={[
                { required: true, whitespace: true },
                { max: 255 },
              ]}
            >
              <Input />
            </Form.Item>
            {endpointChanged && (
              <>
                <Form.Item
                  name="test_credential_id"
                  label={t('instanceDetail.testWithCredential')}
                  rules={[{ required: true }]}
                >
                  <Select
                    disabled={!!backend}
                    options={credentials
                      .filter((item) => item.status === 'active')
                      .map((item) => ({
                        value: item.id,
                        label: item.name,
                      }))}
                  />
                </Form.Item>
                <Button
                  loading={endpointTestLoading}
                  onClick={() => void handleEndpointTest()}
                >
                  {t('instances.testConnection')}
                </Button>
                {endpointTestResult && (
                  <Alert
                    type={
                      endpointTestResult.status === 'success'
                        ? 'success'
                        : 'error'
                    }
                    showIcon
                    role={
                      endpointTestResult.status === 'success'
                        ? 'status'
                        : 'alert'
                    }
                    message={
                      endpointTestResult.status === 'success'
                        ? t('instances.connectionSucceeded')
                        : endpointTestResult.message
                    }
                    style={{ marginTop: 12, marginBottom: 16 }}
                  />
                )}
              </>
            )}
            <Form.Item
              name="usage"
              label={t('instances.usage')}
              rules={[{ max: 1024 }]}
            >
              <Input.TextArea rows={3} showCount maxLength={1024} />
            </Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" loading={busy}>
                {t('instances.save')}
              </Button>
              <Button
                disabled={busy}
                onClick={() => setInstanceFormOpen(false)}
              >
                {t('common.cancel')}
              </Button>
            </Space>
          </Form>
        </Modal>

        <section aria-labelledby="credentials-heading">
          <Space
            align="start"
            style={{ width: '100%', justifyContent: 'space-between' }}
            wrap
          >
            <div>
              <Title id="credentials-heading" level={4}>
                {t('instanceDetail.credentialsTitle')}
              </Title>
              <Text type="secondary">
                {t('instanceDetail.credentialsDescription')}
              </Text>
            </div>
            <Button
              type="primary"
              onClick={() => {
                credentialForm.resetFields()
                setEditingCredential(null)
                setCredentialTestResult(null)
                setCredentialPurpose('direct_access')
                setCredentialFormOpen(true)
              }}
            >
              {t('instanceDetail.addCredential')}
            </Button>
          </Space>

          {credentialFormOpen && (
            <div
              style={{
                background: 'var(--surface-tertiary)',
                borderRadius: 'var(--radius-md)',
                padding: 16,
                marginTop: 16,
              }}
            >
              <Title level={5}>
                {editingCredential ? t('instanceDetail.editCredential') : t('instanceDetail.addCredential')}
              </Title>
              <Form
                form={credentialForm}
                layout="vertical"
                initialValues={{
                  purpose: 'direct_access',
                  capability: 'readonly',
                  include_database: false,
                }}
                onValuesChange={() => setCredentialTestResult(null)}
                onFinish={(values) => void handleCredentialCreate(values)}
                style={{ maxWidth: 680 }}
              >
                <Form.Item
                  name="name"
                  label={t('instanceDetail.credentialName')}
                  rules={[{ required: true, whitespace: true }, { max: 255 }]}
                >
                  <Input autoComplete="off" />
                </Form.Item>
                <Form.Item name="purpose" label={t('instanceDetail.purpose')} rules={[{ required: true }]}>
                  <Select
                    disabled={!!editingCredential}
                    onChange={(purpose) => {
                      setCredentialPurpose(purpose)
                      credentialForm.setFieldsValue({
                        capability:
                          purpose === 'provisioning_admin'
                            ? 'admin'
                            : 'readonly',
                        include_database: false,
                        database_name: undefined,
                      })
                    }}
                    options={[
                      { value: 'direct_access', label: t('instanceDetail.directAccess') },
                      ...(canProvision
                        ? [
                            {
                              value: 'provisioning_admin',
                              label: t('instanceDetail.provisioningAdmin'),
                            },
                          ]
                        : []),
                    ]}
                  />
                </Form.Item>
                <Form.Item
                  name="capability"
                  label={t('instanceDetail.capability')}
                  rules={[{ required: true }]}
                >
                  <Select
                    options={
                      credentialPurpose === 'provisioning_admin'
                        ? [{ value: 'admin', label: t('instanceDetail.administrator') }]
                        : [
                            { value: 'readonly', label: t('instanceDetail.readOnly') },
                            { value: 'readwrite', label: t('instanceDetail.readWrite') },
                          ]
                    }
                  />
                </Form.Item>
                <Form.Item
                  name="username"
                  label={t('instances.username')}
                  extra={
                    editingCredential
                      ? t('instanceDetail.keepUsername')
                      : undefined
                  }
                  rules={[
                    { required: !editingCredential },
                    { max: 255 },
                  ]}
                >
                  <Input autoComplete="off" />
                </Form.Item>
                <Form.Item
                  name="password"
                  label={t('instances.password')}
                  extra={
                    editingCredential
                      ? t('instanceDetail.keepPassword')
                      : undefined
                  }
                  rules={[
                    { required: !editingCredential },
                    { max: 1024 },
                  ]}
                >
                  <Input.Password autoComplete="new-password" />
                </Form.Item>
                {credentialPurpose === 'direct_access' && (
                  <>
                    <Form.Item
                      name="include_database"
                      label={t('instanceDetail.scopeDatabase')}
                      valuePropName="checked"
                    >
                      <Switch />
                    </Form.Item>
                    <Form.Item
                      noStyle
                      shouldUpdate={(previous, current) =>
                        previous.include_database !== current.include_database
                      }
                    >
                      {({ getFieldValue }) =>
                        getFieldValue('include_database') ? (
                          <Form.Item
                            name="database_name"
                            label={t('instanceDetail.databaseName')}
                            rules={[
                              { required: true, whitespace: true },
                              { max: 255 },
                            ]}
                          >
                            <Input autoComplete="off" />
                          </Form.Item>
                        ) : null
                      }
                    </Form.Item>
                  </>
                )}
                <Space>
                  <Button
                    loading={credentialTestLoading}
                    onClick={() => void handleCredentialTest()}
                  >
                    {t('instances.testConnection')}
                  </Button>
                  <Button type="primary" htmlType="submit" loading={busy}>
                    {t('instanceDetail.saveCredential')}
                  </Button>
                  <Button
                    disabled={busy}
                    onClick={() => {
                      setCredentialFormOpen(false)
                      setEditingCredential(null)
                      setCredentialTestResult(null)
                      setCredentialPurpose('direct_access')
                      credentialForm.resetFields()
                    }}
                  >
                    {t('common.cancel')}
                  </Button>
                </Space>
                {credentialTestResult && (
                  <Alert
                    type={
                      credentialTestResult.status === 'success'
                        ? 'success'
                        : 'error'
                    }
                    showIcon
                    role={
                      credentialTestResult.status === 'success'
                        ? 'status'
                        : 'alert'
                    }
                    message={
                      credentialTestResult.status === 'success'
                        ? t('instances.connectionSucceeded')
                        : credentialTestResult.message
                    }
                    style={{ marginTop: 12 }}
                  />
                )}
              </Form>
            </div>
          )}

          {credentials.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={t('instanceDetail.addCredentialFirst')}
            />
          ) : (
            <Table
              rowKey="id"
              dataSource={credentials}
              pagination={false}
              scroll={{ x: 880 }}
              style={{ marginTop: 16 }}
              columns={[
                { title: t('instances.name'), dataIndex: 'name' },
                {
                  title: t('instanceDetail.purpose'),
                  dataIndex: 'purpose',
                  render: (value: string) => <Tag>{value}</Tag>,
                },
                {
                  title: t('instanceDetail.capability'),
                  dataIndex: 'capability',
                  render: (value: string) => <Tag>{value}</Tag>,
                },
                {
                  title: t('auditLogs.database'),
                  dataIndex: 'database_name',
                  render: (value: string | null) => value || t('instanceDetail.allDatabases'),
                },
                {
                  title: t('instances.status'),
                  dataIndex: 'status',
                  render: (value: string) => (
                    <Tag color={statusColor(value)}>{value}</Tag>
                  ),
                },
                {
                  title: t('instances.actions'),
                  render: (_: unknown, item: InstanceCredential) => (
                    <Space align="start" wrap>
                      <CredentialReveal
                        targetKey={`${item.id}:${item.version}:${item.status}`}
                        disabled={item.status !== 'active'}
                        reveal={(request) =>
                          revealCredential(item.id, request).then(
                            (response) => response.data,
                          )
                        }
                      />
                      <Button
                        disabled={
                          item.status !== 'active' ||
                          item.purpose === 'resource_access'
                        }
                        aria-label={`Edit ${item.name}`}
                        onClick={() => {
                          if (item.purpose === 'resource_access') return
                          setEditingCredential(item)
                          setCredentialPurpose(item.purpose)
                          setCredentialTestResult(null)
                          credentialForm.setFieldsValue({
                            name: item.name,
                            purpose: item.purpose,
                            capability: item.capability,
                            include_database: !!item.database_name,
                            database_name: item.database_name ?? undefined,
                            username: '',
                            password: '',
                          })
                          setCredentialFormOpen(true)
                        }}
                      >
                        {t('instanceDetail.edit')}
                      </Button>
                      <Button
                        danger
                        disabled={item.status !== 'active'}
                        aria-label={`${t('instanceDetail.revoke')} ${item.name}`}
                        onClick={() =>
                          setConfirmation({
                            kind: 'revoke',
                            credential: item,
                          })
                        }
                      >
                        {t('instanceDetail.revoke')}
                      </Button>
                    </Space>
                  ),
                },
              ]}
            />
          )}
        </section>

        <section aria-labelledby="backend-heading">
          <Space
            align="start"
            style={{ width: '100%', justifyContent: 'space-between' }}
            wrap
          >
            <div>
              <Title id="backend-heading" level={4}>
                {t('instanceDetail.provisioningBackend')}
              </Title>
              <Text type="secondary">
                {t('instanceDetail.provisioningBackendDescription')}
              </Text>
            </div>
            {canProvision && (
              <Button
                type={backend ? 'default' : 'primary'}
                disabled={
                  activeProvisioningCredentials.length === 0 || busy
                }
                onClick={openBackendForm}
              >
                {backend ? t('instanceDetail.editBackend') : t('instanceDetail.createBackend')}
              </Button>
            )}
          </Space>

          {!canProvision ? (
            <Alert
              type="info"
              showIcon
              message={t('instanceDetail.backendEligibility')}
              style={{ marginTop: 16 }}
            />
          ) : activeProvisioningCredentials.length === 0 ? (
            <Alert
              type="warning"
              showIcon
              message={t('instanceDetail.adminCredentialRequired')}
              style={{ marginTop: 16 }}
            />
          ) : null}

          {backendFormOpen && (
            <div
              style={{
                background: 'var(--surface-tertiary)',
                borderRadius: 'var(--radius-md)',
                padding: 16,
                marginTop: 16,
              }}
            >
              <Title level={5}>
                {backend ? t('instanceDetail.editBackend') : t('instanceDetail.createBackend')}
              </Title>
              <Form
                form={backendForm}
                layout="vertical"
                onFinish={(values) => void handleBackendSave(values)}
                style={{ maxWidth: 680 }}
              >
                <Form.Item name="instance_id" hidden>
                  <Input />
                </Form.Item>
                <Form.Item
                  name="admin_credential_id"
                  label={t('instanceDetail.provisioningCredential')}
                  rules={[{ required: true }]}
                >
                  <Select
                    options={activeProvisioningCredentials.map((item) => ({
                      value: item.id,
                      label: item.name,
                    }))}
                  />
                </Form.Item>
                <Form.Item name="priority" label={t('instanceDetail.priority')} rules={[{ required: true }]}>
                  <InputNumber precision={0} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item
                  name="max_active_resources"
                  label={t('instanceDetail.maxResources')}
                  rules={[{ required: true, type: 'number', min: 1 }]}
                >
                  <InputNumber min={1} precision={0} style={{ width: '100%' }} />
                </Form.Item>
                <Space size={16} wrap style={{ width: '100%' }}>
                  <Form.Item
                    name="resource_min_cpu"
                    label={t('instanceDetail.minCpu')}
                    rules={[{ required: true, type: 'number', min: 0 }]}
                  >
                    <InputNumber min={0} precision={0} />
                  </Form.Item>
                  <Form.Item
                    name="resource_max_cpu"
                    label={t('instanceDetail.maxCpu')}
                    dependencies={['resource_min_cpu']}
                    rules={[
                      { required: true, type: 'number', min: 1 },
                      ({ getFieldValue }) => ({
                        validator(_, value) {
                          if (
                            value === undefined ||
                            value >= getFieldValue('resource_min_cpu')
                          ) {
                            return Promise.resolve()
                          }
                          return Promise.reject(
                            new Error(
                              t('instanceDetail.cpuValidation'),
                            ),
                          )
                        },
                      }),
                    ]}
                  >
                    <InputNumber min={1} precision={0} />
                  </Form.Item>
                  <Form.Item
                    name="ddl_concurrency"
                    label={t('instanceDetail.ddlConcurrency')}
                    rules={[{ required: true, type: 'number', min: 1 }]}
                  >
                    <InputNumber min={1} precision={0} />
                  </Form.Item>
                </Space>
                <Space>
                  <Button type="primary" htmlType="submit" loading={busy}>
                    {t('instanceDetail.saveBackend')}
                  </Button>
                  <Button
                    disabled={busy}
                    onClick={() => setBackendFormOpen(false)}
                  >
                    {t('common.cancel')}
                  </Button>
                </Space>
              </Form>
            </div>
          )}

          {backend && (
            <div style={{ marginTop: 16 }}>
              <Descriptions bordered column={{ xs: 1, sm: 2 }} size="small">
                <Descriptions.Item label={t('instances.status')}>
                  <Tag color={statusColor(backend.status)}>{backend.status}</Tag>
                </Descriptions.Item>
                <Descriptions.Item label={t('instanceDetail.provisioningHealth')}>
                  <Space wrap>
                    <Tag
                      color={
                        instance.health?.healthy ? 'green' : 'red'
                      }
                    >
                      {instance.health?.healthy
                        ? t('instances.healthy')
                        : t('instances.unhealthy')}
                    </Tag>
                    {instance.health?.error_code && (
                      <Text type="secondary">
                        {instance.health.error_code}
                      </Text>
                    )}
                  </Space>
                </Descriptions.Item>
                <Descriptions.Item label={t('instanceDetail.configurationRevision')}>
                  {backend.config_revision}
                </Descriptions.Item>
                <Descriptions.Item label={t('instanceDetail.priority')}>
                  {backend.priority}
                </Descriptions.Item>
                <Descriptions.Item label={t('instanceDetail.capacity')}>
                  {backend.max_active_resources} {t('instanceDetail.activeResources')}
                </Descriptions.Item>
                <Descriptions.Item label={t('instanceDetail.cpuRange')}>
                  {backend.resource_min_cpu}–{backend.resource_max_cpu}
                </Descriptions.Item>
                <Descriptions.Item label={t('instanceDetail.ddlConcurrency')}>
                  {backend.ddl_concurrency}
                </Descriptions.Item>
              </Descriptions>
              <Space wrap style={{ marginTop: 12 }}>
                {backend.status === 'active' && (
                  <Button
                    aria-label={t('instanceDetail.drainBackend')}
                    onClick={() =>
                      setConfirmation({ kind: 'drain', backend })
                    }
                  >
                    {t('instanceDetail.drain')}
                  </Button>
                )}
                {backend.status !== 'disabled' && (
                  <Button
                    danger
                    aria-label={t('instanceDetail.disableBackend')}
                    onClick={() =>
                      setConfirmation({ kind: 'disable', backend })
                    }
                  >
                    {t('instanceDetail.disable')}
                  </Button>
                )}
                {backend.status !== 'active' && (
                  <Button
                    type="primary"
                    loading={busy}
                    onClick={() => void reactivateBackend()}
                  >
                    {t('instanceDetail.reactivate')}
                  </Button>
                )}
              </Space>
            </div>
          )}
        </section>
      </Space>

      <Modal
        title={
          confirmation?.kind === 'revoke'
            ? `Revoke ${confirmation.credential.name}?`
            : confirmation?.kind === 'drain'
              ? t('instanceDetail.drainTitle')
              : t('instanceDetail.disableTitle')
        }
        open={!!confirmation}
        okText={
          confirmation?.kind === 'revoke'
            ? t('instanceDetail.confirmRevoke')
            : confirmation?.kind === 'drain'
              ? t('instanceDetail.confirmDrain')
              : t('instanceDetail.confirmDisable')
        }
        okButtonProps={{
          danger:
            confirmation?.kind === 'revoke' ||
            confirmation?.kind === 'disable',
        }}
        confirmLoading={busy}
        onOk={() => void performConfirmation()}
        onCancel={() => {
          if (!busy) setConfirmation(null)
        }}
      >
        {confirmation?.kind === 'revoke' ? (
          <Text>
            {t('instanceDetail.revokeDescription')}
          </Text>
        ) : confirmation?.kind === 'drain' ? (
          <Text>
            {t('instanceDetail.drainDescription')}
          </Text>
        ) : (
          <Text>
            {t('instanceDetail.disableDescription')}
          </Text>
        )}
      </Modal>
    </PageContainer>
  )
}
