import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { Descriptions, Tag, Spin } from 'antd'
import api from '../../api/client'
import PageContainer from '../../components/PageContainer'

interface InstanceInfo {
  id: string
  cluster_id: string
  name: string
  type: string
  status: string
  region: string | null
  host: string | null
  port: number | null
}

export default function InstanceDetail() {
  const { id } = useParams<{ id: string }>()
  const [instance, setInstance] = useState<InstanceInfo | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.get(`/api/instances/${id}`).then((resp) => {
      setInstance(resp.data)
      setLoading(false)
    })
  }, [id])

  if (loading || !instance) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: 48 }}>
        <Spin size="large" />
      </div>
    )
  }

  return (
    <PageContainer title={instance.name} description={`Cluster ${instance.cluster_id}`}>
      <Descriptions bordered column={2}>
        <Descriptions.Item label="Name">{instance.name}</Descriptions.Item>
        <Descriptions.Item label="Cluster ID">{instance.cluster_id}</Descriptions.Item>
        <Descriptions.Item label="Type"><Tag>{instance.type}</Tag></Descriptions.Item>
        <Descriptions.Item label="Status"><Tag color={instance.status === 'active' ? 'green' : 'orange'}>{instance.status}</Tag></Descriptions.Item>
        <Descriptions.Item label="Region">{instance.region || '-'}</Descriptions.Item>
        <Descriptions.Item label="Host">{instance.host || '-'}:{instance.port || '-'}</Descriptions.Item>
      </Descriptions>
    </PageContainer>
  )
}
