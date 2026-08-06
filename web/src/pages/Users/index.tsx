import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Table,
  Input,
  Button,
  Tag,
  Space,
  message,
  Modal,
  Form,
  Select,
  Skeleton,
  Switch,
  Typography,
} from 'antd'
import { EditOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import api, { getAPIErrorMessage } from '../../api/client'
import {
  listInstanceCredentials,
  type InstanceCredential,
} from '../../api/credentials'
import {
  getUserInstanceAccess,
  listInstances,
  updateUserInstanceAccess,
  type DirectBindingCapability,
  type InstanceAccess,
  type InstanceSummary,
  type Permission,
  type SqlCapability,
} from '../../api/instanceAccess'
import CapabilityEditor from '../../components/CapabilityEditor'
import PageContainer from '../../components/PageContainer'

const { Text, Title } = Typography

interface UserItem {
  id: string
  external_id: string
  display_name: string
  email: string | null
  role: string
  status: string
  provisioning_mode: string | null
  departments: { id: string; name: string; is_primary: boolean }[]
}

interface DeptOption {
  id: string
  name: string
}

interface AccessDraft {
  credential_id: string
  permission: Permission
  capabilities: DirectBindingCapability[]
  enabled: boolean
}

export default function Users() {
  const { t } = useTranslation()
  const [users, setUsers] = useState<UserItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [authMode, setAuthMode] = useState('builtin')
  const [departments, setDepartments] = useState<DeptOption[]>([])
  const [editTarget, setEditTarget] = useState<UserItem | null>(null)
  const [editLoading, setEditLoading] = useState(false)
  const [editForm] = Form.useForm()
  const [resetTarget, setResetTarget] = useState<UserItem | null>(null)
  const [resetLoading, setResetLoading] = useState(false)
  const [resetForm] = Form.useForm()
  const accessGenerationRef = useRef(0)
  const accessSelectedRef = useRef<string | null>(null)
  const mountedRef = useRef(true)
  const [accessTarget, setAccessTarget] = useState<UserItem | null>(null)
  const [accessInstances, setAccessInstances] = useState<InstanceSummary[]>([])
  const [accessRows, setAccessRows] = useState<
    Record<string, InstanceAccess | null>
  >({})
  const [accessCredentials, setAccessCredentials] = useState<
    Record<string, InstanceCredential[]>
  >({})
  const [accessSelectedId, setAccessSelectedId] = useState<string | null>(null)
  const [accessDraft, setAccessDraft] = useState<AccessDraft | null>(null)
  const [accessLoading, setAccessLoading] = useState(false)
  const [accessDetailLoading, setAccessDetailLoading] = useState<
    Record<string, boolean>
  >({})
  const [accessSaving, setAccessSaving] = useState<Record<string, boolean>>({})
  const [accessDetailErrors, setAccessDetailErrors] = useState<
    Record<string, string>
  >({})
  const [accessError, setAccessError] = useState<string | null>(null)
  const [accessNotice, setAccessNotice] = useState<string | null>(null)

  const fetchUsers = useCallback(async () => {
    setLoading(true)
    try {
      const resp = await api.get('/api/users', {
        params: { search, offset: (page - 1) * 20, limit: 20 },
      })
      setUsers(resp.data.items)
      setTotal(resp.data.total)
    } finally {
      setLoading(false)
    }
  }, [page, search])

  useEffect(() => { void fetchUsers() }, [fetchUsers])
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      accessGenerationRef.current += 1
    }
  }, [])
  useEffect(() => {
    api.get('/auth/mode').then(r => setAuthMode(r.data.mode)).catch(() => {})
    api.get('/api/departments').then(r => setDepartments(r.data)).catch(() => {})
  }, [])

  const openEdit = (user: UserItem) => {
    setEditTarget(user)
    editForm.setFieldsValue({
      department_ids: user.departments.map(d => d.id),
      role: user.role,
      provisioning_mode: user.provisioning_mode || 'dedicated',
    })
  }

  const handleEdit = async (values: { department_ids: string[]; role: string; provisioning_mode: string }) => {
    if (!editTarget) return
    setEditLoading(true)
    try {
      await api.put(`/api/users/${editTarget.id}`, {
        department_ids: values.department_ids,
        role: values.role,
        provisioning_mode: values.provisioning_mode,
      })
      message.success(t('users.updated'))
      setEditTarget(null)
      fetchUsers()
    } catch (error: unknown) {
      message.error(getAPIErrorMessage(error, t('users.updateFailed')))
    } finally {
      setEditLoading(false)
    }
  }

  const toggleStatus = (user: UserItem) => {
    const action = user.status === 'active' ? 'disable' : 'enable'
    Modal.confirm({
      title: t('users.statusTitle', { action: action === 'disable' ? t('users.disable') : t('users.enable'), name: user.display_name }),
      onOk: async () => {
        await api.put(`/api/users/${user.id}/${action}`)
        message.success(t('users.statusChanged', { action: action === 'disable' ? t('users.disable') : t('users.enable') }))
        fetchUsers()
      },
    })
  }

  const handleResetPassword = async (values: { new_password: string }) => {
    if (!resetTarget) return
    setResetLoading(true)
    try {
      await api.put(`/api/users/${resetTarget.id}/reset-password`, values)
      message.success(t('users.passwordReset', { name: resetTarget.display_name }))
      setResetTarget(null)
      resetForm.resetFields()
    } catch (error: unknown) {
      message.error(getAPIErrorMessage(error, t('users.passwordResetFailed')))
    } finally {
      setResetLoading(false)
    }
  }

  const closeAccess = () => {
    accessGenerationRef.current += 1
    setAccessTarget(null)
    setAccessInstances([])
    setAccessRows({})
    setAccessCredentials({})
    setAccessSelectedId(null)
    accessSelectedRef.current = null
    setAccessDraft(null)
    setAccessLoading(false)
    setAccessDetailLoading({})
    setAccessSaving({})
    setAccessDetailErrors({})
    setAccessError(null)
    setAccessNotice(null)
  }

  const applyAccessDraft = useCallback(
    (instanceId: string, row: InstanceAccess | null) => {
      const toolCapabilities = (row?.capabilities ?? []).filter(
        (capability): capability is DirectBindingCapability =>
          capability.startsWith('db_instance:'),
      )
      accessSelectedRef.current = instanceId
      setAccessSelectedId(instanceId)
      setAccessDraft({
        credential_id: row?.credential_id ?? '',
        permission: row?.permission ?? 'readonly',
        capabilities: toolCapabilities,
        enabled: row?.enabled ?? true,
      })
      setAccessError(null)
      setAccessNotice(null)
    },
    [],
  )

  const selectAccessInstance = useCallback(
    async (
      instanceId: string,
      user: UserItem | null = accessTarget,
      generation = accessGenerationRef.current,
    ) => {
      if (!user) return
      accessSelectedRef.current = instanceId
      setAccessSelectedId(instanceId)
      setAccessError(null)
      setAccessNotice(null)
      if (Object.prototype.hasOwnProperty.call(accessRows, instanceId)) {
        applyAccessDraft(instanceId, accessRows[instanceId])
        return
      }
      setAccessDraft(null)
      setAccessDetailLoading((current) => ({
        ...current,
        [instanceId]: true,
      }))
      setAccessDetailErrors((current) => {
        const next = { ...current }
        delete next[instanceId]
        return next
      })
      try {
        const [accessResult, credentialsResult] = await Promise.allSettled([
          getUserInstanceAccess(user.id, instanceId),
          listInstanceCredentials(instanceId),
        ])
        if (
          accessResult.status === 'rejected' &&
          (
            accessResult.reason as { response?: { status?: number } }
          ).response?.status !== 404
        ) {
          throw accessResult.reason
        }
        if (credentialsResult.status === 'rejected') {
          throw credentialsResult.reason
        }
        if (
          !mountedRef.current ||
          accessGenerationRef.current !== generation
        ) {
          return
        }
        const row =
          accessResult.status === 'fulfilled' ? accessResult.value.data : null
        const credentials = credentialsResult.value.data.filter(
          (credential) =>
            credential.status === 'active' &&
            credential.purpose === 'direct_access' &&
            credential.capability !== 'admin',
        )
        setAccessRows((current) => ({ ...current, [instanceId]: row }))
        setAccessCredentials((current) => ({
          ...current,
          [instanceId]: credentials,
        }))
        if (accessSelectedRef.current === instanceId) {
          applyAccessDraft(instanceId, row)
        }
      } catch (requestError) {
        if (
          mountedRef.current &&
          accessGenerationRef.current === generation
        ) {
          setAccessDetailErrors((current) => ({
            ...current,
            [instanceId]: getAPIErrorMessage(
              requestError,
              t('users.accessLoadFailed'),
            ),
          }))
        }
      } finally {
        if (
          mountedRef.current &&
          accessGenerationRef.current === generation
        ) {
          setAccessDetailLoading((current) => {
            const next = { ...current }
            delete next[instanceId]
            return next
          })
        }
      }
    },
    [accessRows, accessTarget, applyAccessDraft, t],
  )

  const openAccess = async (user: UserItem) => {
    const generation = accessGenerationRef.current + 1
    accessGenerationRef.current = generation
    setAccessTarget(user)
    setAccessInstances([])
    setAccessRows({})
    setAccessCredentials({})
    setAccessSelectedId(null)
    accessSelectedRef.current = null
    setAccessDraft(null)
    setAccessLoading(true)
    setAccessDetailLoading({})
    setAccessSaving({})
    setAccessDetailErrors({})
    setAccessError(null)
    setAccessNotice(null)
    try {
      const instancesResponse = await listInstances()
      const eligible = instancesResponse.items.filter(
        (instance) =>
          instance.allocation_mode === 'registered' ||
          instance.owner_user_id === user.id,
      )
      if (
        !mountedRef.current ||
        accessGenerationRef.current !== generation
      ) {
        return
      }
      setAccessInstances(eligible)
      if (eligible.length > 0) {
        void selectAccessInstance(eligible[0].id, user, generation)
      }
    } catch (requestError) {
      if (
        !mountedRef.current ||
        accessGenerationRef.current !== generation
      ) {
        return
      }
      setAccessError(
        getAPIErrorMessage(
          requestError,
          t('users.userAccessLoadFailed'),
        ),
      )
    } finally {
      if (
        mountedRef.current &&
        accessGenerationRef.current === generation
      ) {
        setAccessLoading(false)
      }
    }
  }

  const saveAccess = async () => {
    if (!accessTarget || !accessSelectedId || !accessDraft) return
    const generation = accessGenerationRef.current
    const sqlCapabilities = (
      accessRows[accessSelectedId]?.capabilities ?? []
    ).filter(
      (capability): capability is SqlCapability =>
        capability === 'sql:read' || capability === 'sql:write',
    )
    const targetId = accessSelectedId
    setAccessSaving((current) => ({ ...current, [targetId]: true }))
    setAccessError(null)
    setAccessNotice(null)
    try {
      const response = await updateUserInstanceAccess(
        accessTarget.id,
        targetId,
        {
          credential_id: accessDraft.credential_id,
          permission: accessDraft.permission,
          capabilities: [...accessDraft.capabilities, ...sqlCapabilities],
          enabled: accessDraft.enabled,
        },
      )
      if (
        !mountedRef.current ||
        accessGenerationRef.current !== generation
      ) {
        return
      }
      setAccessRows((current) => ({
        ...current,
        [targetId]: response.data,
      }))
      if (accessSelectedRef.current === targetId) {
        setAccessNotice(
          t('users.accessUpdated'),
        )
      }
    } catch (requestError) {
      if (
        !mountedRef.current ||
        accessGenerationRef.current !== generation
      ) {
        return
      }
      if (accessSelectedRef.current === targetId) {
        setAccessError(
          getAPIErrorMessage(
            requestError,
            t('users.accessUpdateFailed'),
          ),
        )
      }
    } finally {
      if (
        mountedRef.current &&
        accessGenerationRef.current === generation
      ) {
        setAccessSaving((current) => {
          const next = { ...current }
          delete next[targetId]
          return next
        })
      }
    }
  }

  const selectedAccess = accessSelectedId
    ? accessRows[accessSelectedId]
    : null
  const selectedInstance = accessInstances.find(
    (instance) => instance.id === accessSelectedId,
  )
  const selectedCredentials = accessSelectedId
    ? accessCredentials[accessSelectedId] ?? []
    : []
  const selectedCredential = selectedCredentials.find(
    (credential) => credential.id === accessDraft?.credential_id,
  )
  const selectedBusy = accessSelectedId
    ? Boolean(accessSaving[accessSelectedId])
    : false
  const sqlCapabilities = useMemo(
    () =>
      (selectedAccess?.capabilities ?? []).filter(
        (capability): capability is SqlCapability =>
          capability === 'sql:read' || capability === 'sql:write',
      ),
    [selectedAccess],
  )

  const columns = [
    { title: t('users.name'), dataIndex: 'display_name', key: 'name' },
    { title: t('users.email'), dataIndex: 'email', key: 'email' },
    { title: t('users.role'), dataIndex: 'role', key: 'role', render: (role: string) => <Tag color={role === 'admin' ? 'red' : 'blue'}>{role}</Tag> },
    {
      title: t('users.departments'),
      key: 'departments',
      render: (_: unknown, record: UserItem) => (
        <Space>
          {record.departments.length === 0 && <Tag>{t('users.none')}</Tag>}
          {record.departments.map((d) => (
            <Tag key={d.id} color={d.is_primary ? 'green' : 'default'}>{d.name}</Tag>
          ))}
        </Space>
      ),
    },
    {
      title: t('users.provisioning'),
      dataIndex: 'provisioning_mode',
      key: 'provisioning_mode',
      render: (v: string | null) => <Tag>{v ?? 'dedicated'}</Tag>,
    },
    {
      title: t('users.status'),
      dataIndex: 'status',
      key: 'status',
      render: (status: string) => <Tag color={status === 'active' ? 'green' : 'red'}>{status}</Tag>,
    },
    {
      title: t('users.actions'),
      key: 'actions',
      render: (_: unknown, record: UserItem) => (
        <Space>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(record)}>
            {t('users.edit')}
          </Button>
          <Button
            size="small"
            aria-label={t('users.accessFor', { name: record.display_name })}
            onClick={() => void openAccess(record)}
          >
            {t('users.instanceAccess')}
          </Button>
          <Button size="small" onClick={() => toggleStatus(record)}>
            {record.status === 'active' ? t('users.disable') : t('users.enable')}
          </Button>
          {authMode === 'builtin' && (
            <Button size="small" onClick={() => setResetTarget(record)}>
              {t('users.resetPassword')}
            </Button>
          )}
        </Space>
      ),
    },
  ]

  return (
    <PageContainer
      title={t('users.title')}
      description={t('users.description')}
      actions={<Input.Search placeholder={t('users.search')} onSearch={setSearch} allowClear style={{ width: 280 }} />}
    >
      <Table
        dataSource={users}
        columns={columns}
        rowKey="id"
        loading={loading}
        pagination={{ total, pageSize: 20, current: page, onChange: setPage }}
      />

      {accessTarget && (
        <section
          aria-labelledby="user-instance-access-heading"
          style={{
            background: 'var(--surface-tertiary)',
            borderRadius: 'var(--radius-md)',
            padding: 16,
            marginTop: 20,
          }}
        >
          <Space
            align="start"
            style={{ width: '100%', justifyContent: 'space-between' }}
            wrap
          >
            <div>
              <Title
                id="user-instance-access-heading"
                level={4}
                style={{ marginTop: 0 }}
              >
                {t('users.accessTitle', { name: accessTarget.display_name })}
              </Title>
              <Text type="secondary">
                {t('users.accessDescription')}
              </Text>
            </div>
            <Button onClick={closeAccess}>{t('users.closeAccess')}</Button>
          </Space>

          {accessError && (
            <Alert
              type="error"
              showIcon
              role="alert"
              message={accessError}
              style={{ marginTop: 16 }}
            />
          )}
          {accessNotice && (
            <Alert
              type="info"
              showIcon
              role="status"
              message={accessNotice}
              closable
              style={{ marginTop: 16 }}
              onClose={() => setAccessNotice(null)}
            />
          )}

          {accessLoading ? (
            <Skeleton active paragraph={{ rows: 5 }} style={{ marginTop: 16 }} />
          ) : accessInstances.length === 0 ? (
            <Alert
              type="info"
              showIcon
              message={t('users.noEligible')}
              description={t('users.noEligibleDescription')}
              style={{ marginTop: 16 }}
            />
          ) : (
            <div
              style={{
                display: 'grid',
                gridTemplateColumns:
                  'repeat(auto-fit, minmax(min(100%, 300px), 1fr))',
                gap: 20,
                marginTop: 20,
              }}
            >
              <div aria-label={t('users.eligibleInstances')}>
                <Space direction="vertical" size={8} style={{ width: '100%' }}>
                  {accessInstances.map((instance) => {
                    const row = accessRows[instance.id]
                    const selected = instance.id === accessSelectedId
                    return (
                      <Button
                        key={instance.id}
                        type={selected ? 'primary' : 'default'}
                        block
                        aria-pressed={selected}
                        style={{ height: 'auto', paddingBlock: 10 }}
                        onClick={() => void selectAccessInstance(instance.id)}
                      >
                        <Space
                          direction="vertical"
                          size={0}
                          align="start"
                          style={{ width: '100%' }}
                        >
                          <span>{instance.name}</span>
                          <Text
                            type={selected ? undefined : 'secondary'}
                            style={
                              selected
                                ? { color: 'currentColor', opacity: 0.85 }
                                : undefined
                            }
                          >
                            {instance.allocation_mode} ·{' '}
                            {row?.origin ?? t('users.notGranted')}
                          </Text>
                        </Space>
                      </Button>
                    )
                  })}
                </Space>
              </div>

              {accessSelectedId && accessDetailErrors[accessSelectedId] && (
                <Alert
                  type="error"
                  showIcon
                  message={accessDetailErrors[accessSelectedId]}
                />
              )}
              {accessSelectedId && accessDetailLoading[accessSelectedId] ? (
                <Skeleton active paragraph={{ rows: 5 }} />
              ) : selectedInstance && accessDraft ? (
                <div>
                  <Space direction="vertical" size={16} style={{ width: '100%' }}>
                    <div>
                      <Text strong>{selectedInstance.name}</Text>
                      <br />
                      <Text type="secondary">
                        {selectedInstance.engine} · {selectedInstance.topology}{' '}
                        · {selectedInstance.cluster_id}
                      </Text>
                    </div>

                    <div>
                      <Text strong>{t('users.existingSql')}</Text>
                      <div style={{ marginTop: 6 }}>
                        {sqlCapabilities.length > 0 ? (
                          <Space wrap>
                            {sqlCapabilities.map((capability) => (
                              <Tag key={capability}>
                                {capability === 'sql:read'
                                  ? t('users.sqlRead')
                                  : t('users.sqlWrite')}
                              </Tag>
                            ))}
                            <Tag>{selectedAccess?.permission}</Tag>
                          </Space>
                        ) : (
                          <Text type="secondary">
                            {t('users.noSql')}
                          </Text>
                        )}
                      </div>
                    </div>

                    <div>
                      <Text strong>{t('users.toolCapabilities')}</Text>
                      <div style={{ marginTop: 10 }}>
                        <CapabilityEditor
                          value={accessDraft.capabilities}
                          onChange={(capabilities) =>
                            setAccessDraft((current) =>
                              current
                                ? { ...current, capabilities }
                                : current,
                            )
                          }
                          disabled={selectedBusy}
                        />
                      </div>
                    </div>

                    <label>
                      <Text strong>{t('users.directCredential')}</Text>
                      <Select
                        aria-label={t('users.directCredential')}
                        value={accessDraft.credential_id || undefined}
                        placeholder={t('users.selectCredential')}
                        disabled={selectedBusy}
                        options={selectedCredentials.map((credential) => ({
                          value: credential.id,
                          label: `${credential.name} · ${credential.capability}`,
                        }))}
                        onChange={(credentialId) => {
                          const credential = selectedCredentials.find(
                            (item) => item.id === credentialId,
                          )
                          setAccessDraft((current) =>
                            current
                              ? {
                                  ...current,
                                  credential_id: credentialId,
                                  permission:
                                    credential?.capability === 'readwrite'
                                      ? current.permission
                                      : 'readonly',
                                }
                              : current,
                          )
                        }}
                        style={{ width: '100%', marginTop: 6 }}
                      />
                    </label>

                    <label>
                      <Text strong>{t('users.permissionCeiling')}</Text>
                      <Select
                        aria-label={t('users.permissionCeiling')}
                        value={accessDraft.permission}
                        disabled={
                          selectedBusy || sqlCapabilities.length > 0
                        }
                        options={[
                          { value: 'readonly', label: t('users.readOnly') },
                          {
                            value: 'readwrite',
                            label: t('users.readWrite'),
                            disabled:
                              selectedCredential?.capability !== 'readwrite',
                          },
                        ]}
                        onChange={(permission) =>
                          setAccessDraft((current) =>
                            current ? { ...current, permission } : current,
                          )
                        }
                        style={{ width: '100%', marginTop: 6 }}
                      />
                      {sqlCapabilities.length > 0 && (
                        <Text type="secondary">
                          {t('users.sqlManaged')}
                        </Text>
                      )}
                    </label>

                    <Space>
                      <Switch
                        aria-label={t('users.accessEnabledLabel')}
                        checked={accessDraft.enabled}
                        disabled={selectedBusy}
                        onChange={(enabled) =>
                          setAccessDraft((current) =>
                            current ? { ...current, enabled } : current,
                          )
                        }
                      />
                      <Text>{t('users.accessEnabled')}</Text>
                    </Space>

                    {selectedCredentials.length === 0 && (
                      <Alert
                        type="warning"
                        showIcon
                        message={t('users.noCredential')}
                        description={t('users.noCredentialDescription')}
                      />
                    )}

                    <div>
                      <Button
                        type="primary"
                        loading={selectedBusy}
                        disabled={
                          !accessDraft.credential_id ||
                          selectedCredentials.length === 0
                        }
                        onClick={() => void saveAccess()}
                      >
                        {t('users.saveAccess')}
                      </Button>
                    </div>
                  </Space>
                </div>
              ) : null}
            </div>
          )}
        </section>
      )}

      <Modal
        title={t('users.editTitle', { name: editTarget?.display_name ?? '' })}
        open={!!editTarget}
        onCancel={() => setEditTarget(null)}
        onOk={() => editForm.submit()}
        confirmLoading={editLoading}
      >
        <Form form={editForm} layout="vertical" onFinish={handleEdit}>
          <Form.Item name="department_ids" label={t('users.departments')}>
            <Select
              mode="multiple"
              placeholder={t('users.selectDepartments')}
              options={departments.map(d => ({ label: d.name, value: d.id }))}
            />
          </Form.Item>
          <Form.Item name="role" label={t('users.role')}>
            <Select options={[
              { label: t('users.member'), value: 'member' },
              { label: t('users.admin'), value: 'admin' },
            ]} />
          </Form.Item>
          <Form.Item name="provisioning_mode" label={t('users.provisioningMode')}>
            <Select options={[
              { label: t('users.dedicated'), value: 'dedicated' },
              { label: t('users.multitenant'), value: 'multitenant' },
            ]} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t('users.resetTitle', { name: resetTarget?.display_name ?? '' })}
        open={!!resetTarget}
        onCancel={() => { setResetTarget(null); resetForm.resetFields() }}
        onOk={() => resetForm.submit()}
        confirmLoading={resetLoading}
      >
        <Form form={resetForm} layout="vertical" onFinish={handleResetPassword}>
          <Form.Item name="new_password" label={t('users.newPassword')} rules={[{ required: true, min: 8, message: t('users.passwordMinimum') }]}>
            <Input.Password />
          </Form.Item>
          <Form.Item
            name="confirm_password"
            label={t('users.confirmPassword')}
            dependencies={['new_password']}
            rules={[
              { required: true },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue('new_password') === value) return Promise.resolve()
                  return Promise.reject(new Error(t('users.passwordsMismatch')))
                },
              }),
            ]}
          >
            <Input.Password />
          </Form.Item>
        </Form>
      </Modal>
    </PageContainer>
  )
}
