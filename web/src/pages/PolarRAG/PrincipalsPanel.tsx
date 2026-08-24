import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Empty,
  Form,
  Input,
  Modal,
  Select,
  Skeleton,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  DeleteOutlined,
  EditOutlined,
  LinkOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  TeamOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { useTranslation } from 'react-i18next'

import api, { getAPIErrorMessage } from '../../api/client'
import { formatDateTime } from '../../i18n/format'
import {
  createEnterprisePrincipal,
  deleteEnterprisePrincipal,
  listEnterprisePrincipals,
  updateEnterprisePrincipal,
  type CreateEnterprisePrincipalInput,
  type EnterprisePrincipal,
} from '../../api/polarrag'

const { Text } = Typography

interface PrincipalsPanelProps {
  userId: string
  userName: string
  userExternalId?: string
  allowManualMapping?: boolean
}

interface EnterpriseIdentity {
  id: string
  identity_source_id: string
  source_name: string
  provider: string
  external_user_id: string
  display_name: string
  email: string | null
  status: string
  mapping_mode: 'synced' | 'pas_managed'
  native_principal_id: string
  principals: Array<{ provider: string; type: string; id: string }>
}

interface DirectoryCandidate {
  identity_source_id: string
  source_name: string
  provider: string
  external_user_id: string
  display_name: string
}

interface MappingValues {
  identity_key: string
}

const DIRECTORY_CANDIDATE_LIMIT = 20

interface ManualPrincipalsPanelProps {
  userId: string
  userName: string
  userExternalId: string
}

