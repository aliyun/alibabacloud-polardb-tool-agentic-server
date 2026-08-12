import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Empty,
  Modal,
  Select,
  Skeleton,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  PauseCircleOutlined,
  StarOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { getAPIErrorMessage } from '../../api/client'
import { listDedicatedPools, type DedicatedPool } from '../../api/dedicatedPools'
import {
  createProvisioningBinding,
  deleteProvisioningBinding,
  listProvisioningBindings,
  reorderDedicatedProvisioningBindings,
  updateProvisioningBinding,
  type AgentResource,
  type ProvisioningBinding,
} from '../../api/instanceAccess'
import { type ProvisioningBackend } from '../../api/provisioningBackends'

const { Paragraph, Text, Title } = Typography

type Confirmation =
  | { kind: 'primary'; binding: ProvisioningBinding }
  | { kind: 'unlink'; binding: ProvisioningBinding }
  | null

interface DedicatedPoolRoutesProps {
  agentId: string
  resources: AgentResource[]
  backends: ProvisioningBackend[]
}

export default function DedicatedPoolRoutes({
  agentId,
  resources,
  backends,
}: DedicatedPoolRoutesProps) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [bindings, setBindings] = useState<ProvisioningBinding[]>([])
  const [pools, setPools] = useState<DedicatedPool[]>([])
  const [selectedPoolId, setSelectedPoolId] = useState<string>()
  const [confirmation, setConfirmation] = useState<Confirmation>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [bindingResponse, poolResponse] =
        await Promise.all([
          listProvisioningBindings(agentId),
          listDedicatedPools(),
        ])
      setBindings(bindingResponse.data)
      setPools(poolResponse.data)
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('agentDetail.routesLoadFailed')))
    } finally {
      setLoading(false)
    }
  }, [agentId, t])

  useEffect(() => {
    void load()
  }, [load])

  const dedicatedBindings = useMemo(
    () => bindings
      .filter((binding) => binding.backend_type === 'dedicated_pool')
      .sort((left, right) => {
        if (left.enabled !== right.enabled) return left.enabled ? -1 : 1
        return (left.routing_order ?? Number.MAX_SAFE_INTEGER)
          - (right.routing_order ?? Number.MAX_SAFE_INTEGER)
      }),
    [bindings],
  )
  const enabledBindings = dedicatedBindings.filter((binding) => binding.enabled)
  const poolById = useMemo(
    () => new Map(pools.map((pool) => [pool.id, pool])),
    [pools],
  )
  const boundPoolIds = new Set(
    dedicatedBindings.map((binding) => binding.dedicated_pool_id),
  )
  const availablePools = pools.filter((pool) => !boundPoolIds.has(pool.id))

  const applyOrder = async (ordered: ProvisioningBinding[]) => {
    setBusy(true)
    setError(null)
    try {
      const response = await reorderDedicatedProvisioningBindings(
        agentId,
        ordered.map((binding) => binding.id),
      )
      const canonical = response.data
      setBindings((current) => [
        ...canonical,
        ...current.filter(
          (binding) => !canonical.some((item) => item.id === binding.id),
        ),
      ])
      message.success(t('agentDetail.routeOrderSaved'))
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('agentDetail.routeSaveFailed')))
    } finally {
      setBusy(false)
    }
  }

  const setPrimary = async (binding: ProvisioningBinding) => {
    await applyOrder([
      binding,
      ...enabledBindings.filter((item) => item.id !== binding.id),
    ])
    setConfirmation(null)
  }

  const move = async (binding: ProvisioningBinding, offset: -1 | 1) => {
    const index = enabledBindings.findIndex((item) => item.id === binding.id)
    const target = index + offset
    if (target < 1 || target >= enabledBindings.length) return
    const ordered = [...enabledBindings]
    ;[ordered[index], ordered[target]] = [ordered[target], ordered[index]]
    await applyOrder(ordered)
  }

  const pause = async (binding: ProvisioningBinding) => {
    setBusy(true)
    setError(null)
    try {
      const response = await updateProvisioningBinding(agentId, binding.id, false)
      setBindings((current) => current.map((item) =>
        item.id === binding.id ? response.data : item,
      ))
      await load()
      message.success(t('agentDetail.routePaused'))
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('agentDetail.routeSaveFailed')))
    } finally {
      setBusy(false)
    }
  }

  const resume = async (binding: ProvisioningBinding) => {
    setBusy(true)
    setError(null)
    try {
      await updateProvisioningBinding(agentId, binding.id, true)
      await load()
      message.success(t('agentDetail.routeResumed'))
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('agentDetail.routeSaveFailed')))
    } finally {
      setBusy(false)
    }
  }

  const resourceCount = (binding: ProvisioningBinding) => resources.filter(
    (resource) => resource.backend_id === binding.backend_id
      && resource.status !== 'deleted',
  ).length

  const unlink = async (binding: ProvisioningBinding) => {
    if (resourceCount(binding) > 0) return
    setBusy(true)
    setError(null)
    try {
      await deleteProvisioningBinding(agentId, binding.id)
      setBindings((current) => current.filter((item) => item.id !== binding.id))
      setConfirmation(null)
      message.success(t('agentDetail.routeUnlinked'))
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('agentDetail.routeSaveFailed')))
    } finally {
      setBusy(false)
    }
  }

  const addRoute = async () => {
    const backend = backends.find(
      (item) => item.dedicated_pool_id === selectedPoolId,
    )
    if (!backend) return
    setBusy(true)
    setError(null)
    try {
      const response = await createProvisioningBinding(agentId, {
        backend_id: backend.id,
        enabled: true,
      })
      setBindings((current) => [...current, response.data])
      setSelectedPoolId(undefined)
      message.success(t('agentDetail.routeAdded'))
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('agentDetail.routeSaveFailed')))
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <Skeleton active paragraph={{ rows: 5 }} />

  return (
    <section id="dedicated-pool-routes" aria-labelledby="dedicated-routes-heading">
      <Title id="dedicated-routes-heading" level={4}>
        {t('agentDetail.dedicatedRoutesTitle')}
      </Title>
      <Paragraph type="secondary">
        {t('agentDetail.dedicatedRoutesDescription')}
      </Paragraph>
      {error && <Alert type="error" showIcon role="alert" message={error} />}

      <Space wrap style={{ marginBlock: 12 }}>
        <Select
          style={{ minWidth: 240 }}
          value={selectedPoolId}
          placeholder={t('agentDetail.selectDedicatedPool')}
          onChange={setSelectedPoolId}
          options={availablePools.map((pool) => ({
            value: pool.id,
            label: `${pool.name} · ${pool.region_id}`,
          }))}
        />
        <Button
          type="primary"
          disabled={!selectedPoolId}
          loading={busy}
          onClick={() => void addRoute()}
        >
          {enabledBindings.length === 0
            ? t('agentDetail.addPrimaryRoute')
            : t('agentDetail.appendFallbackRoute')}
        </Button>
      </Space>

      <Table
        rowKey="id"
        size="small"
        pagination={false}
        dataSource={dedicatedBindings}
        locale={{
          emptyText: (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={t('agentDetail.noDedicatedRoutes')}
            />
          ),
        }}
        scroll={{ x: 1080 }}
        columns={[
          {
            title: t('pool.routeRole'),
            key: 'role',
            render: (_value, binding) => binding.enabled
              ? binding.routing_order === 0
                ? <Tag color="blue">{t('pool.routeRoles.primary')}</Tag>
                : <Tag>{t('agentDetail.fallbackNumber', { count: binding.routing_order })}</Tag>
              : <Tag>{t('pool.routeRoles.paused')}</Tag>,
          },
          {
            title: t('agentDetail.pool'),
            key: 'pool',
            render: (_value, binding) => {
              const pool = poolById.get(binding.dedicated_pool_id ?? '')
              return pool?.name ?? binding.dedicated_pool_id ?? '—'
            },
          },
          {
            title: t('pool.region'),
            key: 'region',
            render: (_value, binding) =>
              poolById.get(binding.dedicated_pool_id ?? '')?.region_id ?? '—',
          },
          {
            title: t('agentDetail.readyCapacity'),
            key: 'capacity',
            render: (_value, binding) => {
              const pool = poolById.get(binding.dedicated_pool_id ?? '')
              return pool
                ? t('pool.readyCapacity', {
                    current: pool.supply_current,
                    target: pool.supply_target,
                  })
                : '—'
            },
          },
          {
            title: t('agentDetail.supplyState'),
            key: 'supply',
            render: (_value, binding) => {
              const pool = poolById.get(binding.dedicated_pool_id ?? '')
              return pool ? t(`pool.supplyStates.${pool.supply_state}`) : '—'
            },
          },
          {
            title: t('pool.actions'),
            key: 'actions',
            render: (_value, binding) => {
              const pool = poolById.get(binding.dedicated_pool_id ?? '')
              const index = enabledBindings.findIndex((item) => item.id === binding.id)
              return (
                <Space wrap>
                  {binding.enabled && binding.routing_order !== 0 && (
                    <Button
                      size="small"
                      icon={<StarOutlined />}
                      aria-label={t('agentDetail.setPoolPrimaryLabel', { pool: pool?.name ?? binding.id })}
                      onClick={() => setConfirmation({ kind: 'primary', binding })}
                    >
                      {t('agentDetail.setPrimary')}
                    </Button>
                  )}
                  {binding.enabled && binding.routing_order !== 0 && (
                    <>
                      <Button
                        size="small"
                        icon={<ArrowUpOutlined />}
                        aria-label={t('agentDetail.moveRouteUp', { pool: pool?.name ?? binding.id })}
                        disabled={index <= 1}
                        onClick={() => void move(binding, -1)}
                      />
                      <Button
                        size="small"
                        icon={<ArrowDownOutlined />}
                        aria-label={t('agentDetail.moveRouteDown', { pool: pool?.name ?? binding.id })}
                        disabled={index >= enabledBindings.length - 1}
                        onClick={() => void move(binding, 1)}
                      />
                    </>
                  )}
                  {binding.enabled ? (
                    <Button
                      size="small"
                      icon={<PauseCircleOutlined />}
                      onClick={() => void pause(binding)}
                    >
                      {t('agentDetail.pauseRoute')}
                    </Button>
                  ) : (
                    <Button size="small" onClick={() => void resume(binding)}>
                      {t('agentDetail.resumeRoute')}
                    </Button>
                  )}
                  <Button
                    size="small"
                    onClick={() => pool && navigate(`/pool/dedicated/${encodeURIComponent(pool.id)}`)}
                  >
                    {t('agentDetail.openPool')}
                  </Button>
                  <Button
                    size="small"
                    danger
                    aria-label={t('agentDetail.unlinkPoolLabel', { pool: pool?.name ?? binding.id })}
                    onClick={() => setConfirmation({ kind: 'unlink', binding })}
                  >
                    {t('agentDetail.unlinkRoute')}
                  </Button>
                </Space>
              )
            },
          },
        ]}
      />

      <Modal
        title={confirmation?.kind === 'primary'
          ? t('agentDetail.setPrimaryTitle')
          : t('agentDetail.unlinkRouteTitle')}
        open={confirmation !== null}
        okText={confirmation?.kind === 'primary'
          ? t('agentDetail.confirmSetPrimary')
          : t('agentDetail.confirmUnlinkRoute')}
        okButtonProps={{
          danger: confirmation?.kind === 'unlink',
          disabled: confirmation?.kind === 'unlink'
            && resourceCount(confirmation.binding) > 0,
        }}
        confirmLoading={busy}
        onOk={() => {
          if (confirmation?.kind === 'primary') void setPrimary(confirmation.binding)
          if (confirmation?.kind === 'unlink') void unlink(confirmation.binding)
        }}
        onCancel={() => setConfirmation(null)}
      >
        {confirmation?.kind === 'primary' && (
          <Alert
            type="warning"
            showIcon
            message={t('agentDetail.primaryReplacementWarning')}
            description={t('agentDetail.primaryReplacementDescription')}
          />
        )}
        {confirmation?.kind === 'unlink' && (
          resourceCount(confirmation.binding) > 0 ? (
            <Alert
              type="warning"
              showIcon
              message={t('agentDetail.routeHasResources', {
                count: resourceCount(confirmation.binding),
              })}
              description={t('agentDetail.routeHasResourcesDescription')}
            />
          ) : (
            <Text>{t('agentDetail.unlinkRouteDescription')}</Text>
          )
        )}
      </Modal>
    </section>
  )
}
