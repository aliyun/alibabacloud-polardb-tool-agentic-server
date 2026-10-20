import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Checkbox,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import { CopyOutlined, PlusOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'

import EtlPermissionsPanel from './EtlPermissionsPanel'
import api, { getAPIErrorMessage } from '../../api/client'
import { formatDateTime } from '../../i18n/format'
import { copyText } from '../../utils/clipboard'

const { Text } = Typography

type Provider = 'feishu' | 'sharepoint' | 'dingtalk'

interface IdentitySource {
  id: string
  name: string
  provider: Provider
  cloud?: 'global' | 'china'
  tenant_id: string | null
  status: string
  last_synced_at: string | null
  last_error: string | null
  full_sync_in_progress: boolean
  group_sync_in_progress?: boolean
  last_group_synced_at?: string | null
  last_group_error?: string | null
  sync_warning: {
    code: string
    skipped_count?: number
    provider_errors?: Record<string, number>
  } | null
  stale_after_seconds: number
  sync_supported: boolean
  event_subscription?: {
    status: 'connecting' | 'connected_unconfirmed' | 'active' | 'error'
    fallback_after_seconds: number
  } | null
  space_bindings: string[]
  acl_membership_snapshot_configured?: boolean
  acl_membership_snapshot?: Omit<SnapshotFormValues, 'password'> | null
}

interface SnapshotFormValues {
  dialect: 'mysql' | 'postgresql'
  host: string
  port: number
  database: string
  username: string
  password?: string
}

interface SpaceOption {
  knowledge_space_id: string
  name: string
  identity_domain: string
  is_bound?: boolean
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
  stale_after_seconds: number
  bind_all_spaces: boolean
}

interface DirectoryData {
  users: Array<{ external_user_id: string; display_name: string; email: string | null }>
  groups: Array<{ external_group_id: string; display_name: string; principal_type: string }>
  userTotal: number
  groupTotal: number
}

const DIRECTORY_PAGE_SIZE = 20
const SPACE_PAGE_SIZE = 20
type SpaceBindingStatus = 'unbound' | 'bound'

export default function EnterpriseIdentitySourcesPanel() {
  const { t, i18n } = useTranslation()
  const [sources, setSources] = useState<IdentitySource[]>([])
  const [sourceTotal, setSourceTotal] = useState(0)
  const [sourcePage, setSourcePage] = useState(1)
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)
  const [editingSource, setEditingSource] = useState<IdentitySource | null>(null)
  const [saving, setSaving] = useState(false)
  const [bindingSource, setBindingSource] = useState<IdentitySource | null>(null)
  const [bindingSpaceIds, setBindingSpaceIds] = useState<string[]>([])
  const [binding, setBinding] = useState(false)
  const [bindingStatus, setBindingStatus] = useState<SpaceBindingStatus>('unbound')
  const [bindingSpaces, setBindingSpaces] = useState<SpaceOption[]>([])
  const [bindingSpacePage, setBindingSpacePage] = useState(1)
  const [bindingSpaceTotal, setBindingSpaceTotal] = useState(0)
  const [bindingSpaceSearch, setBindingSpaceSearch] = useState('')
  const [bindingSpacesLoading, setBindingSpacesLoading] = useState(false)
  const [etlSource, setEtlSource] = useState<IdentitySource | null>(null)
  const [syncingSourceId, setSyncingSourceId] = useState<string | null>(null)
  const [directorySource, setDirectorySource] = useState<IdentitySource | null>(null)
  const [directoryData, setDirectoryData] = useState<DirectoryData | null>(null)
  const [directoryLoading, setDirectoryLoading] = useState(false)
  const [directorySearch, setDirectorySearch] = useState('')
  const [directoryUserOffset, setDirectoryUserOffset] = useState(0)
  const [directoryGroupOffset, setDirectoryGroupOffset] = useState(0)
  const [form] = Form.useForm<FormValues>()
  const [snapshotForm] = Form.useForm<SnapshotFormValues>()
  const [snapshotSource, setSnapshotSource] = useState<IdentitySource | null>(null)
  const [snapshotSaving, setSnapshotSaving] = useState(false)
  const provider = Form.useWatch('provider', form) ?? 'feishu'
  const labels = i18n.language.startsWith('zh')
    ? {
        guide: '接入指南',
        verificationFailed: '无法发起飞书租户验证。',
        feishuVerificationHint: '创建后将跳转到飞书完成租户验证，PAS 自动读取租户 ID。',
        sharepointSyncHint: '保存后，PAS 会使用 Microsoft Graph 同步 Entra 用户、用户组和成员关系。',
        verifyFeishu: '验证飞书租户',
        bindSpace: '绑定 Space',
        bindAllSpaces: '绑定所有 Space（包括以后新增）',
        bindAllSpacesHint: '创建后绑定当前所有 Space，并自动绑定以后注册到 PAS 的 Space。',
        availableSpaces: '可绑定',
        spaceBound: 'Space 已绑定。',
        bindFailed: '无法绑定 Space。',
        boundSpaces: '已绑定 Space',
        selectAllCurrentPage: '全选当前页',
        clearCurrentPage: '取消全选',
        bindSelected: '绑定所选',
        unbindSelected: '解绑所选',
        unbindSpace: '解绑',
        unbindConfirm: '解除该来源与此 Space 的绑定后，用户会立即不再携带此来源的主体访问该 Space。是否继续？',
        spaceUnbound: 'Space 已解绑。',
        unbindFailed: '无法解绑 Space。',
        selectSpace: '选择 Space',
        syncNow: '同步用户和用户组',
        syncSucceeded: '身份源同步完成。',
        syncStarted: '身份源正在后台同步。',
        fullSyncStarted: '全量同步中；已接收的增量事件处理完成后会开始全量对账。',
        fullSyncInProgress: '全量同步中',
        groupSyncInProgress: '用户组与知识权限对账中',
        groupSyncFailed: '最近一次用户组与知识权限对账失败：',
        syncFailed: '无法同步身份源。',
        partialWarning: '{{count}} 个用户组成员同步失败，已保留原授权。',
        eventSync: '增量订阅',
        eventConnecting: '正在连接',
        eventUnconfirmed: '订阅正常',
        eventActive: '订阅正常',
        eventError: '订阅异常',
        eventErrorHint: '用户组与知识权限仍按周期对账；可手动发起全量对账，或等待自动全量对账。',
        lastFullSync: '上次全量：',
        neverSynced: '未执行',
        identitySourceId: 'ID',
        copyIdentitySourceId: '复制身份源 ID',
        identitySourceIdCopied: '身份源 ID 已复制。',
        identitySourceIdCopyFailed: '无法复制身份源 ID。',
        edit: '编辑', delete: '删除', directory: '已同步主体',
        etlDatabase: '权限数据源', databaseType: '数据库类型', databaseHost: '地址', databasePort: '端口',
        databaseName: '数据库名', databaseUser: '用户名', databasePassword: '密码', saveDatabase: '保存',
        keepPassword: '留空保留已保存的密码', databaseSaved: '权限数据源已保存。请绑定知识库所在空间，再通过“同步知识库权限”验证连接并更新用户的访问权限。',
        databaseFailed: '无法保存权限数据源。',
        databaseHint: '配置保存飞书或钉钉知识权限数据的数据库连接，并使用只读账号读取。保存后，请绑定知识库所在空间，再同步知识库权限。开启自动绑定的 Agent 会在其已配置的知识库范围内更新用户访问权限。',
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
        bindAllSpaces: 'Bind all Spaces, including future Spaces',
        bindAllSpacesHint: 'Bind every current Space and automatically bind Spaces registered with PAS later.',
        availableSpaces: 'Available',
        spaceBound: 'Space bound.',
        bindFailed: 'Could not bind Space.',
        boundSpaces: 'Bound Spaces',
        selectAllCurrentPage: 'Select current page',
        clearCurrentPage: 'Clear selection',
        bindSelected: 'Bind selected',
        unbindSelected: 'Unbind selected',
        unbindSpace: 'Unbind',
        unbindConfirm: 'Unbinding this Space removes this source’s principals from users’ access to it immediately. Continue?',
        spaceUnbound: 'Space unbound.',
        unbindFailed: 'Could not unbind Space.',
        selectSpace: 'Select Space',
        syncNow: 'Sync users and groups',
        syncSucceeded: 'Identity source synchronized.',
        syncStarted: 'Identity source sync continues in the background.',
        fullSyncStarted: 'Full sync in progress. Reconciliation will start after received incremental events finish processing.',
        fullSyncInProgress: 'Full sync in progress',
        groupSyncInProgress: 'Group and knowledge permission reconciliation in progress',
        groupSyncFailed: 'Latest group and knowledge permission reconciliation failed: ',
        syncFailed: 'Could not synchronize identity source.',
        partialWarning: '{{count}} user groups failed membership sync; previous access was preserved.',
        eventSync: 'Incremental subscription',
        eventConnecting: 'Connecting',
        eventUnconfirmed: 'Subscription healthy',
        eventActive: 'Subscription healthy',
        eventError: 'Subscription error',
        eventErrorHint: 'Groups and knowledge permissions still reconcile periodically. Start a full reconciliation manually, or wait for automatic full reconciliation.',
        lastFullSync: 'Last full sync: ',
        neverSynced: 'Never',
        identitySourceId: 'ID',
        copyIdentitySourceId: 'Copy identity source ID',
        identitySourceIdCopied: 'Identity source ID copied.',
        identitySourceIdCopyFailed: 'Could not copy identity source ID.',
        edit: 'Edit', delete: 'Delete', directory: 'Synced identities',
        etlDatabase: 'Permission data source', databaseType: 'Database type', databaseHost: 'Host', databasePort: 'Port',
        databaseName: 'Database', databaseUser: 'Username', databasePassword: 'Password', saveDatabase: 'Save',
        keepPassword: 'Leave blank to keep the saved password', databaseSaved: 'Permission data source saved. Bind the knowledge base Space, then verify the connection and update access permissions with Sync knowledge permissions.',
        databaseFailed: 'Could not save the permission data source.',
        databaseHint: 'Connect to the database containing Feishu or DingTalk knowledge permission data using a read-only account. After saving, bind the knowledge base Space and sync knowledge permissions. Agents with auto-binding enabled update user access within their configured knowledge base scope.',
        directoryTitle: 'Synced users and groups', directoryLoadFailed: 'Could not load synced identities.',
        deleteConfirm: 'Deleting this source revokes directory data, Space bindings, and Agent group assignments. Existing PAS users are kept. Continue?',
      }

  const load = useCallback(async (nextPage = sourcePage) => {
    setLoading(true)
    try {
      const sourceResponse = await api.get<{ items: IdentitySource[]; total: number }>('/api/identity-sources', {
        params: { offset: (nextPage - 1) * 20, limit: 20 },
      })
      setSources(sourceResponse.data.items)
      setSourceTotal(sourceResponse.data.total)
      setSourcePage(nextPage)
    } catch (error: unknown) {
      message.error(getAPIErrorMessage(error, t('users.identitySourcesLoadFailed')))
    } finally {
      setLoading(false)
    }
  }, [sourcePage, t])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (!sources.some(source => source.full_sync_in_progress || source.group_sync_in_progress)) return
    const timer = window.setInterval(() => void load(), 5000)
    return () => window.clearInterval(timer)
  }, [sources, load])

  const loadSpaces = async (
    source: IdentitySource,
    status: SpaceBindingStatus,
    page = 1,
    search = '',
  ) => {
    setBindingSpacesLoading(true)
    try {
      const response = await api.get<{
        items: SpaceOption[]
        total: number
      }>('/api/identity-sources/spaces', {
        params: {
          identity_source_id: source.id,
          binding_status: status,
          offset: (page - 1) * SPACE_PAGE_SIZE,
          limit: SPACE_PAGE_SIZE,
          search: search || undefined,
        },
      })
      setBindingSpaces(response.data.items)
      setBindingSpaceTotal(response.data.total)
      setBindingSpacePage(page)
    } catch (error: unknown) {
      message.error(getAPIErrorMessage(error, t('users.identitySourcesLoadFailed')))
    } finally {
      setBindingSpacesLoading(false)
    }
  }

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
      stale_after_seconds: source.stale_after_seconds,
    })
    setOpen(true)
  }

  const copyIdentitySourceId = async (sourceId: string) => {
    try {
      await copyText(sourceId)
      message.success(labels.identitySourceIdCopied)
    } catch {
      message.error(labels.identitySourceIdCopyFailed)
    }
  }

  const startFeishuVerification = async (source: IdentitySource) => {
    try {
      const response = await api.post<{ authorization_url: string }>(
        `/api/identity-sources/${encodeURIComponent(source.id)}/${source.provider}-verification`,
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
        stale_after_seconds: values.stale_after_seconds,
        ...(values.provider === 'feishu'
          ? { app_id: values.app_id, app_secret: values.app_secret }
          : {
              ...(!editingSource ? { tenant_id: values.tenant_id } : {}),
              ...(values.provider === 'sharepoint' ? { cloud: values.cloud } : {}),
              client_id: values.client_id,
              client_secret: values.client_secret,
            }),
      }
      const saved = editingSource
        ? await api.put<IdentitySource>(`/api/identity-sources/${encodeURIComponent(editingSource.id)}`, payload)
        : await api.post<IdentitySource>('/api/identity-sources', {
            provider: values.provider,
            bind_all_spaces: values.bind_all_spaces,
            ...payload,
          })
      close()
      if (['feishu', 'dingtalk'].includes(saved.data.provider)) {
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
      setBindingSpaceIds([])
      await Promise.all([
        load(),
        loadSpaces(bindingSource, bindingStatus, 1, bindingSpaceSearch),
      ])
    } catch (error: unknown) {
      message.error(getAPIErrorMessage(error, labels.bindFailed))
    } finally {
      setBinding(false)
    }
  }

  const syncNow = async (source: IdentitySource) => {
    setSyncingSourceId(source.id)
    try {
      const result = await api.post<IdentitySource>(
        `/api/identity-sources/${encodeURIComponent(source.id)}/sync`,
      )
      message.success(result.status === 202
        ? source.provider === 'feishu' ? labels.fullSyncStarted : labels.syncStarted
        : labels.syncSucceeded)
      await load()
    } catch (error: unknown) {
      message.error(getAPIErrorMessage(error, labels.syncFailed))
    } finally {
      setSyncingSourceId(null)
    }
  }

  const showSnapshotDatabase = (source: IdentitySource) => {
    snapshotForm.resetFields()
    snapshotForm.setFieldsValue(source.acl_membership_snapshot ?? {
      dialect: 'mysql', host: '', port: 3306, database: 'polar_rag_meta', username: '',
    })
    setSnapshotSource(source)
  }

  const closeSnapshotDatabase = () => {
    setSnapshotSource(null)
    snapshotForm.resetFields()
  }

  const saveSnapshotDatabase = async (values: SnapshotFormValues) => {
    if (!snapshotSource) return
    setSnapshotSaving(true)
    try {
      const { password, ...connection } = values
      const payload = password?.trim() ? { ...connection, password: password.trim() } : connection
      await api.put(`/api/identity-sources/${encodeURIComponent(snapshotSource.id)}/acl-membership-snapshot`, payload)
      message.success(labels.databaseSaved)
      closeSnapshotDatabase()
      await load()
    } catch (error: unknown) {
      message.error(getAPIErrorMessage(error, labels.databaseFailed))
    } finally {
      setSnapshotSaving(false)
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

  const unbindSpaces = (source: IdentitySource) => {
    Modal.confirm({
      title: labels.unbindSpace,
      content: labels.unbindConfirm,
      okType: 'danger',
      onOk: async () => {
        setBinding(true)
        try {
          await Promise.all(
            bindingSpaceIds.map((knowledgeSpaceId) => api.delete(
              `/api/identity-sources/${encodeURIComponent(source.id)}/spaces/${encodeURIComponent(knowledgeSpaceId)}`,
            )),
          )
          setBindingSpaceIds([])
          message.success(labels.spaceUnbound)
          await Promise.all([
            load(),
            loadSpaces(source, bindingStatus, 1, bindingSpaceSearch),
          ])
        } catch (error: unknown) {
          message.error(getAPIErrorMessage(error, labels.unbindFailed))
          throw error
        } finally {
          setBinding(false)
        }
      },
    })
  }
  const allCurrentSpacesSelected = useMemo(
    () => bindingSpaces.length > 0
      && bindingSpaces.every((space) => bindingSpaceIds.includes(space.knowledge_space_id)),
    [bindingSpaceIds, bindingSpaces],
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
        pagination={{
          current: sourcePage,
          pageSize: 20,
          total: sourceTotal,
          showSizeChanger: false,
          onChange: (page) => void load(page),
        }}
        dataSource={sources}
        columns={[
          {
            title: t('users.identitySourceName'),
            dataIndex: 'name',
            render: (_value, source: IdentitySource) => (
              <Space direction="vertical" size={0}>
                <Text>{source.name}</Text>
                <Space size={4}>
                  <Text type="secondary" code>
                    {`${labels.identitySourceId}: ${source.id}`}
                  </Text>
                  <Tooltip title={labels.copyIdentitySourceId}>
                    <Button
                      type="text"
                      size="small"
                      icon={<CopyOutlined />}
                      aria-label={labels.copyIdentitySourceId}
                      onClick={() => void copyIdentitySourceId(source.id)}
                    />
                  </Tooltip>
                </Space>
              </Space>
            ),
          },
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
                {source.sync_warning?.code === 'MEMBERSHIPS_PARTIAL' && (
                  <Text type="warning">
                    {labels.partialWarning.replace(
                      '{{count}}',
                      String(source.sync_warning.skipped_count ?? 0),
                    )}
                  </Text>
                )}
                {source.provider === 'feishu' && source.full_sync_in_progress && (
                  <Text type="secondary">{labels.fullSyncInProgress}</Text>
                )}
                {source.provider === 'feishu' && source.group_sync_in_progress && (
                  <Text type="secondary">{labels.groupSyncInProgress}</Text>
                )}
                {source.provider === 'feishu' && source.last_group_error && (
                  <Text type="warning">{labels.groupSyncFailed}{source.last_group_error}</Text>
                )}
              </Space>
            ),
          },
          {
            title: labels.eventSync,
            render: (_value, source: IdentitySource) => {
              const subscription = source.event_subscription
              if (!['feishu', 'dingtalk'].includes(source.provider) || !subscription) return <Text type="secondary">—</Text>
              const statusLabels = {
                connecting: labels.eventConnecting,
                connected_unconfirmed: labels.eventUnconfirmed,
                active: labels.eventActive,
                error: labels.eventError,
              }
              const color = subscription.status === 'active' || subscription.status === 'connected_unconfirmed'
                ? 'green'
                : subscription.status === 'error'
                  ? 'red'
                  : 'blue'
              return (
                <Space direction="vertical" size={2}>
                  <Tag color={color}>{statusLabels[subscription.status]}</Tag>
                  {subscription.status === 'error' && (
                    <Text type="warning">{labels.eventErrorHint}</Text>
                  )}
                  <Text type="secondary">
                    {labels.lastFullSync}{source.last_synced_at
                      ? formatDateTime(source.last_synced_at, i18n.resolvedLanguage ?? i18n.language)
                      : labels.neverSynced}
                  </Text>
                </Space>
              )
            },
          },
          {
            title: t('users.identitySourceSpaces'),
            render: (_value, source: IdentitySource) => <Text>{source.space_bindings.length}</Text>,
          },
          {
            title: t('users.actions'),
            render: (_value, source: IdentitySource) => (
              <Space wrap size={[4, 4]} style={{ maxWidth: 460 }}>
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
                {['feishu', 'dingtalk'].includes(source.provider) && source.status === 'pending_tenant_verification' && (
                  <Button size="small" onClick={() => void startFeishuVerification(source)}>
                    {source.provider === 'dingtalk' ? (i18n.language.startsWith('zh') ? '验证钉钉企业' : 'Verify DingTalk enterprise') : labels.verifyFeishu}
                  </Button>
                )}
                <Button size="small" onClick={() => {
                  setBindingSource(source)
                  setBindingStatus('unbound')
                  setBindingSpaceIds([])
                  setBindingSpaceSearch('')
                  void loadSpaces(source, 'unbound')
                }}>
                  {labels.bindSpace}
                </Button>
                {['feishu', 'dingtalk'].includes(source.provider) && source.tenant_id && (
                  <Space><Button size="small" onClick={() => showSnapshotDatabase(source)}>{labels.etlDatabase}</Button>{source.acl_membership_snapshot_configured && <Button size="small" onClick={() => setEtlSource(source)}>{i18n.language.startsWith('zh') ? '同步知识库权限' : 'Sync knowledge permissions'}</Button>}</Space>
                )}
                <Button danger size="small" onClick={() => void removeSource(source)}>
                  {labels.delete}
                </Button>
              </Space>
            ),
          },
        ]}
      />
      <Modal title={i18n.language.startsWith('zh') ? '同步知识库权限' : 'Sync knowledge permissions'} open={!!etlSource} onCancel={() => setEtlSource(null)} footer={null} destroyOnClose>
        {etlSource && <EtlPermissionsPanel key={etlSource.id} sourceId={etlSource.id} />}
      </Modal>
      <Modal
        title={i18n.language.startsWith('zh') ? '知识库权限数据源' : 'Knowledge permission data source'}
        open={snapshotSource !== null}
        onCancel={closeSnapshotDatabase}
        onOk={() => snapshotForm.submit()}
        okText={labels.saveDatabase}
        confirmLoading={snapshotSaving}
      >
        <Alert type="info" showIcon message={labels.databaseHint} />
        <Form<SnapshotFormValues> form={snapshotForm} layout="vertical" onFinish={saveSnapshotDatabase}>
          <Form.Item name="dialect" label={labels.databaseType} rules={[{ required: true }]}>
            <Select options={[{ value: 'mysql', label: 'MySQL' }, { value: 'postgresql', label: 'PostgreSQL' }]}
              onChange={(dialect: SnapshotFormValues['dialect']) => {
                const port = snapshotForm.getFieldValue('port')
                if (!port || port === 3306 || port === 5432) snapshotForm.setFieldValue('port', dialect === 'postgresql' ? 5432 : 3306)
              }} />
          </Form.Item>
          <Form.Item name="host" label={labels.databaseHost} rules={[{ required: true, whitespace: true }]}><Input autoComplete="off" /></Form.Item>
          <Form.Item name="port" label={labels.databasePort} rules={[{ required: true }]}><InputNumber min={1} max={65535} /></Form.Item>
          <Form.Item name="database" label={labels.databaseName} rules={[{ required: true, whitespace: true }]}><Input autoComplete="off" /></Form.Item>
          <Form.Item name="username" label={labels.databaseUser} rules={[{ required: true, whitespace: true }]}><Input autoComplete="off" /></Form.Item>
          <Form.Item name="password" label={labels.databasePassword} rules={[{ required: !snapshotSource?.acl_membership_snapshot_configured, whitespace: true }]}>
            <Input.Password autoComplete="new-password" placeholder={snapshotSource?.acl_membership_snapshot_configured ? labels.keepPassword : undefined} />
          </Form.Item>
        </Form>
      </Modal>
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
          initialValues={{
            provider: 'feishu',
            cloud: 'global',
            stale_after_seconds: 7 * 24 * 60 * 60,
            bind_all_spaces: true,
          }}
          onFinish={save}
        >
          <Form.Item name="name" label={t('users.identitySourceName')} rules={[{ required: true, whitespace: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="provider" label={t('users.identityProvider')} rules={[{ required: true }]}>
            <Select disabled={editingSource !== null} options={[
              { value: 'feishu', label: t('users.feishu') },
              { value: 'sharepoint', label: t('users.sharepoint') },
              { value: 'dingtalk', label: t('users.dingtalk') },
            ]} />
          </Form.Item>
          <Form.Item
            name="stale_after_seconds"
            label={i18n.language.startsWith('zh') ? 'STALE 宽限期（秒）' : 'STALE grace period (seconds)'}
            rules={[{ required: true }]}
          >
            <InputNumber min={60} max={provider === 'dingtalk' ? 900 : 30 * 24 * 60 * 60} style={{ width: '100%' }} />
          </Form.Item>
          {!editingSource && (
            <Form.Item name="bind_all_spaces" valuePropName="checked">
              <Checkbox>
                <Space direction="vertical" size={0}>
                  <Text strong>{labels.bindAllSpaces}</Text>
                  <Text type="secondary">{labels.bindAllSpacesHint}</Text>
                </Space>
              </Checkbox>
            </Form.Item>
          )}
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
              <Alert type="info" showIcon message={provider === 'dingtalk' ? (i18n.language.startsWith('zh') ? '使用企业内部应用的 AppKey/AppSecret 和 corpId，保存后验证企业。仅同步应用可见的员工与部门。' : 'Use an internal enterprise application AppKey/AppSecret and corpId, then verify the enterprise. Only application-visible employees and departments are synchronized.') : labels.sharepointSyncHint} />
              {provider === 'sharepoint' && <Form.Item name="cloud" label={i18n.language.startsWith('zh') ? '云环境' : 'Cloud'} rules={[{ required: true }]}>
                <Select options={[
                  { value: 'global', label: i18n.language.startsWith('zh') ? '全球云' : 'Global cloud' },
                  { value: 'china', label: i18n.language.startsWith('zh') ? '中国云（世纪互联）' : 'China cloud (21Vianet)' },
                ]} />
              </Form.Item>}
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
        width={900}
        footer={null}
        onCancel={() => {
          setBindingSource(null)
          setBindingSpaceIds([])
        }}
      >
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Input.Search
            allowClear
            placeholder={labels.selectSpace}
            value={bindingSpaceSearch}
            onChange={(event) => setBindingSpaceSearch(event.target.value)}
            onSearch={(value) => bindingSource && void loadSpaces(
              bindingSource,
              bindingStatus,
              1,
              value.trim(),
            )}
          />
          <Tabs
            activeKey={bindingStatus}
            onChange={(key) => {
              const nextStatus = key as SpaceBindingStatus
              setBindingStatus(nextStatus)
              setBindingSpaceIds([])
              if (bindingSource) void loadSpaces(bindingSource, nextStatus, 1, bindingSpaceSearch)
            }}
            items={[
              { key: 'unbound', label: labels.availableSpaces },
              { key: 'bound', label: labels.boundSpaces },
            ]}
          />
          <Table
            rowKey="knowledge_space_id"
            loading={bindingSpacesLoading}
            dataSource={bindingSpaces}
            rowSelection={{
              selectedRowKeys: bindingSpaceIds,
              onChange: (keys) => setBindingSpaceIds(keys.map(String)),
            }}
            columns={[
              { title: 'Space', dataIndex: 'name' },
              { title: i18n.language.startsWith('zh') ? '身份域' : 'Identity domain', dataIndex: 'identity_domain' },
            ]}
            pagination={{
              current: bindingSpacePage,
              pageSize: SPACE_PAGE_SIZE,
              total: bindingSpaceTotal,
              showSizeChanger: false,
              onChange: (page) => {
                setBindingSpaceIds([])
                if (bindingSource) void loadSpaces(
                  bindingSource,
                  bindingStatus,
                  page,
                  bindingSpaceSearch,
                )
              },
            }}
          />
          <Space style={{ justifyContent: 'flex-end', width: '100%' }}>
            <Button
              disabled={bindingSpaces.length === 0}
              onClick={() => setBindingSpaceIds(
                allCurrentSpacesSelected
                  ? []
                  : bindingSpaces.map((space) => space.knowledge_space_id),
              )}
            >
              {allCurrentSpacesSelected ? labels.clearCurrentPage : labels.selectAllCurrentPage}
            </Button>
            {bindingStatus === 'unbound' ? (
              <Button
                type="primary"
                disabled={bindingSpaceIds.length === 0}
                loading={binding}
                onClick={() => void bindSpace()}
              >
                {labels.bindSelected}
              </Button>
            ) : (
              <Button
                danger
                disabled={bindingSpaceIds.length === 0}
                loading={binding}
                onClick={() => bindingSource && unbindSpaces(bindingSource)}
              >
                {labels.unbindSelected}
              </Button>
            )}
          </Space>
        </Space>
      </Modal>
    </Space>
  )
}
