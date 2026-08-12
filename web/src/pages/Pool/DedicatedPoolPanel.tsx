import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Col,
  Descriptions,
  Empty,
  Popconfirm,
  Row,
  Skeleton,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { ReloadOutlined, SettingOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'

import { getAPIErrorMessage } from '../../api/client'
import {
  drainDedicatedPool,
  listDedicatedPools,
  runDedicatedMemberAction,
  type DedicatedPool,
  type DedicatedPoolMember,
} from '../../api/dedicatedPools'
import {
  listDBInstanceResources,
  restoreDBInstanceResource,
  type DBInstanceResourceAdmin,
} from '../../api/dbInstanceResources'
import { formatDateTime } from '../../i18n/format'
import PermissionTemplateDrawer from './PermissionTemplateDrawer'

const { Paragraph, Text, Title } = Typography

function statusColor(status: string) {
  if (['available', 'allocated', 'active', 'fresh'].includes(status)) return 'green'
  if (['replenishing', 'checking', 'restoring'].includes(status)) return 'blue'
  if (['quarantined', 'failed', 'delete_failed'].includes(status)) return 'red'
  if (['cooling_down', 'draining', 'stale'].includes(status)) return 'orange'
  return 'default'
}

function readinessMessage(member: DedicatedPoolMember, t: (key: string) => string) {
  if (member.status === 'quarantined') return t('pool.memberFailedDescription')
  if (member.readiness_status === 'stale') return t('pool.memberStaleDescription')
  if (member.readiness_status === 'checking') return t('pool.memberCheckingDescription')
  return t('pool.memberFreshDescription')
}

export default function DedicatedPoolPanel() {
  const { t, i18n } = useTranslation()
  const navigate = useNavigate()
  const [pools, setPools] = useState<DedicatedPool[]>([])
  const [resources, setResources] = useState<DBInstanceResourceAdmin[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [permissionPool, setPermissionPool] = useState<DedicatedPool | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const response = await listDedicatedPools()
      setPools(response.data)
      try {
        const resourceResponse = await listDBInstanceResources()
        setResources(
          resourceResponse.data.filter(
            (resource) =>
              resource.provisioning_mode === 'dedicated' &&
              ['deleting', 'cooling_down', 'delete_failed', 'restoring'].includes(
                resource.status,
              ),
          ),
        )
      } catch {
        setResources([])
      }
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('pool.dedicatedLoadFailed')))
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => {
    void load()
  }, [load])

  const memberAction = async (
    pool: DedicatedPool,
    member: DedicatedPoolMember,
    action: 'retry' | 'quarantine' | 'destroy',
  ) => {
    try {
      await runDedicatedMemberAction(pool.id, member.id, action)
      message.success(t('pool.memberActionQueued'))
      await load()
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('pool.actionFailed')))
    }
  }

  const drain = async (pool: DedicatedPool) => {
    try {
      await drainDedicatedPool(pool.id)
      message.success(t('pool.drainQueued'))
      await load()
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('pool.actionFailed')))
    }
  }

  const restore = async (resource: DBInstanceResourceAdmin) => {
    try {
      await restoreDBInstanceResource(resource.id)
      message.success(t('pool.restoreQueued'))
      await load()
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('pool.restoreFailed')))
    }
  }

  const memberColumns = (pool: DedicatedPool) => [
    {
      title: t('pool.instance'),
      dataIndex: 'instance_id',
      width: 220,
      render: (value: string) => <Text code>{value}</Text>,
    },
    {
      title: t('pool.status'),
      dataIndex: 'status',
      width: 150,
      render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag>,
    },
    {
      title: t('pool.readiness'),
      key: 'readiness',
      width: 310,
      render: (_: unknown, member: DedicatedPoolMember) => (
        <Space direction="vertical" size={0}>
          <Tag color={statusColor(member.readiness_status)}>
            {member.readiness_status}
          </Tag>
          <Text type="secondary">{readinessMessage(member, t)}</Text>
          {member.last_ready_verified_at && (
            <Text type="secondary">
              {t('pool.lastVerified', {
                value: formatDateTime(
                  member.last_ready_verified_at,
                  i18n.resolvedLanguage ?? i18n.language,
                ),
              })}
            </Text>
          )}
        </Space>
      ),
    },
    {
      title: t('pool.cooldown'),
      key: 'cooldown',
      width: 210,
      render: (_: unknown, member: DedicatedPoolMember) =>
        t('pool.cooldownValue', {
          count: member.delete_cooldown_duration_hours,
          source: t(`pool.cooldownSource.${member.delete_cooldown_source}`),
        }),
    },
    {
      title: t('pool.actions'),
      key: 'actions',
      fixed: 'right' as const,
      width: 240,
      render: (_: unknown, member: DedicatedPoolMember) => (
        <Space wrap>
          {member.actions.retry && (
            <Button
              size="small"
              aria-label={t('pool.retryMemberLabel', {
                instance: member.instance_id,
              })}
              onClick={() => void memberAction(pool, member, 'retry')}
            >
              {t('common.retry')}
            </Button>
          )}
          {member.actions.quarantine && (
            <Popconfirm
              title={t('pool.quarantineConfirm')}
              onConfirm={() => void memberAction(pool, member, 'quarantine')}
            >
              <Button size="small">{t('pool.quarantine')}</Button>
            </Popconfirm>
          )}
          {member.actions.destroy && (
            <Popconfirm
              title={t('pool.destroyConfirm')}
              description={t('pool.destroyDescription')}
              okButtonProps={{ danger: true }}
              onConfirm={() => void memberAction(pool, member, 'destroy')}
            >
              <Button size="small" danger>
                {t('pool.destroy')}
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ]

  return (
    <Space direction="vertical" size={20} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message={t('pool.dedicatedBillingTitle')}
        description={t('pool.dedicatedBillingDescription')}
      />
      <Space wrap>
        <Button
          type="primary"
          onClick={() => navigate('/pool/dedicated/new')}
        >
          {t('pool.createDedicated')}
        </Button>
        <Button icon={<ReloadOutlined />} onClick={() => void load()}>
          {t('pool.refresh')}
        </Button>
      </Space>
      {error && (
        <Alert
          role="alert"
          type="error"
          showIcon
          message={error}
          action={
            <Button size="small" onClick={() => void load()}>
              {t('common.retry')}
            </Button>
          }
        />
      )}
      {loading ? (
        <Skeleton active paragraph={{ rows: 8 }} />
      ) : pools.length === 0 ? (
        <Empty
          description={
            <Space direction="vertical" size={2}>
              <Text strong>{t('pool.dedicatedEmpty')}</Text>
              <Text type="secondary">{t('pool.dedicatedEmptyDescription')}</Text>
            </Space>
          }
        >
          <Button
            type="primary"
            onClick={() => navigate('/pool/dedicated/new')}
          >
            {t('pool.createDedicated')}
          </Button>
        </Empty>
      ) : (
        pools.map((pool) => (
          <section
            key={pool.id}
            aria-labelledby={`pool-${pool.id}`}
            style={{
              background: 'var(--surface-primary)',
              border: '1px solid var(--border-strong)',
              borderRadius: 'var(--radius-lg)',
              padding: 20,
            }}
          >
            <Row gutter={[16, 16]} align="middle" justify="space-between">
              <Col>
                <Space align="center">
                  <Title id={`pool-${pool.id}`} level={4} style={{ margin: 0 }}>
                    {pool.name}
                  </Title>
                  <Tag color={statusColor(pool.status)}>{pool.status}</Tag>
                </Space>
                <Paragraph type="secondary" style={{ margin: '6px 0 0' }}>
                  {t('pool.poolLocation', {
                    region: pool.region_id,
                    vpc: pool.vpc_id,
                  })}
                </Paragraph>
              </Col>
              <Col>
                <Space wrap>
                  <Button
                    onClick={() => navigate(`/pool/dedicated/${encodeURIComponent(pool.id)}`)}
                  >
                    {t('pool.openDetails')}
                  </Button>
                  <Button
                    icon={<SettingOutlined />}
                    onClick={() => setPermissionPool(pool)}
                  >
                    {t('pool.permissions')}
                  </Button>
                  {pool.status === 'active' && (
                    <Popconfirm
                      title={t('pool.drainConfirm')}
                      description={t('pool.drainDescription')}
                      onConfirm={() => void drain(pool)}
                    >
                      <Button>{t('pool.drain')}</Button>
                    </Popconfirm>
                  )}
                </Space>
              </Col>
            </Row>
            <Descriptions
              size="small"
              bordered
              column={{ xs: 1, sm: 2, lg: 4 }}
              style={{ marginTop: 16 }}
            >
              <Descriptions.Item label={t('pool.target')}>
                {pool.target_size}
              </Descriptions.Item>
              <Descriptions.Item label={t('pool.allocatable')}>
                {pool.allocatable}
              </Descriptions.Item>
              <Descriptions.Item label={t('pool.planning')}>
                {pool.planning}
              </Descriptions.Item>
              <Descriptions.Item label={t('pool.billable')}>
                {pool.billable_total} / {pool.max_total_members}
              </Descriptions.Item>
              <Descriptions.Item label={t('pool.reclaimPolicy')}>
                {pool.reclaim_policy}
              </Descriptions.Item>
              <Descriptions.Item label={t('pool.defaultCooldown')}>
                {t('pool.hours', {
                  count: pool.effective_delete_cooldown_duration_hours,
                })}
              </Descriptions.Item>
              <Descriptions.Item label={t('pool.purchaseBudget')}>
                {pool.max_member_purchases_per_hour}
              </Descriptions.Item>
              <Descriptions.Item label={t('pool.agentBudget')}>
                {pool.max_create_requests_per_agent_per_hour} /{' '}
                {pool.max_delete_requests_per_agent_per_hour}
              </Descriptions.Item>
            </Descriptions>
            {pool.surplus > 0 && (
              <Alert
                type="warning"
                showIcon
                style={{ marginTop: 16 }}
                message={t('pool.surplusTitle', { count: pool.surplus })}
                description={t('pool.surplusDescription')}
              />
            )}
            <Table
              style={{ marginTop: 16 }}
              rowKey="id"
              size="small"
              pagination={false}
              dataSource={pool.members}
              columns={memberColumns(pool)}
              scroll={{ x: 1120 }}
              locale={{ emptyText: t('pool.noMembers') }}
            />
          </section>
        ))
      )}

      {resources.length > 0 && (
        <section aria-labelledby="recoverable-resources">
          <Title id="recoverable-resources" level={4}>
            {t('pool.recoverableResources')}
          </Title>
          <Paragraph type="secondary">
            {t('pool.recoverableResourcesDescription')}
          </Paragraph>
          <Table
            rowKey="id"
            size="small"
            pagination={false}
            dataSource={resources}
            scroll={{ x: 760 }}
            columns={[
              { title: t('pool.resource'), dataIndex: 'id' },
              {
                title: t('pool.status'),
                dataIndex: 'status',
                render: (value: string) => (
                  <Tag color={statusColor(value)}>{value}</Tag>
                ),
              },
              {
                title: t('pool.cooldownUntil'),
                dataIndex: 'cooldown_until',
                render: (value: string | null) =>
                  value
                    ? formatDateTime(
                        value,
                        i18n.resolvedLanguage ?? i18n.language,
                      )
                    : t('pool.pendingDisconnect'),
              },
              {
                title: t('pool.actions'),
                render: (_: unknown, resource: DBInstanceResourceAdmin) =>
                  resource.actions.restore ? (
                    <Popconfirm
                      title={t('pool.restoreConfirm')}
                      onConfirm={() => void restore(resource)}
                    >
                      <Button size="small">{t('pool.restore')}</Button>
                    </Popconfirm>
                  ) : null,
              },
            ]}
          />
        </section>
      )}

      {permissionPool && (
        <PermissionTemplateDrawer
          open
          poolId={permissionPool.id}
          configRevision={permissionPool.config_revision}
          selectedRevisionId={permissionPool.permission_template_revision_id}
          onPoolUpdated={(updated) => {
            setPools((current) => current.map((pool) =>
              pool.id === updated.id ? updated : pool,
            ))
            setPermissionPool(updated)
          }}
          onClose={() => setPermissionPool(null)}
        />
      )}
    </Space>
  )
}
