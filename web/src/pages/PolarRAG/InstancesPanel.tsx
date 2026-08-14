import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  Modal,
  Skeleton,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { useTranslation } from 'react-i18next'
import {
  ApiOutlined,
  CheckCircleOutlined,
  CloudSyncOutlined,
  CloudUploadOutlined,
  KeyOutlined,
  SafetyCertificateOutlined,
  StopOutlined,
} from '@ant-design/icons'

import { getAPIErrorMessage } from '../../api/client'
import {
  claimPolarRAGKnowledgeBase,
  checkPolarRAGInstance,
  configurePolarRAGSpaceOss,
  disablePolarRAGInstance,
  disablePolarRAGSpace,
  enablePolarRAGSpace,
  listPolarRAGInstances,
  listPolarRAGSpaces,
  listUnclaimedPolarRAGKnowledgeBases,
  syncPolarRAGSpace,
  updatePolarRAGInstance,
  type PolarRAGInstance,
  type PolarRAGOwnerCandidate,
  type PolarRAGSpace,
  type UnclaimedPolarRAGKnowledgeBase,
  type UpdatePolarRAGInstanceInput,
  type ConfigurePolarRAGOssInput,
} from '../../api/polarrag'
import './PolarRAG.css'

const { Text } = Typography

function statusColor(status: PolarRAGInstance['status']) {
  if (status === 'active') return 'green'
  if (status === 'error' || status === 'disabled') return 'red'
  if (status === 'capability_missing') return 'orange'
  return 'blue'
}

interface InstancesPanelProps {
  onRegister?: () => void
}

