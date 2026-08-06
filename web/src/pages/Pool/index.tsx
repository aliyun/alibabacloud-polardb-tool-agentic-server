import { useEffect, useState } from 'react'
import { Table, Button, Card, Statistic, Row, Col, message, Alert, Space, Popconfirm, Tag } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { getPoolStatus, getPoolInstances, removePoolInstance, triggerReplenish, type PoolStatus, type PoolInstance } from '../../api/pool'
import PageContainer from '../../components/PageContainer'
import { formatDateTime } from '../../i18n/format'

export default function Pool() {
  const { t, i18n } = useTranslation()
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
      message.success(t('pool.replenishTriggered'))
      refresh()
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      message.error(error.response?.data?.detail || t('pool.actionFailed'))
    }
  }

  const handleRemove = async (id: string) => {
    try {
      await removePoolInstance(id)
      message.success(t('pool.removed'))
      refresh()
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      message.error(error.response?.data?.detail || t('pool.removeFailed'))
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
    { title: t('pool.clusterId'), dataIndex: 'cluster_id', key: 'cluster_id' },
    { title: t('pool.status'), dataIndex: 'status', key: 'status', render: (s: string) => <Tag color={statusColor(s)}>{s}</Tag> },
    { title: t('pool.step'), dataIndex: 'provisioning_step', key: 'provisioning_step' },
    { title: t('pool.created'), dataIndex: 'created_at', key: 'created_at', render: (value: string | null) => value ? formatDateTime(value, i18n.resolvedLanguage ?? i18n.language) : '-' },
    {
      title: t('pool.actions'), key: 'actions',
      render: (_: unknown, record: PoolInstance) => canRemove(record) ? (
        <Popconfirm title={t('pool.removeConfirm')} onConfirm={() => handleRemove(record.id)}>
          <Button size="small" danger>{t('pool.remove')}</Button>
        </Popconfirm>
      ) : null,
    },
  ]

  return (
    <PageContainer
      title={t('pool.title')}
      description={t('pool.description')}
      actions={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={refresh}>{t('pool.refresh')}</Button>
          <Button type="primary" onClick={handleReplenish} disabled={!status || status.target <= 0}>{t('pool.replenish')}</Button>
        </Space>
      }
    >
      {status && status.failed > 0 && <Alert type="error" message={t('pool.failures', { count: status.failed })} style={{ marginBottom: 16 }} showIcon />}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}><Card><Statistic title={t('pool.target')} value={status?.target ?? 0} /></Card></Col>
        <Col span={6}><Card><Statistic title={t('pool.available')} value={status?.available ?? 0} valueStyle={{ color: '#34c759' }} /></Card></Col>
        <Col span={6}><Card><Statistic title={t('pool.creating')} value={status?.pool_creating ?? 0} /></Card></Col>
        <Col span={6}><Card><Statistic title={t('pool.failed')} value={status?.failed ?? 0} valueStyle={status?.failed ? { color: '#ff3b30' } : {}} /></Card></Col>
      </Row>
      <Table dataSource={instances} columns={columns} rowKey="id" loading={loading} pagination={false} />
    </PageContainer>
  )
}
