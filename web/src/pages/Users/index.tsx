import { useEffect, useState } from 'react'
import { Table, Input, Button, Tag, Space, message, Modal, Form, Select } from 'antd'
import { EditOutlined } from '@ant-design/icons'
import api from '../../api/client'
import PageContainer from '../../components/PageContainer'

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

export default function Users() {
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

  const fetchUsers = async () => {
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
  }

  useEffect(() => { fetchUsers() }, [page, search])
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
      message.success('User updated')
      setEditTarget(null)
      fetchUsers()
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to update user')
    } finally {
      setEditLoading(false)
    }
  }

  const toggleStatus = (user: UserItem) => {
    const action = user.status === 'active' ? 'disable' : 'enable'
    Modal.confirm({
      title: `${action === 'disable' ? 'Disable' : 'Enable'} user ${user.display_name}?`,
      onOk: async () => {
        await api.put(`/api/users/${user.id}/${action}`)
        message.success(`User ${action}d`)
        fetchUsers()
      },
    })
  }

  const handleResetPassword = async (values: { new_password: string }) => {
    if (!resetTarget) return
    setResetLoading(true)
    try {
      await api.put(`/api/users/${resetTarget.id}/reset-password`, values)
      message.success(`Password reset for ${resetTarget.display_name}`)
      setResetTarget(null)
      resetForm.resetFields()
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to reset password')
    } finally {
      setResetLoading(false)
    }
  }

  const columns = [
    { title: 'Name', dataIndex: 'display_name', key: 'name' },
    { title: 'Email', dataIndex: 'email', key: 'email' },
    { title: 'Role', dataIndex: 'role', key: 'role', render: (role: string) => <Tag color={role === 'admin' ? 'red' : 'blue'}>{role}</Tag> },
    {
      title: 'Departments',
      key: 'departments',
      render: (_: unknown, record: UserItem) => (
        <Space>
          {record.departments.length === 0 && <Tag>None</Tag>}
          {record.departments.map((d) => (
            <Tag key={d.id} color={d.is_primary ? 'green' : 'default'}>{d.name}</Tag>
          ))}
        </Space>
      ),
    },
    {
      title: 'Provisioning',
      dataIndex: 'provisioning_mode',
      key: 'provisioning_mode',
      render: (v: string | null) => <Tag>{v ?? 'dedicated'}</Tag>,
    },
    {
      title: 'Status',
      dataIndex: 'status',
      key: 'status',
      render: (status: string) => <Tag color={status === 'active' ? 'green' : 'red'}>{status}</Tag>,
    },
    {
      title: 'Actions',
      key: 'actions',
      render: (_: unknown, record: UserItem) => (
        <Space>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(record)}>
            Edit
          </Button>
          <Button size="small" onClick={() => toggleStatus(record)}>
            {record.status === 'active' ? 'Disable' : 'Enable'}
          </Button>
          {authMode === 'builtin' && (
            <Button size="small" onClick={() => setResetTarget(record)}>
              Reset Password
            </Button>
          )}
        </Space>
      ),
    },
  ]

  return (
    <PageContainer
      title="Users"
      description="Manage user accounts and permissions"
      actions={<Input.Search placeholder="Search users" onSearch={setSearch} allowClear style={{ width: 280 }} />}
    >
      <Table
        dataSource={users}
        columns={columns}
        rowKey="id"
        loading={loading}
        pagination={{ total, pageSize: 20, current: page, onChange: setPage }}
      />

      <Modal
        title={`Edit User: ${editTarget?.display_name ?? ''}`}
        open={!!editTarget}
        onCancel={() => setEditTarget(null)}
        onOk={() => editForm.submit()}
        confirmLoading={editLoading}
      >
        <Form form={editForm} layout="vertical" onFinish={handleEdit}>
          <Form.Item name="department_ids" label="Departments">
            <Select
              mode="multiple"
              placeholder="Select departments"
              options={departments.map(d => ({ label: d.name, value: d.id }))}
            />
          </Form.Item>
          <Form.Item name="role" label="Role">
            <Select options={[
              { label: 'Member', value: 'member' },
              { label: 'Admin', value: 'admin' },
            ]} />
          </Form.Item>
          <Form.Item name="provisioning_mode" label="Provisioning Mode">
            <Select options={[
              { label: 'Dedicated', value: 'dedicated' },
              { label: 'Multitenant', value: 'multitenant' },
            ]} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`Reset Password: ${resetTarget?.display_name ?? ''}`}
        open={!!resetTarget}
        onCancel={() => { setResetTarget(null); resetForm.resetFields() }}
        onOk={() => resetForm.submit()}
        confirmLoading={resetLoading}
      >
        <Form form={resetForm} layout="vertical" onFinish={handleResetPassword}>
          <Form.Item name="new_password" label="New Password" rules={[{ required: true, min: 8, message: 'At least 8 characters' }]}>
            <Input.Password />
          </Form.Item>
          <Form.Item
            name="confirm_password"
            label="Confirm Password"
            dependencies={['new_password']}
            rules={[
              { required: true },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue('new_password') === value) return Promise.resolve()
                  return Promise.reject(new Error('Passwords do not match'))
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
