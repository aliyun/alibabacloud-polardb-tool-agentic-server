import { useEffect, useState } from 'react'
import { Table, Button, Card, Statistic, Row, Col, message, Alert, Space, Popconfirm, Tag } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { getPoolStatus, getPoolInstances, removePoolInstance, triggerReplenish, type PoolStatus, type PoolInstance } from '../../api/pool'
import PageContainer from '../../components/PageContainer'

export default function Pool() {
  const [status, setStatus] = useState<PoolStatus | null>(null)
  const [instances, setInstances] = useState<PoolInstance[]>([])
  const [loading, setLoading] = useState(false)

  const refresh = async () => {
    setLoading(true)
    try {
      const [statusResp, instancesResp] = await Promise.all([getPoolStatus(), getPoolInstances()])
      setStatus(statusResp.data)
      setInstances(instancesResp.data)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { refresh() }, [])

  const handleReplenish = async () => {
    try {
      await triggerReplenish()
      message.success('Replenishment triggered')
      refresh()
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      message.error(error.response?.data?.detail || 'Failed')
    }
  }

  const handleRemove = async (id: string) => {
    try {
      await removePoolInstance(id)
      message.success('Removed')
      refresh()
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      message.error(error.response?.data?.detail || 'Remove failed')
    }
  }

  const statusColor = (s: string) => {
    if (s === 'active') return 'green'
    if (s === 'creating') return 'blue'
    if (s === 'failed') return 'red'
    return 'default'
  }

  const canRemove = (record: PoolInstance) =>
    record.status === 'active'
    || record.status === 'failed'
    || (record.status === 'creating' && record.cluster_id.startsWith('pool-pending-'))

  const columns = [
    { title: 'Cluster ID', dataIndex: 'cluster_id', key: 'cluster_id' },
    { title: 'Status', dataIndex: 'status', key: 'status', render: (s: string) => <Tag color={statusColor(s)}>{s}</Tag> },
    { title: 'Step', dataIndex: 'provisioning_step', key: 'provisioning_step' },
    { title: 'Created', dataIndex: 'created_at', key: 'created_at' },
    {
      title: 'Actions', key: 'actions',
      render: (_: unknown, record: PoolInstance) => canRemove(record) ? (
        <Popconfirm title="Remove from pool?" onConfirm={() => handleRemove(record.id)}>
          <Button size="small" danger>Remove</Button>
        </Popconfirm>
      ) : null,
    },
  ]

  return (
    <PageContainer
      title="Instance Pool"
      description="Pre-provisioned cluster pool status"
      actions={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={refresh}>Refresh</Button>
          <Button type="primary" onClick={handleReplenish} disabled={!status || status.target <= 0}>Replenish</Button>
        </Space>
      }
    >
      {status && status.failed > 0 && <Alert type="error" message={`${status.failed} failed pool instance(s) need attention`} style={{ marginBottom: 16 }} showIcon />}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}><Card><Statistic title="Target" value={status?.target ?? 0} /></Card></Col>
        <Col span={6}><Card><Statistic title="Available" value={status?.available ?? 0} valueStyle={{ color: '#34c759' }} /></Card></Col>
        <Col span={6}><Card><Statistic title="Creating" value={status?.pool_creating ?? 0} /></Card></Col>
        <Col span={6}><Card><Statistic title="Failed" value={status?.failed ?? 0} valueStyle={status?.failed ? { color: '#ff3b30' } : {}} /></Card></Col>
      </Row>
      <Table dataSource={instances} columns={columns} rowKey="id" loading={loading} pagination={false} />
    </PageContainer>
  )
}