export default function InstancesPanel({
  onRegister,
}: InstancesPanelProps) {
  const { t } = useTranslation()
  const capabilityLabel = (value: boolean) => value ? (
    <Tag color="green">{t('polarragAdmin.ready')}</Tag>
  ) : (
    <Tag color="orange">{t('polarragAdmin.required')}</Tag>
  )
  const [instances, setInstances] = useState<PolarRAGInstance[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [rotateInstance, setRotateInstance] =
    useState<PolarRAGInstance | null>(null)
  const [rotating, setRotating] = useState(false)
  const [checkingId, setCheckingId] = useState<string | null>(null)
  const [spaceInstance, setSpaceInstance] =
    useState<PolarRAGInstance | null>(null)
  const [spaces, setSpaces] = useState<PolarRAGSpace[]>([])
  const [spacesLoading, setSpacesLoading] = useState(false)
  const [spaceError, setSpaceError] = useState<string | null>(null)
  const [spaceAction, setSpaceAction] = useState<string | null>(null)
  const [unclaimedKnowledgeBases, setUnclaimedKnowledgeBases] = useState<
    UnclaimedPolarRAGKnowledgeBase[]
  >([])
  const [ownerCandidates, setOwnerCandidates] = useState<
    PolarRAGOwnerCandidate[]
  >([])
  const [selectedOwners, setSelectedOwners] = useState<
    Record<string, string>
  >({})
  const [rotateForm] = Form.useForm<UpdatePolarRAGInstanceInput>()
  const [ossForm] = Form.useForm<ConfigurePolarRAGOssInput>()
  const [ossSpace, setOssSpace] = useState<PolarRAGSpace | null>(null)
  const [ossSaving, setOssSaving] = useState(false)

  const loadInstances = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const response = await listPolarRAGInstances()
      setInstances(response.data.items)
    } catch (requestError) {
      setError(
        getAPIErrorMessage(
          requestError,
          'Could not load PolarRAG instances.',
        ),
      )
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadInstances()
  }, [loadInstances])

  const blockedCount = instances.filter(
    (item) => item.status === 'capability_missing',
  ).length

  const check = async (instance: PolarRAGInstance) => {
    setCheckingId(instance.id)
    try {
      await checkPolarRAGInstance(instance.id)
      message.success(`Capability check finished for ${instance.name}`)
      await loadInstances()
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, 'PolarRAG capability check failed.'),
      )
    } finally {
      setCheckingId(null)
    }
  }

  const rotateCredentials = async (values: UpdatePolarRAGInstanceInput) => {
    if (!rotateInstance) return
    setRotating(true)
    setError(null)
    try {
      await updatePolarRAGInstance(rotateInstance.id, {
        username: values.username?.trim(),
        password: values.password,
        tls_verify: values.tls_verify,
        ca_bundle: values.ca_bundle?.trim() || null,
      })
      message.success(`Credentials rotated for ${rotateInstance.name}`)
      setRotateInstance(null)
      rotateForm.resetFields()
      await loadInstances()
    } catch (requestError) {
      setError(
        getAPIErrorMessage(
          requestError,
          'Could not rotate the PolarRAG credentials.',
        ),
      )
    } finally {
      setRotating(false)
    }
  }

  const disable = (instance: PolarRAGInstance) => {
    Modal.confirm({
      title: `Disable ${instance.name}?`,
      content:
        'The instance and all of its enabled Spaces will immediately disappear from user resource discovery.',
      okText: 'Disable',
      okButtonProps: { danger: true },
      async onOk() {
        await disablePolarRAGInstance(instance.id)
        message.success(`${instance.name} disabled`)
        await loadInstances()
      },
    })
  }

  const openSpaces = async (instance: PolarRAGInstance) => {
    setSpaceInstance(instance)
    setSpaces([])
    setUnclaimedKnowledgeBases([])
    setOwnerCandidates([])
    setSelectedOwners({})
    setSpaceError(null)
    setSpacesLoading(true)
    try {
      const [spaceResponse, pendingResponse] = await Promise.all([
        listPolarRAGSpaces(instance.id),
        listUnclaimedPolarRAGKnowledgeBases(instance.id),
      ])
      setSpaces(spaceResponse.data.items)
      setUnclaimedKnowledgeBases(pendingResponse.data.items)
      setOwnerCandidates(pendingResponse.data.owner_candidates)
    } catch (requestError) {
      setSpaceError(
        getAPIErrorMessage(
          requestError,
          'Could not enumerate trusted upstream Spaces.',
        ),
      )
    } finally {
      setSpacesLoading(false)
    }
  }

  const reloadSpaces = async () => {
    if (!spaceInstance) return
    const [spaceResponse, pendingResponse] = await Promise.all([
      listPolarRAGSpaces(spaceInstance.id),
      listUnclaimedPolarRAGKnowledgeBases(spaceInstance.id),
    ])
    setSpaces(spaceResponse.data.items)
    setUnclaimedKnowledgeBases(pendingResponse.data.items)
    setOwnerCandidates(pendingResponse.data.owner_candidates)
  }

  const enableSpace = async (space: PolarRAGSpace) => {
    if (!spaceInstance) return
    setSpaceAction(`enable:${space.space_id}`)
    try {
      const response = await enablePolarRAGSpace(
        spaceInstance.id,
        space.space_id,
      )
      message.success(
        `${space.name} enabled; ${response.data.sync.knowledge_bases} knowledge bases, ${response.data.sync.active} active resources`,
      )
      await reloadSpaces()
    } catch (requestError) {
      setSpaceError(
        getAPIErrorMessage(requestError, 'Could not enable this Space.'),
      )
    } finally {
      setSpaceAction(null)
    }
  }

  const disableSpace = (space: PolarRAGSpace) => {
    if (!spaceInstance) return
    Modal.confirm({
      title: `Disable ${space.name}?`,
      content:
        'Its knowledge resources will immediately disappear from user resource discovery.',
      okText: 'Disable',
      okButtonProps: { danger: true },
      async onOk() {
        setSpaceAction(`disable:${space.space_id}`)
        try {
          await disablePolarRAGSpace(
            spaceInstance.id,
            space.space_id,
          )
          message.success(`${space.name} disabled`)
          await reloadSpaces()
        } catch (requestError) {
          setSpaceError(
            getAPIErrorMessage(requestError, 'Could not disable this Space.'),
          )
        } finally {
          setSpaceAction(null)
        }
      },
    })
  }

  const syncSpace = async (space: PolarRAGSpace) => {
    if (!spaceInstance) return
    setSpaceAction(`sync:${space.space_id}`)
    try {
      const response = await syncPolarRAGSpace(
        spaceInstance.id,
        space.space_id,
      )
      message.success(
        `${space.name} synchronized; ${response.data.knowledge_bases} knowledge bases, ${response.data.active} active resources`,
      )
      await reloadSpaces()
    } catch (requestError) {
      setSpaceError(
        getAPIErrorMessage(
          requestError,
          'The Space must be enabled before it can be synchronized.',
        ),
      )
    } finally {
      setSpaceAction(null)
    }
  }

  const configureOss = async (values: ConfigurePolarRAGOssInput) => {
    if (!ossSpace?.knowledge_space_id) return
    setOssSaving(true)
    setSpaceError(null)
    try {
      await configurePolarRAGSpaceOss(ossSpace.knowledge_space_id, {
        access_key_id: values.access_key_id.trim(),
        access_key_secret: values.access_key_secret,
        object_prefix: values.object_prefix.trim(),
      })
      message.success(`OSS upload access validated for ${ossSpace.name}`)
      setOssSpace(null)
      ossForm.resetFields()
      await reloadSpaces()
    } catch (requestError) {
      setSpaceError(
        getAPIErrorMessage(
          requestError,
          'Could not validate the Space OSS configuration.',
        ),
      )
    } finally {
      setOssSaving(false)
    }
  }

  const claimKnowledgeBase = async (
    knowledgeBase: UnclaimedPolarRAGKnowledgeBase,
  ) => {
    if (!spaceInstance) return
    const key = `${knowledgeBase.space_id}:${knowledgeBase.kb_id}`
    const principalAssignmentId = selectedOwners[key]
    if (!principalAssignmentId) return
    setSpaceAction(`claim:${key}`)
    try {
      await claimPolarRAGKnowledgeBase(
        spaceInstance.id,
        knowledgeBase.space_id,
        knowledgeBase.kb_id,
        principalAssignmentId,
      )
      message.success(`${knowledgeBase.name} activated and synchronized`)
      setSelectedOwners((current) => {
        const next = { ...current }
        delete next[key]
        return next
      })
      await reloadSpaces()
    } catch (requestError) {
      setSpaceError(
        getAPIErrorMessage(
          requestError,
          'Could not assign the KB owner and activate it.',
        ),
      )
    } finally {
      setSpaceAction(null)
    }
  }

  const knowledgeResources = spaces.flatMap((space) =>
    space.knowledge_resources.map((resource) => ({
      ...resource,
      space_name: space.name,
    })),
  )

  return (
    <Space direction="vertical" size={20} style={{ width: '100%' }}>
      {error && <Alert type="error" showIcon message={error} />}
      {blockedCount > 0 && (
        <Alert
          type="warning"
          showIcon
          message={<code>POLARRAG_CATALOG_CAPABILITY_MISSING</code>}
          description={`${blockedCount} instance${blockedCount === 1 ? '' : 's'} cannot be enabled until PolarRAG provides trusted Space and KB catalog APIs with immutable identity_domain and structured PERSONAL owner.`}
        />
      )}

      {loading ? (
        <Skeleton active paragraph={{ rows: 5 }} />
      ) : instances.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            <Space direction="vertical" size={4}>
              <Text strong>{t('polarragAdmin.empty')}</Text>
              <Text type="secondary">
                {t('polarragAdmin.emptyDescription')}
              </Text>
            </Space>
          }
        >
          {onRegister && (
            <Button type="primary" onClick={onRegister}>
              {t('instances.register')}
            </Button>
          )}
        </Empty>
      ) : (
        <Table
          rowKey="id"
          dataSource={instances}
          pagination={false}
          scroll={{ x: 1180 }}
          expandable={{
            expandedRowRender: (instance) => (
              <Space direction="vertical" size={12} style={{ width: '100%' }}>
                {instance.status === 'capability_missing' && (
                  <Alert
                    type="warning"
                    showIcon
                    message={instance.last_error_code}
                    description={t('polarragAdmin.capabilityDescription')}
                  />
                )}
                <Descriptions size="small" column={3}>
                  <Descriptions.Item label={t('polarragAdmin.search')}>
                    {capabilityLabel(instance.capabilities?.search === true)}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('polarragAdmin.protectedReads')}>
                    {capabilityLabel(
                      instance.capabilities?.protected_document_info === true,
                    )}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('polarragAdmin.context')}>
                    {capabilityLabel(
                      instance.capabilities?.protected_context === true,
                    )}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('polarragAdmin.spaceCatalog')}>
                    {capabilityLabel(
                      instance.capabilities?.space_catalog === true,
                    )}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('polarragAdmin.kbCatalog')}>
                    {capabilityLabel(
                      instance.capabilities?.knowledge_base_catalog === true,
                    )}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('polarragAdmin.lastChecked')}>
                    {instance.last_checked_at
                      ? new Date(instance.last_checked_at).toLocaleString()
                      : 'Never'}
                  </Descriptions.Item>
                </Descriptions>
              </Space>
            ),
          }}
          columns={[
            {
              title: 'Instance',
              dataIndex: 'name',
              width: 260,
              render: (name: string, record) => (
                <Space direction="vertical" size={0}>
                  <Text strong>{name}</Text>
                  <Text type="secondary">
                    {record.scheme}://{record.host}:{record.port}
                  </Text>
                </Space>
              ),
            },
            {
              title: 'Status',
              dataIndex: 'status',
              width: 140,
              render: (status: PolarRAGInstance['status']) => (
                <Tag color={statusColor(status)}>{status}</Tag>
              ),
            },
            {
              title: 'Plugin',
              dataIndex: 'plugin_version',
              width: 180,
              render: (value: string | null) => value || 'Unknown',
            },
            {
              title: 'TLS',
              dataIndex: 'tls_verify',
              width: 250,
              render: (value: boolean) => (
                <Space size={4}>
                  <SafetyCertificateOutlined />
                  {value ? 'Verified' : 'Verification disabled'}
                </Space>
              ),
            },
            {
              title: 'Actions',
              width: 300,
              render: (_: unknown, record) => (
                <Space wrap>
                  <Button
                    size="small"
                    icon={<ApiOutlined />}
                    loading={checkingId === record.id}
                    onClick={() => void check(record)}
                  >
                    {t('polarragAdmin.check')}
                  </Button>
                  <Button
                    size="small"
                    icon={<CloudSyncOutlined />}
                    aria-label={`Manage Spaces for ${record.name}`}
                    onClick={() => void openSpaces(record)}
                  >
                    {t('polarragAdmin.spaces')}
                  </Button>
                  <Button
                    size="small"
                    icon={<KeyOutlined />}
                    aria-label={`Rotate credentials for ${record.name}`}
                    onClick={() => {
                      rotateForm.setFieldsValue({
                        username: undefined,
                        password: undefined,
                        tls_verify: record.tls_verify,
                        ca_bundle: null,
                      })
                      setRotateInstance(record)
                    }}
                  >
                    {t('polarragAdmin.credentials')}
                  </Button>
                  <Button
                    size="small"
                    danger
                    icon={<StopOutlined />}
                    disabled={record.status === 'disabled'}
                    onClick={() => disable(record)}
                  >
                    {t('users.disable')}
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      )}

      <Modal
        title={
          rotateInstance
            ? `Rotate Credentials · ${rotateInstance.name}`
            : 'Rotate Credentials'
        }
        open={rotateInstance !== null}
        okText={t('polarragAdmin.rotate')}
        confirmLoading={rotating}
        onOk={() => rotateForm.submit()}
        onCancel={() => {
          if (!rotating) {
            setRotateInstance(null)
            rotateForm.resetFields()
          }
        }}
        destroyOnHidden
      >
        <Alert
          type="warning"
          showIcon
          message={t('polarragAdmin.storedValuesHidden')}
          description={t('polarragAdmin.rotateDescription')}
          style={{ marginBottom: 20 }}
        />
        <Form
          form={rotateForm}
          layout="vertical"
          onFinish={(values) => void rotateCredentials(values)}
        >
          <Form.Item
            name="username"
            label={t('instances.username')}
            rules={[{ required: true }]}
          >
            <Input autoComplete="off" />
          </Form.Item>
          <Form.Item
            name="password"
            label={t('polarragAdmin.newPassword')}
            rules={[{ required: true }]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
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
          <Form.Item name="ca_bundle" label={t('polarragAdmin.replacementCa')}>
            <Input.TextArea
              rows={4}
              placeholder={t('instances.pemChain')}
              autoComplete="off"
            />
          </Form.Item>
        </Form>
      </Modal>

      <Drawer
        title={
          spaceInstance
            ? `Spaces · ${spaceInstance.name}`
            : 'PolarRAG Spaces'
        }
        width={960}
        open={spaceInstance !== null}
        onClose={() => setSpaceInstance(null)}
      >
        <Alert
          type="info"
          showIcon
          message={t('polarragAdmin.spaceIdentityTitle')}
          description={t('polarragAdmin.spaceIdentityDescription')}
          style={{ marginBottom: 20 }}
        />
        {spaceError && (
          <Alert
            type="error"
            showIcon
            message={spaceError}
            style={{ marginBottom: 20 }}
          />
        )}
        {spacesLoading ? (
          <Skeleton active paragraph={{ rows: 5 }} />
        ) : spaces.length === 0 && !spaceError ? (
          <Empty description={t('polarragAdmin.noSpaces')} />
        ) : (
          <Space direction="vertical" size={20} style={{ width: '100%' }}>
            <Table
              rowKey="space_id"
              dataSource={spaces}
              pagination={false}
              columns={[
              {
                title: 'Space',
                render: (_: unknown, record) => (
                  <Space direction="vertical" size={0}>
                    <Text strong>{record.name}</Text>
                    <Text type="secondary">{record.space_id}</Text>
                  </Space>
                ),
              },
              {
                title: 'Identity domain',
                dataIndex: 'identity_domain',
                render: (value: string) => <Tag>{value}</Tag>,
              },
              {
                title: 'Catalog',
                render: (_: unknown, record) => (
                  <Space direction="vertical" size={2}>
                    <Tag
                      color={record.status === 'ACTIVE' ? 'green' : 'orange'}
                    >
                      {record.status}
                    </Tag>
                    <Tag color={record.enabled ? 'blue' : 'default'}>
                      {record.enabled ? 'Enabled' : 'Not enabled'}
                    </Tag>
                  </Space>
                ),
              },
              {
                title: 'Synchronization',
                render: (_: unknown, record) => (
                  <Space direction="vertical" size={0}>
                    <Text>
                      {t('polarragAdmin.kbCount', { count: record.knowledge_resources.length })}
                    </Text>
                    <Text type="secondary">
                      {record.last_synced_at
                        ? new Date(record.last_synced_at).toLocaleString()
                        : 'Never synchronized'}
                    </Text>
                  </Space>
                ),
              },
              {
                title: 'Actions',
                render: (_: unknown, record) => (
                  <Space>
                    {record.enabled ? (
                      <>
                        <Button
                          size="small"
                          aria-label={`Sync ${record.name}`}
                          loading={spaceAction === `sync:${record.space_id}`}
                          onClick={() => void syncSpace(record)}
                        >
                          {t('polarragAdmin.sync')}
                        </Button>
                        <Button
                          size="small"
                          icon={<CloudUploadOutlined />}
                          aria-label={`Configure OSS for ${record.name}`}
                          disabled={
                            !record.knowledge_space_id ||
                            !record.oss_bucket ||
                            !record.oss_endpoint
                          }
                          title={record.oss_last_error_code ?? undefined}
                          onClick={() => setOssSpace(record)}
                        >
                          {!record.oss_bucket || !record.oss_endpoint
                            ? 'OSS catalog unavailable'
                            : record.oss_config_validated
                              ? 'OSS ready'
                              : 'OSS'}
                        </Button>
                        <Button
                          size="small"
                          danger
                          aria-label={`Disable ${record.name}`}
                          loading={
                            spaceAction === `disable:${record.space_id}`
                          }
                          onClick={() => disableSpace(record)}
                        >
                          {t('users.disable')}
                        </Button>
                      </>
                    ) : (
                      <Button
                        size="small"
                        type="primary"
                        aria-label={`Enable ${record.name}`}
                        loading={
                          spaceAction === `enable:${record.space_id}`
                        }
                        onClick={() => void enableSpace(record)}
                      >
                        {t('users.enable')}
                      </Button>
                    )}
                  </Space>
                ),
              },
              ]}
            />
            <Text strong>{t('polarragAdmin.awaitingOwner')}</Text>
            <Table
              rowKey={(record) => `${record.space_id}:${record.kb_id}`}
              dataSource={unclaimedKnowledgeBases}
              pagination={false}
              size="small"
              locale={{ emptyText: 'No knowledge bases awaiting owner' }}
              columns={[
                {
                  title: 'Knowledge base',
                  render: (_: unknown, record) => (
                    <Space direction="vertical" size={0}>
                      <Text strong>{record.name}</Text>
                      <Text type="secondary">{record.kb_id}</Text>
                    </Space>
                  ),
                },
                { title: 'Space', dataIndex: 'space_name' },
                {
                  title: 'Type',
                  dataIndex: 'kb_type',
                  render: (value: string) => <Tag>{value}</Tag>,
                },
                {
                  title: 'Owner',
                  render: (_: unknown, record) => {
                    const key = `${record.space_id}:${record.kb_id}`
                    const candidates = ownerCandidates.filter(
                      (candidate, index, all) =>
                        candidate.identity_domain === record.identity_domain
                        && all.findIndex(
                          (item) =>
                            item.identity_domain === candidate.identity_domain
                            && item.pas_user_id === candidate.pas_user_id,
                        ) === index,
                    )
                    return (
                      <Select
                        aria-label={`Owner for ${record.name}`}
                        value={selectedOwners[key]}
                        onChange={(value) =>
                          setSelectedOwners((current) => ({
                            ...current,
                            [key]: value,
                          }))
                        }
                        placeholder={t('polarragAccess.selectUser')}
                        showSearch
                        optionFilterProp="label"
                        options={candidates.map((candidate) => ({
                          value: candidate.principal_assignment_id,
                          label: `${candidate.user_name} · ${candidate.user_external_id}`,
                        }))}
                        style={{ minWidth: 260 }}
                        notFoundContent="No eligible user principal in this identity domain"
                      />
                    )
                  },
                },
                {
                  title: 'Actions',
                  render: (_: unknown, record) => {
                    const key = `${record.space_id}:${record.kb_id}`
                    return (
                      <Button
                        size="small"
                        type="primary"
                        aria-label={`Assign owner and activate ${record.name}`}
                        disabled={!selectedOwners[key]}
                        loading={spaceAction === `claim:${key}`}
                        onClick={() => void claimKnowledgeBase(record)}
                      >
                        {t('polarragAdmin.assignOwner')}
                      </Button>
                    )
                  },
                },
              ]}
            />
            <Text strong>{t('polarragAdmin.synchronizedKbs')}</Text>
            <Table
              rowKey="knowledge_resource_id"
              dataSource={knowledgeResources}
              pagination={{ pageSize: 10, showSizeChanger: true }}
              size="small"
              locale={{ emptyText: 'No synchronized knowledge bases' }}
              columns={[
                {
                  title: 'Knowledge base',
                  render: (_: unknown, record) => (
                    <Space direction="vertical" size={0}>
                      <Text strong>{record.name}</Text>
                      <Text type="secondary">
                        {record.knowledge_resource_id}
                      </Text>
                    </Space>
                  ),
                },
                { title: 'Space', dataIndex: 'space_name' },
                {
                  title: 'Type',
                  dataIndex: 'kb_type',
                  render: (value: string) => <Tag>{value}</Tag>,
                },
                {
                  title: 'Binding',
                  dataIndex: 'binding_mode',
                  render: (value: string | null) => value || 'Unsupported',
                },
                {
                  title: 'Status',
                  render: (_: unknown, record) => (
                    <Tag color={record.enabled ? 'green' : 'orange'}>
                      {record.sync_status}
                    </Tag>
                  ),
                },
              ]}
            />
          </Space>
        )}
        <Modal
        title={t('polarragAdmin.configureOss')}
        open={ossSpace !== null}
        okText={t('polarragAdmin.validateSave')}
        confirmLoading={ossSaving}
        onOk={() => ossForm.submit()}
        onCancel={() => {
          if (!ossSaving) {
            setOssSpace(null)
            ossForm.resetFields()
          }
        }}
        destroyOnHidden
      >
        <Alert
          type="info"
          showIcon
          message={t('polarragAdmin.ossTitle')}
          description={t('polarragAdmin.ossDescription')}
          style={{ marginBottom: 20 }}
        />
        <Form
          form={ossForm}
          layout="vertical"
          initialValues={{ object_prefix: 'pas/documents' }}
          onFinish={(values) => void configureOss(values)}
        >
          <Form.Item label={t('polarragAdmin.bucket')} htmlFor="polarrag-oss-bucket">
            <Input
              id="polarrag-oss-bucket"
              value={ossSpace?.oss_bucket ?? ''}
              readOnly
            />
          </Form.Item>
          <Form.Item label={t('polarragAdmin.endpoint')} htmlFor="polarrag-oss-endpoint">
            <Input
              id="polarrag-oss-endpoint"
              value={ossSpace?.oss_endpoint ?? ''}
              readOnly
            />
          </Form.Item>
          <Form.Item
            name="object_prefix"
            label={t('polarragAdmin.objectPrefix')}
            rules={[{ required: true }]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="access_key_id"
            label={t('polarragAdmin.accessKeyId')}
            rules={[{ required: true }]}
          >
            <Input autoComplete="off" />
          </Form.Item>
          <Form.Item
            name="access_key_secret"
            label={t('polarragAdmin.accessKeySecret')}
            rules={[{ required: true }]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
        </Form>
        </Modal>
      </Drawer>
    </Space>
  )
}
