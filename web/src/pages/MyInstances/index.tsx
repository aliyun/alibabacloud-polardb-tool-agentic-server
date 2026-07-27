import { useEffect, useState } from 'react'
import { Table, Tag } from 'antd'
import api from '../../api/client'
import PageContainer from '../../components/PageContainer'

interface AccessibleInstance {
  instance_id: string
  cluster_id: string
  name: string
  type: string
  status: string
  access_type: string
  permission: string
}

export default function MyInstances() {
  const [instances, setInstances] = useState<AccessibleInstance[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    setLoading(true)
    api.get('/mcp/list_instances').then((resp) => {
      setInstances(resp.data.instances || [])
    }).finally(() => setLoading(false))
  }, [])

  const columns = [
    { title: 'Name', dataIndex: 'name', key: 'name' },
    { title: 'Cluster ID', dataIndex: 'cluster_id', key: 'cluster_id' },
    { title: 'Type', dataIndex: 'type', key: 'type', render: (t: string) => <Tag>{t}</Tag> },
    { title: 'Access', dataIndex: 'access_type', key: 'access', render: (a: string) => <Tag color={a === 'personal' ? 'blue' : 'green'}>{a}</Tag> },
    { title: 'Permission', dataIndex: 'permission', key: 'permission', render: (p: string) => <Tag>{p}</Tag> },
    { title: 'Status', dataIndex: 'status', key: 'status', render: (s: string) => <Tag color={s === 'active' ? 'green' : 'orange'}>{s}</Tag> },
  ]

  return (
    <PageContainer title="My Instances" description="Instances accessible to your account">
      <Table dataSource={instances} columns={columns} rowKey="instance_id" loading={loading} pagination={false} />
    </PageContainer>
  )
}
