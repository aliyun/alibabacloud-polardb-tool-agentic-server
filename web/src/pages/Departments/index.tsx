import { useEffect, useState, useCallback } from 'react'
import { Table, Button, Modal, Form, Input, InputNumber, Space, message, Popconfirm, Tag, Descriptions, Select } from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import api from '../../api/client'
import PageContainer from '../../components/PageContainer'

interface DeptItem {
  id: string
  name: string
  description: string | null
  max_instances: number | null
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
  const [depts, setDepts] = useState<DeptItem[]>([])
  const [loading, setLoading] = useState(false)
  const [modalOpen, setModalOpen] = useState(false)
  const [editingDept, setEditingDept] = useState<DeptItem | null>(null)
  const [form] = Form.useForm()

  const [mtModalOpen, setMtModalOpen] = useState(false)
  const [mtModalDeptId, setMtModalDeptId] = useState<string | null>(null)
  const [mtForm] = Form.useForm()
  const [mtInstances, setMtInstances] = useState<Record<string, MtInstanceInfo | null>>({})
  const [tenants, setTenants] = useState<Record<string, TenantItem[]>>({})
  const [addUserModalOpen, setAddUserModalOpen] = useState(false)
  const [addUserInstanceId, setAddUserInstanceId] = useState<string | null>(null)
  const [users, setUsers] = useState<{ id: string; display_name: string }[]>([])
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null)

  const fetchMtInstance = useCallback(async (deptId: string) => {
    try {
      const resp = await api.get(`/api/departments/${deptId}/multitenant-instance`)
      const inst = resp.data as MtInstanceInfo | null
      setMtInstances(prev => ({ ...prev, [deptId]: inst }))
      if (inst) {
        const tenantResp = await api.get(`/api/instances/${inst.id}/tenants`)
        setTenants(prev => ({ ...prev, [inst.id]: tenantResp.data }))
      }
    } catch {
      setMtInstances(prev => ({ ...prev, [deptId]: null }))
    }
  }, [])

  const fetchDepts = async () => {
    setLoading(true)
    try {
      const resp = await api.get('/api/departments')
      setDepts(resp.data)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { fetchDepts() }, [])

  useEffect(() => {
    depts.forEach(d => fetchMtInstance(d.id))
  }, [depts, fetchMtInstance])

  const handleSave = async (values: { name: string; description?: string; max_instances?: number | null }) => {
    if (editingDept) {
      await api.put(`/api/departments/${editingDept.id}`, values)
      message.success('Department updated')
    } else {
      await api.post('/api/departments', values)
      message.success('Department created')
    }
    setModalOpen(false)
    setEditingDept(null)
    form.resetFields()
    fetchDepts()
  }

  const handleDelete = async (id: string) => {
    try {
      await api.delete(`/api/departments/${id}`)
      message.success('Department deleted')
      fetchDepts()
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: { message?: string } } } }
      message.error(error.response?.data?.detail?.message || 'Failed to delete')
    }
  }

  const handleBindMt = async (values: { cluster_id: string; name: string; host: string; port: number; region?: string; admin_account: string; admin_password: string }) => {
    if (!mtModalDeptId) return
    try {
      await api.post(`/api/departments/${mtModalDeptId}/multitenant-instance`, values)
      message.success('Multitenant instance bound')
      setMtModalOpen(false)
      mtForm.resetFields()
      fetchMtInstance(mtModalDeptId)
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      message.error(error.response?.data?.detail || 'Bind failed')
    }
  }

  const handleUnbindMt = async (deptId: string, instanceId: string) => {
    try {
      await api.delete(`/api/departments/${deptId}/multitenant-instance/${instanceId}`)
      message.success('Unbound')
      setMtInstances(prev => ({ ...prev, [deptId]: null }))
      setTenants(prev => { const next = { ...prev }; delete next[instanceId]; return next })
    } catch {
      message.error('Unbind failed')
    }
  }

  const handleAddUser = async () => {
    if (!addUserInstanceId || !selectedUserId) return
    try {
      await api.post(`/api/instances/${addUserInstanceId}/tenants`, { user_id: selectedUserId })
      message.success('User tenant created')
      setAddUserModalOpen(false)
      setSelectedUserId(null)
      const resp = await api.get(`/api/instances/${addUserInstanceId}/tenants`)
      setTenants(prev => ({ ...prev, [addUserInstanceId]: resp.data }))
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      message.error(error.response?.data?.detail || 'Creation failed')
    }
  }

  const handleRetryTenant = async (instanceId: string, userId: string) => {
    try {
      await api.post(`/api/instances/${instanceId}/tenants/${userId}/retry`)
      message.success('Retry succeeded')
      const resp = await api.get(`/api/instances/${instanceId}/tenants`)
      setTenants(prev => ({ ...prev, [instanceId]: resp.data }))
    } catch {
      message.error('Retry failed')
    }
  }

  const handleDeleteTenant = async (instanceId: string, userId: string) => {
    try {
      await api.delete(`/api/instances/${instanceId}/tenants/${userId}`)
      message.success('Deleted')
      const resp = await api.get(`/api/instances/${instanceId}/tenants`)
      setTenants(prev => ({ ...prev, [instanceId]: resp.data }))
    } catch {
      message.error('Delete failed')
    }
  }

  const openAddUser = async (instanceId: string) => {
    setAddUserInstanceId(instanceId)
    setSelectedUserId(null)
    try {
      const resp = await api.get('/api/users', { params: { limit: 100 } })
      setUsers(resp.data.items)
    } catch {
      setUsers([])
    }
    setAddUserModalOpen(true)
  }

  const tenantColumns = (instanceId: string) => [
    { title: 'User', dataIndex: 'display_name', key: 'display_name', render: (v: string | null) => v || '-' },
    { title: 'Tenant Name', dataIndex: 'tenant_name', key: 'tenant_name' },
    {
      title: 'Status', dataIndex: 'provisioning_step', key: 'status',
      render: (step: string | null) => step
        ? <Tag color="blue">Provisioning ({step})</Tag>
        : <Tag color="green">Active</Tag>,
    },
    { title: 'Created At', dataIndex: 'created_at', key: 'created_at', render: (v: string | null) => v ? new Date(v).toLocaleString() : '-' },
    {
      title: 'Actions', key: 'actions',
      render: (_: unknown, record: TenantItem) => (
        <Space>
          {record.provisioning_step && (
            <Button size="small" onClick={() => handleRetryTenant(instanceId, record.user_id)}>Retry</Button>
          )}
          <Popconfirm title="Delete this tenant?" onConfirm={() => handleDeleteTenant(instanceId, record.user_id)}>
            <Button size="small" danger>Delete</Button>
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
          <p>No multitenant instance bound</p>
          <Button type="primary" onClick={() => { setMtModalDeptId(dept.id); mtForm.resetFields(); setMtModalOpen(true) }}>
            Bind Instance
          </Button>
        </div>
      )
    }
    const instTenants = tenants[inst.id] || []
    return (
      <div style={{ padding: 16 }}>
        <Descriptions title="Multitenant Instance" bordered size="small" column={2} style={{ marginBottom: 16 }}>
          <Descriptions.Item label="Name">{inst.name}</Descriptions.Item>
          <Descriptions.Item label="Cluster ID">{inst.cluster_id}</Descriptions.Item>
          <Descriptions.Item label="Host">{inst.host}:{inst.port}</Descriptions.Item>
          <Descriptions.Item label="Status"><Tag color="green">{inst.status}</Tag></Descriptions.Item>
          <Descriptions.Item label="Permission">
            <Tag color={inst.default_permission === 'readwrite' ? 'green' : 'orange'}>
              {inst.default_permission === 'readwrite' ? 'Read/Write' : 'Read Only'}
            </Tag>
          </Descriptions.Item>
        </Descriptions>
        <Space style={{ marginBottom: 8 }}>
          <Popconfirm title="Unbind this instance?" onConfirm={() => handleUnbindMt(dept.id, inst.id)}>
            <Button danger size="small">Unbind</Button>
          </Popconfirm>
          <Button size="small" type="primary" onClick={() => openAddUser(inst.id)}>Add User</Button>
        </Space>
        <Table dataSource={instTenants} columns={tenantColumns(inst.id)} rowKey="user_id" pagination={false} size="small" />
      </div>
    )
  }

  const columns = [
    { title: 'Name', dataIndex: 'name', key: 'name' },
    { title: 'Description', dataIndex: 'description', key: 'description' },
    { title: 'Max Instances', dataIndex: 'max_instances', key: 'max_instances', render: (v: number | null) => v ?? 'Unlimited' },
    {
      title: 'Actions',
      key: 'actions',
      render: (_: unknown, record: DeptItem) => (
        <Space>
          <Button size="small" onClick={() => { setEditingDept(record); form.setFieldsValue(record); setModalOpen(true) }}>Edit</Button>
          <Popconfirm title="Delete this department?" onConfirm={() => handleDelete(record.id)}>
            <Button size="small" danger>Delete</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <PageContainer
      title="Departments"
      description="Manage departments and multitenant instances"
      actions={
        <Button type="primary" icon={<PlusOutlined />} onClick={() => { setEditingDept(null); form.resetFields(); setModalOpen(true) }}>
          New Department
        </Button>
      }
    >
      <Table
        dataSource={depts}
        columns={columns}
        rowKey="id"
        loading={loading}
        pagination={false}
        expandable={{ expandedRowRender }}
      />
      <Modal title={editingDept ? 'Edit Department' : 'New Department'} open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()}>
        <Form form={form} onFinish={handleSave} layout="vertical">
          <Form.Item name="name" label="Name" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="description" label="Description">
            <Input.TextArea />
          </Form.Item>
          <Form.Item name="max_instances" label="Max Instances (leave empty for unlimited)">
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>
      <Modal title="Bind Multitenant Instance" open={mtModalOpen} onCancel={() => setMtModalOpen(false)} onOk={() => mtForm.submit()}>
        <Form form={mtForm} onFinish={handleBindMt} layout="vertical">
          <Form.Item name="name" label="Instance Name" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="cluster_id" label="Cluster ID" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="host" label="Host" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="port" label="Port" initialValue={3306} rules={[{ required: true }]}>
            <InputNumber min={1} max={65535} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="region" label="Region">
            <Input />
          </Form.Item>
          <Form.Item name="admin_account" label="Admin Account" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="admin_password" label="Admin Password" rules={[{ required: true }]}>
            <Input.Password />
          </Form.Item>
        </Form>
      </Modal>
      <Modal title="Add User" open={addUserModalOpen} onCancel={() => setAddUserModalOpen(false)} onOk={handleAddUser} okButtonProps={{ disabled: !selectedUserId }}>
        <Select
          showSearch
          placeholder="Select user"
          style={{ width: '100%' }}
          value={selectedUserId}
          onChange={setSelectedUserId}
          optionFilterProp="label"
          options={users.map(u => ({ value: u.id, label: u.display_name }))}
        />
      </Modal>
    </PageContainer>
  )
}
