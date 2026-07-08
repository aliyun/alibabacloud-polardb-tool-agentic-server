import { useEffect, useState } from 'react'
import { Table, Button, Modal, Form, Input, Select, Tag, message, Popconfirm } from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import api from '../../api/client'
import PageContainer from '../../components/PageContainer'

interface InstanceItem {
  id: string
  cluster_id: string
  name: string
  type: string
  status: string
  region: string | null
  host: string | null
  port: number | null
}

export default function Instances() {
  const [instances, setInstances] = useState<InstanceItem[]>([])
  const [loading, setLoading] = useState(false)
  const [modalOpen, setModalOpen] = useState(false)
  const [form] = Form.useForm()
  const navigate = useNavigate()

  const fetchInstances = async () => {
    setLoading(true)
    try {
      const resp = await api.get('/api/instances')
      setInstances(resp.data)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { fetchInstances() }, [])

  const handleRegister = async (values: Record<string, unknown>) => {
    try {
      await api.post('/api/instances', values)
      message.success('Instance registered')
      setModalOpen(false)
      form.resetFields()
      fetchInstances()
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      message.error(error.response?.data?.detail || 'Failed to register')
    }
  }

  const handleRemove = async (id: string) => {
    await api.delete(`/api/instances/${id}`)
    message.success('Instance removed')
    fetchInstances()
  }

  const columns = [
    { title: 'Name', dataIndex: 'name', key: 'name', render: (name: string, record: InstanceItem) => <a onClick={() => navigate(`/instances/${record.id}`)}>{name}</a> },
    { title: 'Cluster ID', dataIndex: 'cluster_id', key: 'cluster_id' },
    { title: 'Type', dataIndex: 'type', key: 'type', render: (t: string) => <Tag>{t}</Tag> },
    { title: 'Status', dataIndex: 'status', key: 'status', render: (s: string) => <Tag color={s === 'active' ? 'green' : 'orange'}>{s}</Tag> },
    {
      title: 'Actions',
      key: 'actions',
      render: (_: unknown, record: InstanceItem) => (
        <Popconfirm title="Remove this instance?" onConfirm={() => handleRemove(record.id)}>
          <Button size="small" danger>Remove</Button>
        </Popconfirm>
      ),
    },
  ]

  return (
    <PageContainer
      title="Instances"
      description="Registered PolarDB clusters"
      actions={
        <Button type="primary" icon={<PlusOutlined />} onClick={() => { form.resetFields(); setModalOpen(true) }}>
          Register Instance
        </Button>
      }
    >
      <Table dataSource={instances} columns={columns} rowKey="id" loading={loading} pagination={false} />
      <Modal title="Register Instance" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()}>
        <Form form={form} onFinish={handleRegister} layout="vertical">
          <Form.Item name="cluster_id" label="Cluster ID" rules={[{ required: true }]}>
            <Input placeholder="pc-xxx" />
          </Form.Item>
          <Form.Item name="name" label="Name" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="type" label="Type" initialValue="shared">
            <Select options={[{ value: 'shared', label: 'Shared' }, { value: 'personal', label: 'Personal' }]} />
          </Form.Item>
          <Form.Item name="region" label="Region">
            <Input placeholder="cn-hangzhou" />
          </Form.Item>
        </Form>
      </Modal>
    </PageContainer>
  )
}
