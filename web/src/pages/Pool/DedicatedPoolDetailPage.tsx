import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Descriptions,
  Empty,
  Modal,
  Skeleton,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import {
  ArrowLeftOutlined,
  CheckCircleOutlined,
  ExclamationCircleOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
} from '@ant-design/icons'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { getAPIErrorMessage } from '../../api/client'
import {
  getDedicatedPool,
  getDedicatedPurchaseProfile,
  getDedicatedReadiness,
  upgradeDedicatedPurchaseProfile,
  type DedicatedPool,
  type DedicatedPoolMember,
  type DedicatedPurchaseProfile,
  type DedicatedReadiness,
} from '../../api/dedicatedPools'
import PageContainer from '../../components/PageContainer'
import DedicatedPoolSettingsDrawer from './DedicatedPoolSettingsDrawer'
import PermissionTemplateDrawer from './PermissionTemplateDrawer'

const { Text, Title } = Typography

const PREPARATION_STEPS = [
  'pending',
  'purchase_intent_stored',
  'purchase_requested',
  'cluster_ready',
  'endpoint_resolved',
  'lifecycle_account_stored',
  'lifecycle_account_created',
  'sandbox_account_stored',
  'sandbox_account_created',
  'database_created',
  'openapi_ready',
  'privileges_granted',
  'verified',
]

function supplyColor(state: DedicatedPool['supply_state']) {
  if (state === 'ready') return 'success'
  if (state === 'prewarming' || state === 'partially_ready') return 'processing'
  if (state === 'not_started' || state === 'capacity_limited') return 'warning'
  return 'error'
}

