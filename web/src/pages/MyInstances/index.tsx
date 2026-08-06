import { useEffect, useState } from 'react'
import { Table, Tag } from 'antd'
import { useTranslation } from 'react-i18next'
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
  const { t } = useTranslation()
  const [instances, setInstances] = useState<AccessibleInstance[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    setLoading(true)
    api.get('/mcp/list_instances').then((resp) => {
      setInstances(resp.data.instances || [])
    }).finally(() => setLoading(false))
  }, [])

  const columns = [
    { title: t('myInstances.name'), dataIndex: 'name', key: 'name' },
    { title: t('myInstances.clusterId'), dataIndex: 'cluster_id', key: 'cluster_id' },
    { title: t('myInstances.type'), dataIndex: 'type', key: 'type', render: (value: string) => <Tag>{value}</Tag> },
    { title: t('myInstances.access'), dataIndex: 'access_type', key: 'access', render: (a: string) => <Tag color={a === 'personal' ? 'blue' : 'green'}>{a}</Tag> },
    { title: t('myInstances.permission'), dataIndex: 'permission', key: 'permission', render: (p: string) => <Tag>{p}</Tag> },
    { title: t('myInstances.status'), dataIndex: 'status', key: 'status', render: (s: string) => <Tag color={s === 'active' ? 'green' : 'orange'}>{s}</Tag> },
  ]

  return (
    <PageContainer title={t('myInstances.title')} description={t('myInstances.description')}>
      <Table dataSource={instances} columns={columns} rowKey="instance_id" loading={loading} pagination={false} />
    </PageContainer>
  )
}
