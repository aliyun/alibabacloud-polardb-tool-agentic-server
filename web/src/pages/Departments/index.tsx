import { useEffect, useState, useCallback } from 'react'
import { Table, Button, Modal, Form, Input, Space, message, Popconfirm, Tag, Descriptions, Select, Pagination } from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import api from '../../api/client'
import PageContainer from '../../components/PageContainer'
import { formatDateTime } from '../../i18n/format'

interface DeptItem {
  id: string
  name: string
  description: string | null
}

interface MtInstanceInfo {
  id: string
  cluster_id: string
  name: string
  host: string
  port: number
  status: string
  default_permission: string
}

interface TenantItem {
  user_id: string
  display_name: string | null
  tenant_name: string
  provisioning_step: string | null
  created_at: string | null
}

export default function Departments() {
  const { t, i18n } = useTranslation()
  const [depts, setDepts] = useState<DeptItem[]>([])
  const [deptTotal, setDeptTotal] = useState(0)
  const [deptPage, setDeptPage] = useState(1)
  const [loading, setLoading] = useState(false)
  const [modalOpen, setModalOpen] = useState(false)
  const [editingDept, setEditingDept] = useState<DeptItem | null>(null)
  const [form] = Form.useForm()

  const [mtModalOpen, setMtModalOpen] = useState(false)
  const [mtModalDeptId, setMtModalDeptId] = useState<string | null>(null)
  const [mtForm] = Form.useForm()
  const [mtInstances, setMtInstances] = useState<Record<string, MtInstanceInfo | null>>({})
  const [eligibleMtInstances, setEligibleMtInstances] = useState<
    { id: string; name: string; cluster_id: string }[]
  >([])
  const [eligibleMtLoading, setEligibleMtLoading] = useState(false)
  const [tenants, setTenants] = useState<Record<string, TenantItem[]>>({})
  const [tenantTotals, setTenantTotals] = useState<Record<string, number>>({})
  const [tenantPages, setTenantPages] = useState<Record<string, number>>({})
  const [addUserModalOpen, setAddUserModalOpen] = useState(false)
  const [addUserInstanceId, setAddUserInstanceId] = useState<string | null>(null)
  const [users, setUsers] = useState<{
    id: string
    display_name: string
    external_id: string
  }[]>([])
  const [userTotal, setUserTotal] = useState(0)
  const [userOffset, setUserOffset] = useState(0)
  const [userSearch, setUserSearch] = useState('')
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null)

  const fetchTenants = useCallback(async (instanceId: string, page = 1) => {
    const response = await api.get(`/api/instances/${instanceId}/tenants`, {
      params: { offset: (page - 1) * 20, limit: 20 },
    })
    setTenants((current) => ({ ...current, [instanceId]: response.data.items }))
    setTenantTotals((current) => ({ ...current, [instanceId]: response.data.total }))
    setTenantPages((current) => ({ ...current, [instanceId]: page }))
  }, [])

  const fetchMtInstance = useCallback(async (deptId: string) => {
    try {
      const resp = await api.get(`/api/departments/${deptId}/multitenant-instance`)
      const inst = resp.data as MtInstanceInfo | null
      setMtInstances(prev => ({ ...prev, [deptId]: inst }))
      if (inst) {
        await fetchTenants(inst.id)
      }
    } catch {
      setMtInstances(prev => ({ ...prev, [deptId]: null }))
    }
  }, [fetchTenants])

  const fetchDepts = useCallback(async (page = 1) => {
    setLoading(true)
    try {
      const resp = await api.get('/api/departments', {
        params: { offset: (page - 1) * 20, limit: 20 },
      })
      setDepts(resp.data.items)
      setDeptTotal(resp.data.total)
      setDeptPage(page)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void fetchDepts() }, [fetchDepts])

  useEffect(() => {
    depts.forEach(d => fetchMtInstance(d.id))
  }, [depts, fetchMtInstance])

  const handleSave = async (values: { name: string; description?: string }) => {
    if (editingDept) {
      await api.put(`/api/departments/${editingDept.id}`, values)
      message.success(t('departments.updated'))
    } else {
      await api.post('/api/departments', values)
      message.success(t('departments.created'))
    }
    setModalOpen(false)
    setEditingDept(null)
    form.resetFields()
    void fetchDepts(deptPage)
  }

  const handleDelete = async (id: string) => {
    try {
      await api.delete(`/api/departments/${id}`)
      message.success(t('departments.deleted'))
      void fetchDepts(deptPage)
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: { message?: string } } } }
      message.error(error.response?.data?.detail?.message || t('departments.deleteFailed'))
    }
  }

  const openBindMt = async (departmentId: string) => {
    setMtModalDeptId(departmentId)
    mtForm.resetFields()
    setEligibleMtInstances([])
    setEligibleMtLoading(true)
    setMtModalOpen(true)
    try {
      const response = await api.get('/api/instances', {
        params: {
          topology: 'multitenant',
          allocation_mode: 'registered',
          limit: 200,
        },
      })
      const items = response.data.items as Array<{
        id: string
        name: string
        cluster_id: string
        status: string
      }>
      setEligibleMtInstances(
        items
          .filter(instance => instance.status === 'active')
          .map(({ id, name, cluster_id }) => ({ id, name, cluster_id })),
      )
    } catch {
      message.error(t('departments.loadEligibleFailed'))
    } finally {
      setEligibleMtLoading(false)
    }
  }

  const handleBindMt = async (values: { instance_id: string }) => {
    if (!mtModalDeptId) return
    try {
      await api.post(`/api/departments/${mtModalDeptId}/multitenant-instance`, values)
      message.success(t('departments.bound'))
      setMtModalOpen(false)
      mtForm.resetFields()
      fetchMtInstance(mtModalDeptId)
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      message.error(error.response?.data?.detail || t('departments.bindFailed'))
    }
  }

  const handleUnbindMt = async (deptId: string, instanceId: string) => {
    try {
      await api.delete(`/api/departments/${deptId}/multitenant-instance/${instanceId}`)
      message.success(t('departments.unbound'))
      setMtInstances(prev => ({ ...prev, [deptId]: null }))
      setTenants(prev => { const next = { ...prev }; delete next[instanceId]; return next })
    } catch {
      message.error(t('departments.unbindFailed'))
    }
  }

  const handleAddUser = async () => {
    if (!addUserInstanceId || !selectedUserId) return
    try {
      await api.post(`/api/instances/${addUserInstanceId}/tenants`, { user_id: selectedUserId })
      message.success(t('departments.tenantCreated'))
      setAddUserModalOpen(false)
      setSelectedUserId(null)
      await fetchTenants(addUserInstanceId, tenantPages[addUserInstanceId] ?? 1)
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      message.error(error.response?.data?.detail || t('departments.tenantCreationFailed'))
    }
  }

  const handleRetryTenant = async (instanceId: string, userId: string) => {
    try {
      await api.post(`/api/instances/${instanceId}/tenants/${userId}/retry`)
      message.success(t('departments.retrySucceeded'))
      await fetchTenants(instanceId, tenantPages[instanceId] ?? 1)
    } catch {
      message.error(t('departments.retryFailed'))
    }
  }

  const handleDeleteTenant = async (instanceId: string, userId: string) => {
    try {
      await api.delete(`/api/instances/${instanceId}/tenants/${userId}`)
      message.success(t('departments.tenantDeleted'))
      await fetchTenants(instanceId, tenantPages[instanceId] ?? 1)
    } catch {
      message.error(t('departments.tenantDeleteFailed'))
    }
  }

  const loadUsers = async (search = userSearch, offset = userOffset) => {
    try {
      const resp = await api.get('/api/users', {
        params: { search: search || undefined, offset, limit: 50 },
      })
      setUsers(resp.data.items)
      setUserTotal(resp.data.total)
    } catch {
      setUsers([])
      setUserTotal(0)
    }
  }

  const openAddUser = async (instanceId: string) => {
    setAddUserInstanceId(instanceId)
    setSelectedUserId(null)
    setUserSearch('')
    setUserOffset(0)
    await loadUsers('', 0)
    setAddUserModalOpen(true)
  }

  const tenantColumns = (instanceId: string) => [
    { title: t('departments.user'), dataIndex: 'display_name', key: 'display_name', render: (v: string | null) => v || '-' },
    { title: t('departments.tenantName'), dataIndex: 'tenant_name', key: 'tenant_name' },
    {
      title: t('departments.status'), dataIndex: 'provisioning_step', key: 'status',
      render: (step: string | null) => step
        ? <Tag color="blue">{t('departments.provisioning', { step })}</Tag>
        : <Tag color="green">{t('departments.active')}</Tag>,
    },
    { title: t('departments.createdAt'), dataIndex: 'created_at', key: 'created_at', render: (v: string | null) => v ? formatDateTime(v, i18n.resolvedLanguage ?? i18n.language) : '-' },
    {
      title: t('departments.actions'), key: 'actions',
      render: (_: unknown, record: TenantItem) => (
        <Space>
          {record.provisioning_step && (
            <Button size="small" onClick={() => handleRetryTenant(instanceId, record.user_id)}>{t('common.retry')}</Button>
          )}
          <Popconfirm title={t('departments.deleteTenantConfirm')} onConfirm={() => handleDeleteTenant(instanceId, record.user_id)}>
            <Button size="small" danger>{t('departments.delete')}</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  const expandedRowRender = (dept: DeptItem) => {
    const inst = mtInstances[dept.id]
    if (!inst) {
      return (
        <div style={{ padding: 16 }}>
          <p>{t('departments.noMultitenantInstance')}</p>
          <Button type="primary" onClick={() => void openBindMt(dept.id)}>
            {t('departments.bindInstance')}
          </Button>
        </div>
      )
    }
    const instTenants = tenants[inst.id] || []
    return (
      <div style={{ padding: 16 }}>
        <Descriptions title={t('departments.instanceDetails')} bordered size="small" column={2} style={{ marginBottom: 16 }}>
          <Descriptions.Item label={t('departments.name')}>{inst.name}</Descriptions.Item>
          <Descriptions.Item label={t('departments.clusterId')}>{inst.cluster_id}</Descriptions.Item>
          <Descriptions.Item label={t('departments.host')}>{inst.host}:{inst.port}</Descriptions.Item>
          <Descriptions.Item label={t('departments.status')}><Tag color="green">{inst.status}</Tag></Descriptions.Item>
          <Descriptions.Item label={t('departments.permission')}>
            <Tag color={inst.default_permission === 'readwrite' ? 'green' : 'orange'}>
              {inst.default_permission === 'readwrite' ? t('departments.readWrite') : t('departments.readOnly')}
            </Tag>
          </Descriptions.Item>
        </Descriptions>
        <Space style={{ marginBottom: 8 }}>
          <Popconfirm title={t('departments.unbindConfirm')} onConfirm={() => handleUnbindMt(dept.id, inst.id)}>
            <Button danger size="small">{t('departments.unbind')}</Button>
          </Popconfirm>
          <Button size="small" type="primary" onClick={() => openAddUser(inst.id)}>{t('departments.addUser')}</Button>
        </Space>
        <Table
          dataSource={instTenants}
          columns={tenantColumns(inst.id)}
          rowKey="user_id"
          pagination={{
            current: tenantPages[inst.id] ?? 1,
            pageSize: 20,
            total: tenantTotals[inst.id] ?? 0,
            showSizeChanger: false,
            onChange: (page) => void fetchTenants(inst.id, page),
          }}
          size="small"
        />
      </div>
    )
  }

  const columns = [
    { title: t('departments.name'), dataIndex: 'name', key: 'name' },
    { title: t('departments.descriptionLabel'), dataIndex: 'description', key: 'description' },
    {
      title: t('departments.actions'),
      key: 'actions',
      render: (_: unknown, record: DeptItem) => (
        <Space>
          <Button size="small" onClick={() => { setEditingDept(record); form.setFieldsValue(record); setModalOpen(true) }}>{t('departments.edit')}</Button>
          <Popconfirm title={t('departments.deleteDepartmentConfirm')} onConfirm={() => handleDelete(record.id)}>
            <Button size="small" danger>{t('departments.delete')}</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <PageContainer
      title={t('departments.title')}
      description={t('departments.description')}
      actions={
        <Button type="primary" icon={<PlusOutlined />} onClick={() => { setEditingDept(null); form.resetFields(); setModalOpen(true) }}>
          {t('departments.newDepartment')}
        </Button>
      }
    >
      <Table
        dataSource={depts}
        columns={columns}
        rowKey="id"
        loading={loading}
        pagination={{
          current: deptPage,
          pageSize: 20,
          total: deptTotal,
          showSizeChanger: false,
          onChange: (page) => void fetchDepts(page),
        }}
        expandable={{ expandedRowRender }}
      />
      <Modal title={editingDept ? t('departments.editDepartment') : t('departments.newDepartment')} open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()}>
        <Form form={form} onFinish={handleSave} layout="vertical">
          <Form.Item name="name" label={t('departments.name')} rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="description" label={t('departments.descriptionLabel')}>
            <Input.TextArea />
          </Form.Item>
        </Form>
      </Modal>
      <Modal title={t('departments.bindTitle')} open={mtModalOpen} onCancel={() => setMtModalOpen(false)} onOk={() => mtForm.submit()}>
        <Form form={mtForm} onFinish={handleBindMt} layout="vertical">
          <Form.Item name="instance_id" label={t('departments.multitenantInstance')} rules={[{ required: true }]}>
            <Select
              loading={eligibleMtLoading}
              placeholder={t('departments.selectMultitenantInstance')}
              options={eligibleMtInstances.map(instance => ({
                value: instance.id,
                label: `${instance.name} (${instance.cluster_id})`,
              }))}
              notFoundContent={
                eligibleMtLoading
                  ? t('departments.loading')
                  : t('departments.noEligibleInstances')
              }
            />
          </Form.Item>
        </Form>
      </Modal>
      <Modal title={t('departments.addUser')} open={addUserModalOpen} onCancel={() => setAddUserModalOpen(false)} onOk={handleAddUser} okButtonProps={{ disabled: !selectedUserId }}>
        <Select
          showSearch
          placeholder={t('departments.selectUser')}
          style={{ width: '100%' }}
          value={selectedUserId}
          onChange={setSelectedUserId}
          filterOption={false}
          onSearch={(value) => {
            setUserSearch(value)
            setUserOffset(0)
            void loadUsers(value, 0)
          }}
          options={users.map(u => ({ value: u.id, label: `${u.display_name} (${u.external_id})` }))}
        />
        <Pagination
          size="small"
          current={Math.floor(userOffset / 50) + 1}
          pageSize={50}
          total={userTotal}
          showSizeChanger={false}
          style={{ marginTop: 12 }}
          onChange={(page) => {
            const offset = (page - 1) * 50
            setUserOffset(offset)
            void loadUsers(userSearch, offset)
          }}
        />
      </Modal>
    </PageContainer>
  )
}