export default function DedicatedPoolDetailPage() {
  const { t } = useTranslation()
  const { poolId = '' } = useParams<{ poolId: string }>()
  const navigate = useNavigate()
  const [pool, setPool] = useState<DedicatedPool | null>(null)
  const [readiness, setReadiness] = useState<DedicatedReadiness | null>(null)
  const [profile, setProfile] = useState<DedicatedPurchaseProfile | null>(null)
  const [loading, setLoading] = useState(true)
  const [upgrading, setUpgrading] = useState(false)
  const [upgradeOpen, setUpgradeOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [permissionsOpen, setPermissionsOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [poolResponse, readinessResponse, profileResponse] =
        await Promise.all([
          getDedicatedPool(poolId),
          getDedicatedReadiness(),
          getDedicatedPurchaseProfile(),
        ])
      setPool(poolResponse.data)
      setReadiness(readinessResponse.data)
      setProfile(profileResponse.data)
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('pool.detailLoadFailed')))
    } finally {
      setLoading(false)
    }
  }, [poolId, t])

  useEffect(() => {
    void load()
  }, [load])

  const upgrade = async () => {
    if (!pool) return
    setUpgrading(true)
    setError(null)
    try {
      const response = await upgradeDedicatedPurchaseProfile(
        pool.id,
        pool.config_revision,
      )
      setPool(response.data)
      setUpgradeOpen(false)
      message.success(t('pool.profileUpgraded'))
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('pool.profileUpgradeFailed')))
    } finally {
      setUpgrading(false)
    }
  }

  if (loading) {
    return (
      <PageContainer title={t('pool.detailTitle')}>
        <Skeleton active paragraph={{ rows: 12 }} />
      </PageContainer>
    )
  }

  if (!pool) {
    return (
      <PageContainer title={t('pool.detailTitle')}>
        <Alert
          type="error"
          showIcon
          message={error ?? t('pool.detailNotFound')}
          action={<Button onClick={() => void load()}>{t('common.retry')}</Button>}
        />
      </PageContainer>
    )
  }

  const waitingForPrivateDataPlane = pool.members.some(
    (member) => member.preparation_step === 'openapi_ready',
  )

  return (
    <PageContainer
      title={pool.name}
      description={t('pool.detailDescription')}
      actions={(
        <Space wrap>
          <Button
            icon={<SafetyCertificateOutlined />}
            onClick={() => setPermissionsOpen(true)}
          >
            {t('pool.manageAgentPermissions')}
          </Button>
          <Button icon={<SettingOutlined />} onClick={() => setSettingsOpen(true)}>
            {t('pool.editConfiguration')}
          </Button>
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/pool')}>
            {t('pool.backToPools')}
          </Button>
        </Space>
      )}
    >
      <Space direction="vertical" size={20} style={{ width: '100%' }}>
        {error && <Alert type="error" showIcon role="alert" message={error} />}
        {readiness?.simulation_mode && (
          <Alert
            type="warning"
            showIcon
            message={t('pool.detailSimulationTitle')}
            description={t('pool.simulationDescription')}
          />
        )}
        {readiness && (
          <Alert
            type={readiness.preparation_mode === 'openapi_only'
              || waitingForPrivateDataPlane
              ? 'warning'
              : 'info'}
            showIcon
            message={readiness.preparation_mode === 'openapi_only'
              ? t('pool.openapiOnlyTitle')
              : waitingForPrivateDataPlane
                ? t('pool.privateDataPlanePendingTitle')
                : t('pool.preparationModeCurrent', {
                  mode: t('pool.preparationModeValues.full'),
                })}
            description={(
              <Space direction="vertical" size={4}>
                <Text>
                  {readiness.preparation_mode === 'openapi_only'
                    ? t('pool.openapiOnlyDescription')
                    : waitingForPrivateDataPlane
                      ? t('pool.privateDataPlanePendingDescription')
                      : t('pool.fullPreparationDescription')}
                </Text>
                <Text type="secondary">
                  {t('pool.preparationModeGlobalHint')}
                </Text>
              </Space>
            )}
            action={(
              <Link to="/settings/configuration?module=runtime_policy&field=dedicated_pool_preparation_mode">
                {t('pool.configurePreparationMode')}
              </Link>
            )}
          />
        )}
        {pool.purchase_profile_status === 'upgrade_required' && (
          <Alert
            type="warning"
            showIcon
            message={t('pool.profileUpgradeRequired')}
            description={t('pool.profileUpgradeDescription')}
            action={(
              <Button onClick={() => setUpgradeOpen(true)}>
                {t('pool.reviewUpgrade')}
              </Button>
            )}
          />
        )}

        <section aria-labelledby="pool-state-heading">
          <Title id="pool-state-heading" level={4}>
            {t('pool.stateAndCapacity')}
          </Title>
          <Space wrap>
            <Tag icon={<CheckCircleOutlined />}>
              {t('pool.configurationBadge', {
                status: t(`pool.configurationStates.${pool.status}`),
              })}
            </Tag>
            <Tag
              icon={pool.supply_state === 'ready'
                ? <CheckCircleOutlined />
                : <ExclamationCircleOutlined />}
              color={supplyColor(pool.supply_state)}
            >
              {t('pool.supplyBadge', {
                state: t(`pool.supplyStates.${pool.supply_state}`),
              })}
            </Tag>
            <Text strong>
              {t('pool.readyCapacity', {
                current: pool.supply_current,
                target: pool.supply_target,
              })}
            </Text>
          </Space>
          {pool.blocking_reasons.length > 0 && (
            <Alert
              type="warning"
              showIcon
              style={{ marginTop: 16 }}
              message={t('pool.currentBlockers')}
              description={(
                <ul>
                  {pool.blocking_reasons.map((code) => (
                    <li key={code}>{t(`pool.blockingReasons.${code}`)}</li>
                  ))}
                </ul>
              )}
            />
          )}
        </section>

        <section aria-labelledby="pool-config-heading">
          <Title id="pool-config-heading" level={4}>
            {t('pool.configuration')}
          </Title>
          <Descriptions bordered size="small" column={{ xs: 1, md: 2 }}>
            <Descriptions.Item label={t('pool.prewarmedTarget')}>
              {pool.target_size}
            </Descriptions.Item>
            <Descriptions.Item label={t('pool.maximumBillable')}>
              {pool.billable_total} / {pool.max_total_members}
            </Descriptions.Item>
            <Descriptions.Item label={t('pool.regionId')}>
              <Text copyable>{pool.region_id}</Text>
            </Descriptions.Item>
            <Descriptions.Item label={t('pool.zoneId')}>
              <Text copyable>{pool.zone_id ?? '—'}</Text>
            </Descriptions.Item>
            <Descriptions.Item label={t('pool.vpcId')}>
              <Text copyable>{pool.vpc_id}</Text>
            </Descriptions.Item>
            <Descriptions.Item label={t('pool.vswitchId')}>
              <Text copyable>{pool.vswitch_id}</Text>
            </Descriptions.Item>
            <Descriptions.Item label={t('pool.pasPrivateSourceCidrs')}>
              <Text copyable>{pool.security_ip_list ?? '—'}</Text>
            </Descriptions.Item>
            <Descriptions.Item label={t('pool.storageType')}>
              {pool.storage_type ?? t('pool.profileUpgradeRequiredShort')}
            </Descriptions.Item>
            <Descriptions.Item label={t('pool.purchaseProfilePrerequisite')}>
              {pool.purchase_profile_id
                ? `${pool.purchase_profile_id} · v${pool.purchase_profile_revision}`
                : t('pool.profileUpgradeRequiredShort')}
            </Descriptions.Item>
            <Descriptions.Item label={t('pool.agentDefaultPermissions')}>
              <Text copyable>{pool.permission_template_revision_id}</Text>
            </Descriptions.Item>
            <Descriptions.Item label={t('pool.preparationMode')}>
              {readiness
                ? t(`pool.preparationModeValues.${readiness.preparation_mode}`)
                : '—'}
            </Descriptions.Item>
            <Descriptions.Item label={t('pool.reclaimPolicy')}>
              {t(`pool.reclaimPolicies.${pool.reclaim_policy}`)}
            </Descriptions.Item>
          </Descriptions>
        </section>

        <section aria-labelledby="pool-routes-heading">
          <Title id="pool-routes-heading" level={4}>
            {t('pool.agentRoutesTitle')}
          </Title>
          {pool.route_usage.length === 0 ? (
            <Alert
              type="info"
              showIcon
              message={t('pool.unboundPoolTitle')}
              description={t('pool.unboundPoolDescription')}
            />
          ) : (
            <Table
              rowKey="binding_id"
              size="small"
              pagination={false}
              dataSource={pool.route_usage}
              columns={[
                { title: t('agents.agent'), dataIndex: 'agent_name' },
                {
                  title: t('pool.routeRole'),
                  dataIndex: 'role',
                  render: (role: string) => t(`pool.routeRoles.${role}`),
                },
                {
                  title: t('pool.actions'),
                  render: (_value, route) => (
                    <Button
                      size="small"
                      onClick={() => navigate(`/agents/${encodeURIComponent(route.agent_id)}#dedicated-pool-routes`)}
                    >
                      {t('pool.openAgentRoutes')}
                    </Button>
                  ),
                },
              ]}
            />
          )}
        </section>

        <section aria-labelledby="pool-members-heading">
          <Title id="pool-members-heading" level={4}>
            {t('pool.membersAndPreparation')}
          </Title>
          <Table
            rowKey="id"
            size="small"
            pagination={false}
            dataSource={pool.members}
            locale={{ emptyText: <Empty description={t('pool.noMembers')} /> }}
            scroll={{ x: 940 }}
            columns={[
              {
                title: t('pool.instance'),
                dataIndex: 'instance_id',
                render: (value: string) => <Text code copyable>{value}</Text>,
              },
              {
                title: t('pool.status'),
                dataIndex: 'status',
                render: (value: string) => <Tag>{value}</Tag>,
              },
              {
                title: t('pool.preparation'),
                dataIndex: 'preparation_step',
                render: (value: string) => (
                  <Space direction="vertical" size={0}>
                    <Text>{t(`pool.preparationSteps.${value}`)}</Text>
                    <Text type="secondary">
                      {t('pool.preparationProgress', {
                        current: Math.max(1, PREPARATION_STEPS.indexOf(value) + 1),
                        total: PREPARATION_STEPS.length,
                      })}
                    </Text>
                  </Space>
                ),
              },
              {
                title: t('pool.cloudRequestId'),
                dataIndex: 'cloud_request_id',
                render: (
                  value: string | null,
                  member: DedicatedPoolMember,
                ) => {
                  if (!value) return '—'
                  const hasDiagnostics = Boolean(
                    member.failure_operation
                    || member.failure_occurred_at
                    || member.failure_detail,
                  )
                  const requestId = <Text code copyable>{value}</Text>
                  if (!hasDiagnostics) return requestId
                  return (
                    <Tooltip
                      title={(
                        <Space direction="vertical" size={2}>
                          {member.failure_operation && (
                            <span>{t('pool.cloudFailureOperation', {
                              operation: member.failure_operation,
                            })}</span>
                          )}
                          {member.failure_occurred_at && (
                            <span>{t('pool.cloudFailureTime', {
                              time: member.failure_occurred_at,
                            })}</span>
                          )}
                          {member.failure_detail && (
                            <span>{t('pool.cloudFailureDetail', {
                              detail: member.failure_detail,
                            })}</span>
                          )}
                        </Space>
                      )}
                    >
                      <span>{requestId}</span>
                    </Tooltip>
                  )
                },
              },
              {
                title: t('pool.failureCode'),
                dataIndex: 'failure_reason',
                render: (value: string | null) => value
                  ? <Text type="danger" code>{value}</Text>
                  : '—',
              },
            ]}
          />
        </section>
      </Space>

      <Modal
        title={t('pool.profileUpgradeTitle')}
        open={upgradeOpen}
        okText={t('pool.confirmUpgrade')}
        confirmLoading={upgrading}
        onOk={() => void upgrade()}
        onCancel={() => setUpgradeOpen(false)}
      >
        <Alert
          type="warning"
          showIcon
          message={t('pool.profileUpgradePurchaseWarning')}
        />
        <Descriptions size="small" bordered column={1} style={{ marginTop: 16 }}>
          <Descriptions.Item label={t('pool.databaseMinorVersion')}>
            {profile?.fixed_parameters.db_minor_version ?? '8.0.2'}
          </Descriptions.Item>
          <Descriptions.Item label={t('pool.storageType')}>
            {profile?.default_storage_type ?? 'essdpl1'}
          </Descriptions.Item>
          <Descriptions.Item label={t('pool.purchaseProfilePrerequisite')}>
            {profile ? `${profile.profile_id} · v${profile.revision}` : '—'}
          </Descriptions.Item>
        </Descriptions>
      </Modal>

      <DedicatedPoolSettingsDrawer
        open={settingsOpen}
        pool={pool}
        profile={profile}
        onClose={() => setSettingsOpen(false)}
        onUpdated={setPool}
      />
      <PermissionTemplateDrawer
        open={permissionsOpen}
        poolId={pool.id}
        configRevision={pool.config_revision}
        selectedRevisionId={pool.permission_template_revision_id}
        onPoolUpdated={setPool}
        onClose={() => setPermissionsOpen(false)}
      />
    </PageContainer>
  )
}
