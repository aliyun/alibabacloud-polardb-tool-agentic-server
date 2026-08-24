import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'

import api, { getAPIErrorMessage } from '../../api/client'

const { Text } = Typography

type Provider = 'feishu' | 'sharepoint'

interface IdentitySource {
  id: string
  name: string
  provider: Provider
  cloud?: 'global' | 'china'
  tenant_id: string | null
  status: string
  last_synced_at: string | null
  last_error: string | null
  sync_supported: boolean
  space_bindings: string[]
}

interface SpaceOption {
  knowledge_space_id: string
  name: string
  identity_domain: string
}

interface FormValues {
  name: string
  provider: Provider
  tenant_id?: string
  app_id?: string
  app_secret?: string
  cloud?: 'global' | 'china'
  client_id?: string
  client_secret?: string
}

interface DirectoryData {
  users: Array<{ external_user_id: string; display_name: string; email: string | null }>
  groups: Array<{ external_group_id: string; display_name: string; principal_type: string }>
  userTotal: number
  groupTotal: number
}

const DIRECTORY_PAGE_SIZE = 20

export default function EnterpriseIdentitySourcesPanel() {
  const { t, i18n } = useTranslation()
  const [sources, setSources] = useState<IdentitySource[]>([])
  const [spaces, setSpaces] = useState<SpaceOption[]>([])
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)
  const [editingSource, setEditingSource] = useState<IdentitySource | null>(null)
  const [saving, setSaving] = useState(false)
  const [bindingSource, setBindingSource] = useState<IdentitySource | null>(null)
  const [bindingSpaceIds, setBindingSpaceIds] = useState<string[]>([])
  const [binding, setBinding] = useState(false)
  const [syncingSourceId, setSyncingSourceId] = useState<string | null>(null)
  const [directorySource, setDirectorySource] = useState<IdentitySource | null>(null)
  const [directoryData, setDirectoryData] = useState<DirectoryData | null>(null)
  const [directoryLoading, setDirectoryLoading] = useState(false)
  const [directorySearch, setDirectorySearch] = useState('')
  const [directoryUserOffset, setDirectoryUserOffset] = useState(0)
  const [directoryGroupOffset, setDirectoryGroupOffset] = useState(0)
  const [form] = Form.useForm<FormValues>()
  const provider = Form.useWatch('provider', form) ?? 'feishu'
  const labels = i18n.language.startsWith('zh')
    ? {
        guide: '接入指南',
        verificationFailed: '无法发起飞书租户验证。',
        feishuVerificationHint: '创建后将跳转到飞书完成租户验证，PAS 自动读取租户 ID。',
        sharepointSyncHint: '保存后，PAS 会使用 Microsoft Graph 同步 Entra 用户、用户组和成员关系。',
        verifyFeishu: '验证飞书租户',
        bindSpace: '绑定 Space',
        spaceBound: 'Space 已绑定。',
        bindFailed: '无法绑定 Space。',
        boundSpaces: '已绑定 Space',
        unbindSpace: '解绑',
        unbindConfirm: '解除该来源与此 Space 的绑定后，用户会立即不再携带此来源的主体访问该 Space。是否继续？',
        spaceUnbound: 'Space 已解绑。',
        unbindFailed: '无法解绑 Space。',
        selectSpace: '选择 Space',
        syncNow: '立即同步',
        syncSucceeded: '身份源同步完成。',
        syncFailed: '无法同步身份源。',
        edit: '编辑', delete: '删除', directory: '已同步主体',
        directoryTitle: '已同步用户和组', directoryLoadFailed: '无法读取已同步主体。',
        deleteConfirm: '删除此身份源将撤销目录数据、Space 和 Agent 组绑定；已创建的 PAS 用户不会删除。是否继续？',
      }
    : {
        guide: 'Integration guide',
        verificationFailed: 'Could not start Feishu tenant verification.',
        feishuVerificationHint: 'After creation, complete tenant verification in Feishu. PAS discovers the tenant ID automatically.',
        sharepointSyncHint: 'After saving, PAS synchronizes Entra users, groups, and memberships through Microsoft Graph.',
        verifyFeishu: 'Verify Feishu tenant',
        bindSpace: 'Bind Space',
        spaceBound: 'Space bound.',
        bindFailed: 'Could not bind Space.',
        boundSpaces: 'Bound Spaces',
        unbindSpace: 'Unbind',
        unbindConfirm: 'Unbinding this Space removes this source’s principals from users’ access to it immediately. Continue?',
        spaceUnbound: 'Space unbound.',
        unbindFailed: 'Could not unbind Space.',
        selectSpace: 'Select Space',
        syncNow: 'Sync now',
        syncSucceeded: 'Identity source synchronized.',
        syncFailed: 'Could not synchronize identity source.',
        edit: 'Edit', delete: 'Delete', directory: 'Synced identities',
        directoryTitle: 'Synced users and groups', directoryLoadFailed: 'Could not load synced identities.',
        deleteConfirm: 'Deleting this source revokes directory data, Space bindings, and Agent group assignments. Existing PAS users are kept. Continue?',
      }

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [sourceResponse, spaceResponse] = await Promise.all([
        api.get<{ items: IdentitySource[] }>('/api/identity-sources'),
        api.get<{ items: SpaceOption[] }>('/api/identity-sources/spaces'),
      ])
      setSources(sourceResponse.data.items)
      setSpaces(spaceResponse.data.items)
    } catch (error: unknown) {
      message.error(getAPIErrorMessage(error, t('users.identitySourcesLoadFailed')))
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => {
    void load()
  }, [load])

  const close = () => {
    setOpen(false)
    setEditingSource(null)
    form.resetFields()
  }

  const openEdit = (source: IdentitySource) => {
    setEditingSource(source)
    form.setFieldsValue({
      name: source.name,
      provider: source.provider,
      cloud: source.cloud ?? 'global',
    })
    setOpen(true)
  }

  const startFeishuVerification = async (source: IdentitySource) => {
    try {
      const response = await api.post<{ authorization_url: string }>(
        `/api/identity-sources/${encodeURIComponent(source.id)}/feishu-verification`,
      )
      window.location.assign(response.data.authorization_url)
    } catch (error: unknown) {
      message.error(getAPIErrorMessage(error, labels.verificationFailed))
    }
  }

  const save = async (values: FormValues) => {
    setSaving(true)
    try {
      const payload = {
        name: values.name,
        ...(values.provider === 'feishu'
          ? { app_id: values.app_id, app_secret: values.app_secret }
          : {
              tenant_id: values.tenant_id,
              cloud: values.cloud,
              client_id: values.client_id,
              client_secret: values.client_secret,
            }),
      }
      const saved = editingSource
        ? await api.put<IdentitySource>(`/api/identity-sources/${encodeURIComponent(editingSource.id)}`, payload)
        : await api.post<IdentitySource>('/api/identity-sources', { provider: values.provider, ...payload })
      close()
      if (saved.data.provider === 'feishu') {
        await startFeishuVerification(saved.data)
        return
      }
      message.success(t('users.identitySourceSaved'))
      await load()
    } catch (error: unknown) {
      message.error(getAPIErrorMessage(error, t('users.identitySourceSaveFailed')))
    } finally {
      setSaving(false)
    }
  }

  const bindSpace = async () => {
    if (!bindingSource || bindingSpaceIds.length === 0) return
    setBinding(true)
    try {
      await Promise.all(
        bindingSpaceIds.map((knowledgeSpaceId) =>
          api.post(
            `/api/identity-sources/${encodeURIComponent(bindingSource.id)}/spaces/${encodeURIComponent(knowledgeSpaceId)}`,
          ),
        ),
      )
      message.success(labels.spaceBound)
      setBindingSource(null)
      setBindingSpaceIds([])
      await load()
    } catch (error: unknown) {
      message.error(getAPIErrorMessage(error, labels.bindFailed))
    } finally {
      setBinding(false)
    }
  }

  const syncNow = async (source: IdentitySource) => {
    setSyncingSourceId(source.id)
    try {
      await api.post(
        `/api/identity-sources/${encodeURIComponent(source.id)}/sync`,
      )
      message.success(labels.syncSucceeded)
      await load()
    } catch (error: unknown) {
      message.error(getAPIErrorMessage(error, labels.syncFailed))
    } finally {
      setSyncingSourceId(null)
    }
  }

  const loadDirectory = async (
    source: IdentitySource,
    userOffset = 0,
    groupOffset = 0,
    search = directorySearch,
  ) => {
    setDirectoryLoading(true)
    try {
      const params = new URLSearchParams({ limit: String(DIRECTORY_PAGE_SIZE) })
      if (search.trim()) params.set('search', search.trim())
      const baseUrl = `/api/identity-sources/${encodeURIComponent(source.id)}/directory`
      const [users, groups] = await Promise.all([
        api.get<{ users: DirectoryData['users']; total: number }>(
          `${baseUrl}?entry_type=users&offset=${userOffset}&${params.toString()}`,
        ),
        api.get<{ groups: DirectoryData['groups']; total: number }>(
          `${baseUrl}?entry_type=groups&offset=${groupOffset}&${params.toString()}`,
        ),
      ])
      setDirectoryData({
        users: users.data.users ?? [],
        groups: groups.data.groups ?? [],
        userTotal: users.data.total ?? 0,
        groupTotal: groups.data.total ?? 0,
      })
      setDirectoryUserOffset(userOffset)
      setDirectoryGroupOffset(groupOffset)
    } catch (error: unknown) {
      message.error(getAPIErrorMessage(error, labels.directoryLoadFailed))
    } finally {
      setDirectoryLoading(false)
    }
  }

  const showDirectory = async (source: IdentitySource) => {
    setDirectorySource(source)
    setDirectoryData(null)
    setDirectorySearch('')
    await loadDirectory(source, 0, 0, '')
  }

  const removeSource = (source: IdentitySource) => {
    Modal.confirm({
      title: labels.delete,
      content: labels.deleteConfirm,
      okType: 'danger',
      onOk: async () => {
        await api.delete(`/api/identity-sources/${encodeURIComponent(source.id)}`)
        await load()
      },
    })
  }

  const unbindSpace = (source: IdentitySource, space: SpaceOption) => {
    Modal.confirm({
      title: labels.unbindSpace,
      content: labels.unbindConfirm,
      okType: 'danger',
      onOk: async () => {
        try {
          await api.delete(
            `/api/identity-sources/${encodeURIComponent(source.id)}/spaces/${encodeURIComponent(space.knowledge_space_id)}`,
          )
          setBindingSource((current) => current && current.id === source.id
            ? {
                ...current,
                space_bindings: current.space_bindings.filter(
                  (knowledgeSpaceId) => knowledgeSpaceId !== space.knowledge_space_id,
                ),
              }
            : current)
          message.success(labels.spaceUnbound)
          await load()
        } catch (error: unknown) {
          message.error(getAPIErrorMessage(error, labels.unbindFailed))
          throw error
        }
      },
    })
  }

  const boundSpaces = useMemo(
    () => spaces.filter((space) => bindingSource?.space_bindings.includes(space.knowledge_space_id)),
    [bindingSource, spaces],
  )
  const availableSpaces = useMemo(
    () => spaces.filter((space) => !bindingSource?.space_bindings.includes(space.knowledge_space_id)),
    [bindingSource, spaces],
  )

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message={(
          <Space size={4}>
            <span>{t('users.identitySourcesDescription')}</span>
            <Typography.Link
              href={`/help/enterprise-identity-sources?locale=${encodeURIComponent(i18n.language)}`}
              target="_blank"
            >
              {labels.guide}
            </Typography.Link>
          </Space>
        )}
      />
      <div style={{ textAlign: 'right' }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
          {t('users.addIdentitySource')}
        </Button>
      </div>
      <Table
        rowKey="id"
        loading={loading}
        pagination={false}
        dataSource={sources}
        columns={[
          { title: t('users.identitySourceName'), dataIndex: 'name' },
          { title: t('users.identityProvider'), dataIndex: 'provider' },
          {
            title: t('users.identityTenant'),
            render: (_value, source: IdentitySource) => source.tenant_id ?? '—',
          },
          {
            title: t('users.identitySourceStatus'),
            render: (_value, source: IdentitySource) => (
              <Space direction="vertical" size={2}>
                <Tag color={source.sync_supported ? 'blue' : 'default'}>
                  {source.status}
                </Tag>
                {!source.sync_supported && (
                  <Text type="secondary">
                    {t('users.identitySourcePendingSync')}
                  </Text>
                )}
              </Space>
            ),
          },
          {
            title: t('users.identitySourceSpaces'),
            render: (_value, source: IdentitySource) => <Text>{source.space_bindings.length}</Text>,
          },
          {
            title: t('users.actions'),
            render: (_value, source: IdentitySource) => (
              <Space>
                <Button size="small" onClick={() => void showDirectory(source)}>
                  {labels.directory}
                </Button>
                {source.sync_supported && source.tenant_id && (
                  <Button
                    size="small"
                    loading={syncingSourceId === source.id}
                    onClick={() => void syncNow(source)}
                  >
                    {labels.syncNow}
                  </Button>
                )}
                <Button size="small" onClick={() => openEdit(source)}>
                  {labels.edit}
                </Button>
                {source.provider === 'feishu' && source.status === 'pending_tenant_verification' && (
                  <Button size="small" onClick={() => void startFeishuVerification(source)}>
                    {labels.verifyFeishu}
                  </Button>
                )}
                <Button size="small" onClick={() => setBindingSource(source)}>
                  {labels.bindSpace}
                </Button>
                <Button danger size="small" onClick={() => void removeSource(source)}>
                  {labels.delete}
                </Button>
              </Space>
            ),
          },
        ]}
      />
      <Modal
        title={editingSource ? labels.edit : t('users.addIdentitySource')}
        open={open}
        onCancel={close}
        onOk={() => form.submit()}
        okText={editingSource ? t('users.saveAccess') : t('users.create')}
        confirmLoading={saving}
      >
        <Form<FormValues>
          form={form}
          layout="vertical"
          initialValues={{ provider: 'feishu', cloud: 'global' }}
          onFinish={save}
        >
          <Form.Item name="name" label={t('users.identitySourceName')} rules={[{ required: true, whitespace: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="provider" label={t('users.identityProvider')} rules={[{ required: true }]}>
            <Select disabled={editingSource !== null} options={[
              { value: 'feishu', label: t('users.feishu') },
              { value: 'sharepoint', label: t('users.sharepoint') },
            ]} />
          </Form.Item>
          {provider === 'feishu' ? (
            <>
              <Alert type="info" showIcon message={labels.feishuVerificationHint} />
              <Form.Item name="app_id" label={t('users.identityAppId')} rules={[{ required: true, whitespace: true }]}>
                <Input autoComplete="off" />
              </Form.Item>
              <Form.Item name="app_secret" label={t('users.identityAppSecret')} rules={[{ required: true, whitespace: true }]}>
                <Input.Password autoComplete="new-password" />
              </Form.Item>
            </>
          ) : (
            <>
              <Alert type="info" showIcon message={labels.sharepointSyncHint} />
              <Form.Item name="cloud" label={i18n.language.startsWith('zh') ? '云环境' : 'Cloud'} rules={[{ required: true }]}>
                <Select options={[
                  { value: 'global', label: i18n.language.startsWith('zh') ? '全球云' : 'Global cloud' },
                  { value: 'china', label: i18n.language.startsWith('zh') ? '中国云（世纪互联）' : 'China cloud (21Vianet)' },
                ]} />
              </Form.Item>
              <Form.Item name="tenant_id" label={t('users.identityTenant')} rules={[{ required: true, whitespace: true }]}>
                <Input />
              </Form.Item>
              <Form.Item name="client_id" label={t('users.identityClientId')} rules={[{ required: true, whitespace: true }]}>
                <Input autoComplete="off" />
              </Form.Item>
              <Form.Item name="client_secret" label={t('users.identityClientSecret')} rules={[{ required: true, whitespace: true }]}>
                <Input.Password autoComplete="new-password" />
              </Form.Item>
            </>
          )}
        </Form>
      </Modal>
      <Modal
        title={`${labels.directoryTitle}${directorySource ? ` · ${directorySource.name}` : ''}`}
        open={directorySource !== null}
        footer={null}
        onCancel={() => {
          setDirectorySource(null)
          setDirectoryData(null)
        }}
        width={900}
      >
        <Input.Search
          allowClear
          placeholder={t('users.search')}
          value={directorySearch}
          onChange={(event) => setDirectorySearch(event.target.value)}
          onSearch={(value) => directorySource && void loadDirectory(directorySource, 0, 0, value)}
          style={{ marginBottom: 16 }}
        />
        <Typography.Title level={5}>{i18n.language.startsWith('zh') ? '用户' : 'Users'}</Typography.Title>
        <Table
          rowKey="external_user_id"
          loading={directoryLoading}
          pagination={{
            current: Math.floor(directoryUserOffset / DIRECTORY_PAGE_SIZE) + 1,
            pageSize: DIRECTORY_PAGE_SIZE,
            total: directoryData?.userTotal ?? 0,
            showSizeChanger: false,
            onChange: (page) => directorySource && void loadDirectory(
              directorySource,
              (page - 1) * DIRECTORY_PAGE_SIZE,
              directoryGroupOffset,
            ),
          }}
          dataSource={directoryData?.users ?? []}
          columns={[
            { title: t('users.identitySourceName'), dataIndex: 'display_name' },
            { title: 'ID', dataIndex: 'external_user_id' },
            { title: 'Email', dataIndex: 'email', render: (value) => value ?? '—' },
          ]}
        />
        <Typography.Title level={5} style={{ marginTop: 20 }}>{i18n.language.startsWith('zh') ? '组' : 'Groups'}</Typography.Title>
        <Table
          rowKey="external_group_id"
          loading={directoryLoading}
          pagination={{
            current: Math.floor(directoryGroupOffset / DIRECTORY_PAGE_SIZE) + 1,
            pageSize: DIRECTORY_PAGE_SIZE,
            total: directoryData?.groupTotal ?? 0,
            showSizeChanger: false,
            onChange: (page) => directorySource && void loadDirectory(
              directorySource,
              directoryUserOffset,
              (page - 1) * DIRECTORY_PAGE_SIZE,
            ),
          }}
          dataSource={directoryData?.groups ?? []}
          columns={[
            { title: t('users.identitySourceName'), dataIndex: 'display_name' },
            { title: 'ID', dataIndex: 'external_group_id' },
            { title: t('users.identityProvider'), dataIndex: 'principal_type' },
          ]}
        />
      </Modal>
      <Modal
        title={labels.bindSpace}
        open={bindingSource !== null}
        onCancel={() => {
          setBindingSource(null)
          setBindingSpaceIds([])
        }}
        onOk={() => void bindSpace()}
        okButtonProps={{ disabled: bindingSpaceIds.length === 0 }}
        confirmLoading={binding}
      >
        {boundSpaces.length > 0 && (
          <Space direction="vertical" size={8} style={{ width: '100%', marginBottom: 16 }}>
            <Text strong>{labels.boundSpaces}</Text>
            {boundSpaces.map((space) => (
              <Space key={space.knowledge_space_id} style={{ justifyContent: 'space-between', width: '100%' }}>
                <Text>{space.name} · {space.identity_domain}</Text>
                <Button danger size="small" onClick={() => bindingSource && unbindSpace(bindingSource, space)}>
                  {labels.unbindSpace}
                </Button>
              </Space>
            ))}
          </Space>
        )}
        <Select
          style={{ width: '100%' }}
          mode="multiple"
          value={bindingSpaceIds}
          placeholder={labels.selectSpace}
          onChange={setBindingSpaceIds}
          options={availableSpaces.map((space) => ({
            value: space.knowledge_space_id,
            label: `${space.name} · ${space.identity_domain}`,
          }))}
        />
      </Modal>
    </Space>
  )
}