export default function PrincipalsPanel({
  userId,
  userName,
  userExternalId,
  allowManualMapping = true,
}: PrincipalsPanelProps) {
  const { t } = useTranslation()
  const [identities, setIdentities] = useState<EnterpriseIdentity[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [candidates, setCandidates] = useState<DirectoryCandidate[]>([])
  const [candidatesLoading, setCandidatesLoading] = useState(false)
  const candidateRequestId = useRef(0)
  const [mappingOpen, setMappingOpen] = useState(false)
  const [editing, setEditing] = useState<EnterpriseIdentity | null>(null)
  const [saving, setSaving] = useState(false)
  const [form] = Form.useForm<MappingValues>()

  const labels = {
    bind: t('principals.bindEnterpriseIdentity'),
    synced: t('principals.syncedIdentity'),
    managed: t('principals.pasManagedIdentity'),
    source: t('principals.identitySource'),
    userId: t('principals.externalUserId'),
    principals: t('principals.resolvedPrincipals'),
    alias: t('principals.nativeAlias'),
  }

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const response = await api.get<{ items: EnterpriseIdentity[] }>(
        `/api/identity-sources/users/${encodeURIComponent(userId)}/identities`,
      )
      setIdentities(response.data.items ?? [])
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('principals.loadFailed')))
    } finally {
      setLoading(false)
    }
  }, [t, userId])

  useEffect(() => {
    void load()
  }, [load])

  const loadCandidates = async (search = '') => {
    const requestId = ++candidateRequestId.current
    setCandidatesLoading(true)
    try {
      const sourceResponse = await api.get<{ items: Array<{ id: string; name: string; provider: string }> }>(
        '/api/identity-sources',
      )
      const params = new URLSearchParams({ limit: String(DIRECTORY_CANDIDATE_LIMIT) })
      if (search.trim()) params.set('search', search.trim())
      const directories = await Promise.all(
        (sourceResponse.data.items ?? []).map(async (source) => {
          const response = await api.get<{ users: Array<{ external_user_id: string; display_name: string }> }>(
            `/api/identity-sources/${encodeURIComponent(source.id)}/directory?entry_type=users&offset=0&${params.toString()}`,
          )
          return {
            ...source,
            users: response.data.users ?? [],
          }
        }),
      )
      if (requestId === candidateRequestId.current) {
        setCandidates(
          directories.flatMap((source) =>
            source.users.map((user) => ({
              identity_source_id: source.id,
              source_name: source.name,
              provider: source.provider,
              external_user_id: user.external_user_id,
              display_name: user.display_name,
            })),
          ),
        )
      }
    } finally {
      if (requestId === candidateRequestId.current) {
        setCandidatesLoading(false)
      }
    }
  }

  const openMapping = async (identity: EnterpriseIdentity | null = null) => {
    if (!allowManualMapping && identity === null) return
    setError(null)
    setEditing(identity)
    try {
      await loadCandidates(identity?.external_user_id)
      form.setFieldsValue({
        identity_key: identity
          ? `${identity.identity_source_id}:${identity.external_user_id}`
          : undefined,
      })
      setMappingOpen(true)
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('principals.loadFailed')))
    }
  }

  const saveMapping = async ({ identity_key }: MappingValues) => {
    const candidate = candidates.find(
      (item) => `${item.identity_source_id}:${item.external_user_id}` === identity_key,
    )
    if (!candidate) return
    setSaving(true)
    try {
      const payload = {
        identity_source_id: candidate.identity_source_id,
        external_user_id: candidate.external_user_id,
      }
      if (editing) {
        await api.put(
          `/api/identity-sources/users/${encodeURIComponent(userId)}/identities/${encodeURIComponent(editing.id)}`,
          payload,
        )
      } else {
        await api.post(
          `/api/identity-sources/users/${encodeURIComponent(userId)}/identities`,
          payload,
        )
      }
      message.success(t('principals.identitySaved'))
      setMappingOpen(false)
      form.resetFields()
      await load()
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('principals.identitySaveFailed')))
    } finally {
      setSaving(false)
    }
  }

  const remove = (identity: EnterpriseIdentity) => {
    Modal.confirm({
      title: t('principals.deleteIdentityTitle'),
      content: t('principals.deleteIdentityDescription', { name: identity.display_name }),
      okText: t('principals.delete'),
      okButtonProps: { danger: true },
      onOk: async () => {
        await api.delete(
          `/api/identity-sources/users/${encodeURIComponent(userId)}/identities/${encodeURIComponent(identity.id)}`,
        )
        message.success(t('principals.identityDeleted'))
        await load()
      },
    })
  }

  return (
    <Space direction="vertical" size={20} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message={t('principals.identityTitle')}
        description={t('principals.identityDescription')}
      />
      {error && <Alert type="error" showIcon message={error} />}
      {loading ? (
        <Skeleton active paragraph={{ rows: 5 }} />
      ) : (
        <>
          <div className="polarrag-section-heading polarrag-identity-heading">
            <Space direction="vertical" size={0}>
              <Text strong>{userName}</Text>
              <Text type="secondary">
                {t('principals.identityCount', { count: identities.length })}
              </Text>
            </Space>
            {allowManualMapping && (
              <Button size="small" type="primary" icon={<LinkOutlined />} onClick={() => void openMapping()}>
                {labels.bind}
              </Button>
            )}
          </div>
          {identities.length === 0 ? (
            <Empty description={t('principals.emptyIdentities')} />
          ) : (
            <Table
              rowKey="id"
              dataSource={identities}
              pagination={false}
              columns={[
                {
                  title: labels.source,
                  render: (_: unknown, record: EnterpriseIdentity) => (
                    <Space direction="vertical" size={0}>
                      <Text strong>{record.source_name}</Text>
                      <Text type="secondary">{record.provider}</Text>
                    </Space>
                  ),
                },
                { title: labels.userId, dataIndex: 'external_user_id' },
                {
                  title: labels.principals,
                  render: (_: unknown, record: EnterpriseIdentity) => (
                    <Space wrap>
                      {record.principals.map((principal) => (
                        <Tag key={`${principal.type}:${principal.id}`}>
                          {principal.type}: {principal.id}
                        </Tag>
                      ))}
                    </Space>
                  ),
                },
                {
                  title: labels.alias,
                  render: (_: unknown, record: EnterpriseIdentity) => (
                    <Tag color="purple">
                      {t('principals.nativeAliasValue', {
                        principal: record.native_principal_id,
                      })}
                    </Tag>
                  ),
                },
                {
                  title: t('principals.source'),
                  render: (_: unknown, record: EnterpriseIdentity) => (
                    <Tag color={record.mapping_mode === 'synced' ? 'blue' : 'gold'}>
                      {record.mapping_mode === 'synced' ? labels.synced : labels.managed}
                    </Tag>
                  ),
                },
                {
                  title: t('principals.actions'),
                  render: (_: unknown, record: EnterpriseIdentity) =>
                    record.mapping_mode === 'pas_managed' ? (
                      <Space>
                        <Button size="small" icon={<EditOutlined />} onClick={() => void openMapping(record)}>
                          {t('principals.edit')}
                        </Button>
                        <Button size="small" danger icon={<DeleteOutlined />} onClick={() => remove(record)}>
                          {t('principals.delete')}
                        </Button>
                      </Space>
                    ) : <Text type="secondary">—</Text>,
                },
              ]}
            />
          )}
        </>
      )}
      {allowManualMapping && (
      <Modal
        title={editing ? t('principals.editEnterpriseIdentity') : labels.bind}
        open={mappingOpen}
        onCancel={() => {
          if (!saving) {
            setMappingOpen(false)
            form.resetFields()
          }
        }}
        onOk={() => form.submit()}
        confirmLoading={saving}
      >
        <Form form={form} layout="vertical" onFinish={(values) => void saveMapping(values)}>
          <Form.Item name="identity_key" label={labels.bind} rules={[{ required: true }]}>
            <Select
              showSearch
              filterOption={false}
              loading={candidatesLoading}
              onSearch={(value) => void loadCandidates(value)}
              options={candidates.map((candidate) => ({
                value: `${candidate.identity_source_id}:${candidate.external_user_id}`,
                label: `${candidate.source_name} · ${candidate.display_name} · ${candidate.external_user_id}`,
              }))}
            />
          </Form.Item>
        </Form>
      </Modal>
      )}
      {allowManualMapping && userExternalId && (
        <ManualPrincipalsPanel
          userId={userId}
          userName={userName}
          userExternalId={userExternalId}
        />
      )}
    </Space>
  )
}

function ManualPrincipalsPanel({
  userId,
  userName,
  userExternalId,
}: ManualPrincipalsPanelProps) {
  const { t, i18n } = useTranslation()
  const [principals, setPrincipals] = useState<EnterprisePrincipal[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [updatingId, setUpdatingId] = useState<string | null>(null)
  const [form] = Form.useForm<CreateEnterprisePrincipalInput>()
  const provider = Form.useWatch('provider', form)
  const nativePrincipal = provider === 'polarrag'

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const response = await listEnterprisePrincipals(userId)
      setPrincipals(response.data.items)
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('principals.loadFailed')))
    } finally {
      setLoading(false)
    }
  }, [t, userId])

  useEffect(() => {
    void load()
  }, [load])

  const addPrincipal = async (values: CreateEnterprisePrincipalInput) => {
    setSaving(true)
    try {
      await createEnterprisePrincipal(userId, {
        ...values,
        identity_domain: values.identity_domain.trim(),
        principal_id: nativePrincipal
          ? values.principal_id
          : values.principal_id.trim(),
        valid_until: values.valid_until || null,
      })
      message.success(t('principals.added'))
      setOpen(false)
      form.resetFields()
      await load()
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('principals.addFailed')))
    } finally {
      setSaving(false)
    }
  }

  const toggleStatus = async (principal: EnterprisePrincipal) => {
    const status = principal.status === 'active' ? 'disabled' : 'active'
    setUpdatingId(principal.id)
    try {
      await updateEnterprisePrincipal(userId, principal.id, { status })
      await load()
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('principals.updateFailed')))
    } finally {
      setUpdatingId(null)
    }
  }

  const removePrincipal = (principal: EnterprisePrincipal) => {
    Modal.confirm({
      title: t('principals.deleteTitle'),
      content: t('principals.removeDescription', {
        principalId: principal.principal_id,
        userName,
      }),
      okText: t('principals.delete'),
      okButtonProps: { danger: true },
      onOk: async () => {
        await deleteEnterprisePrincipal(userId, principal.id)
        message.success(t('principals.deleted'))
        await load()
      },
    })
  }

  return (
    <>
      {error && <Alert type="error" showIcon message={error} />}
      <div className="polarrag-section-heading">
        <Space direction="vertical" size={0}>
          <Text strong>{t('principals.pasManagedIdentity')}</Text>
          <Text type="secondary">
            {t('principals.assignmentCount', { count: principals.length })}
          </Text>
        </Space>
        <Button
          icon={<PlusOutlined />}
          onClick={() => {
            form.resetFields()
            setOpen(true)
          }}
        >
          {t('principals.addPrincipal')}
        </Button>
      </div>
      {loading ? (
        <Skeleton active paragraph={{ rows: 3 }} />
      ) : principals.length === 0 ? (
        <Empty description={t('principals.empty')} />
      ) : (
        <Table
          rowKey="id"
          dataSource={principals}
          pagination={false}
          columns={[
            {
              title: t('principals.principal'),
              render: (_: unknown, principal: EnterprisePrincipal) => (
                <Space>
                  {principal.principal_type === 'user' ? <UserOutlined /> : <TeamOutlined />}
                  <Space direction="vertical" size={0}>
                    <Text strong>{principal.principal_id}</Text>
                    <Text type="secondary">
                      {principal.provider} · {principal.principal_type}
                    </Text>
                  </Space>
                </Space>
              ),
            },
            {
              title: t('principals.identityDomain'),
              dataIndex: 'identity_domain',
              render: (value: string) => <Tag>{value}</Tag>,
            },
            {
              title: t('principals.status'),
              dataIndex: 'status',
              render: (value: EnterprisePrincipal['status']) => (
                <Tag color={value === 'active' ? 'green' : 'red'}>{value}</Tag>
              ),
            },
            {
              title: t('principals.validUntilColumn'),
              dataIndex: 'valid_until',
              render: (value: string | null) => value
                ? formatDateTime(value, i18n.resolvedLanguage ?? i18n.language)
                : t('principals.noExpiry'),
            },
            {
              title: t('principals.actions'),
              render: (_: unknown, principal: EnterprisePrincipal) => (
                <Space>
                  <Button
                    size="small"
                    loading={updatingId === principal.id}
                    icon={principal.status === 'active' ? <PauseCircleOutlined /> : <PlayCircleOutlined />}
                    onClick={() => void toggleStatus(principal)}
                  >
                    {principal.status === 'active' ? t('principals.disable') : t('principals.enable')}
                  </Button>
                  <Button size="small" danger icon={<DeleteOutlined />} onClick={() => removePrincipal(principal)}>
                    {t('principals.delete')}
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      )}
      <Modal
        title={t('principals.addTitle')}
        open={open}
        okText={t('principals.add')}
        confirmLoading={saving}
        onOk={() => form.submit()}
        onCancel={() => {
          if (!saving) {
            setOpen(false)
            form.resetFields()
          }
        }}
        destroyOnHidden
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={{ provider: 'feishu', principal_type: 'user', valid_until: null }}
          onFinish={(values) => void addPrincipal(values)}
        >
          <Form.Item name="identity_domain" label={t('principals.identityDomain')} rules={[{ required: true }]}>
            <Input placeholder={t('principals.domainPlaceholder')} />
          </Form.Item>
          <Space align="start" style={{ display: 'flex' }}>
            <Form.Item name="provider" label={t('principals.provider')} rules={[{ required: true }]} style={{ flex: 1 }}>
              <Select
                options={[
                  { value: 'feishu', label: 'Feishu' },
                  { value: 'sharepoint', label: 'SharePoint' },
                  { value: 'polarrag', label: 'PolarRAG' },
                ]}
                onChange={(value) => form.setFieldsValue(
                  value === 'polarrag'
                    ? { principal_type: 'user', principal_id: userExternalId }
                    : { principal_id: '' },
                )}
              />
            </Form.Item>
            <Form.Item name="principal_type" label={t('principals.principalType')} rules={[{ required: true }]} style={{ flex: 1 }}>
              <Select disabled={nativePrincipal} options={[{ value: 'user', label: 'User' }, { value: 'group', label: 'Group' }]} />
            </Form.Item>
          </Space>
          <Form.Item name="principal_id" label={t('principals.principalId')} rules={[{ required: true }]}>
            <Input disabled={nativePrincipal} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
