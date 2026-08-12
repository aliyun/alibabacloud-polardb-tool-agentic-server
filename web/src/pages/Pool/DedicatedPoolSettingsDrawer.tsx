import { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Checkbox,
  Col,
  Collapse,
  Drawer,
  Form,
  Input,
  InputNumber,
  Row,
  Select,
  Space,
  message,
} from 'antd'
import { useTranslation } from 'react-i18next'

import { getAPIErrorMessage } from '../../api/client'
import {
  updateDedicatedPool,
  type DedicatedPool,
  type DedicatedPurchaseProfile,
  type UpdateDedicatedPoolInput,
} from '../../api/dedicatedPools'

interface PoolSettingsForm {
  name: string
  target_size: number
  max_total_members: number
  max_member_purchases_per_hour: number
  max_create_requests_per_agent_per_hour: number
  max_delete_requests_per_agent_per_hour: number
  storage_type: string
  region_id: string
  zone_id: string
  vpc_id: string
  vswitch_id: string
  security_ip_list?: string
  network_change_confirmed: boolean
  reclaim_policy: 'destroy' | 'sanitize_and_reuse'
  delete_cooldown_duration_hours?: number
  available_health_check_interval_seconds: number
  available_health_stale_after_seconds: number
}

interface DedicatedPoolSettingsDrawerProps {
  open: boolean
  pool: DedicatedPool
  profile: DedicatedPurchaseProfile | null
  onClose: () => void
  onUpdated: (pool: DedicatedPool) => void
}

export default function DedicatedPoolSettingsDrawer({
  open,
  pool,
  profile,
  onClose,
  onUpdated,
}: DedicatedPoolSettingsDrawerProps) {
  const { t } = useTranslation()
  const [form] = Form.useForm<PoolSettingsForm>()
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const regionId = Form.useWatch('region_id', form)
  const zoneId = Form.useWatch('zone_id', form)
  const vpcId = Form.useWatch('vpc_id', form)
  const vswitchId = Form.useWatch('vswitch_id', form)
  const normalize = (value: string | undefined | null) =>
    (value ?? '').trim()
  const networkChanged = [
    [regionId, pool.region_id],
    [zoneId, pool.zone_id],
    [vpcId, pool.vpc_id],
    [vswitchId, pool.vswitch_id],
  ].some(([value, original]) =>
    value !== undefined && normalize(value) !== normalize(original),
  )

  useEffect(() => {
    if (!open) return
    setError(null)
    form.setFieldsValue({
      name: pool.name,
      target_size: pool.target_size,
      max_total_members: pool.max_total_members,
      max_member_purchases_per_hour: pool.max_member_purchases_per_hour,
      max_create_requests_per_agent_per_hour:
        pool.max_create_requests_per_agent_per_hour,
      max_delete_requests_per_agent_per_hour:
        pool.max_delete_requests_per_agent_per_hour,
      storage_type: pool.storage_type ?? profile?.default_storage_type,
      region_id: pool.region_id,
      zone_id: pool.zone_id ?? '',
      vpc_id: pool.vpc_id,
      vswitch_id: pool.vswitch_id,
      security_ip_list: pool.security_ip_list ?? '',
      network_change_confirmed: false,
      reclaim_policy: pool.reclaim_policy,
      delete_cooldown_duration_hours:
        pool.delete_cooldown_duration_hours ?? undefined,
      available_health_check_interval_seconds:
        pool.available_health_check_interval_seconds,
      available_health_stale_after_seconds:
        pool.available_health_stale_after_seconds,
    })
  }, [form, open, pool, profile])

  const save = async () => {
    let values: PoolSettingsForm
    try {
      values = await form.validateFields()
    } catch {
      return
    }
    setSaving(true)
    setError(null)
    const input: UpdateDedicatedPoolInput = {
      ...values,
      expected_config_revision: pool.config_revision,
      network_change_confirmed:
        networkChanged && values.network_change_confirmed,
      region_id: normalize(values.region_id),
      zone_id: normalize(values.zone_id),
      vpc_id: normalize(values.vpc_id),
      vswitch_id: normalize(values.vswitch_id),
      security_ip_list: normalize(values.security_ip_list) || null,
      delete_cooldown_duration_hours:
        values.delete_cooldown_duration_hours ?? null,
    }
    try {
      const response = await updateDedicatedPool(pool.id, input)
      onUpdated(response.data)
      message.success(t('pool.configurationUpdated'))
      onClose()
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, t('pool.configurationUpdateFailed')),
      )
    } finally {
      setSaving(false)
    }
  }

  return (
    <Drawer
      open={open}
      width={640}
      title={t('pool.editConfiguration')}
      onClose={onClose}
      destroyOnClose
      extra={(
        <Space>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="primary" loading={saving} onClick={() => void save()}>
            {t('common.saveChanges')}
          </Button>
        </Space>
      )}
    >
      <Space direction="vertical" size={20} style={{ width: '100%' }}>
        {error && <Alert type="error" showIcon message={error} />}
        <Form<PoolSettingsForm> form={form} layout="vertical">
          <Form.Item
            name="name"
            label={t('pool.name')}
            rules={[{ required: true, whitespace: true }]}
          >
            <Input maxLength={255} />
          </Form.Item>
          <Row gutter={16}>
            <Col xs={24} md={12}>
              <Form.Item
                name="target_size"
                label={t('pool.prewarmedTarget')}
                rules={[{ required: true }]}
              >
                <InputNumber min={0} precision={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item
                name="max_total_members"
                label={t('pool.maximumBillable')}
                dependencies={['target_size']}
                rules={[
                  { required: true },
                  ({ getFieldValue }) => ({
                    validator(_, value) {
                      return value >= getFieldValue('target_size')
                        ? Promise.resolve()
                        : Promise.reject(
                          new Error(t('pool.maximumBillableError')),
                        )
                    },
                  }),
                ]}
              >
                <InputNumber min={1} precision={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item
            name="storage_type"
            label={t('pool.storageType')}
            rules={[{ required: true }]}
          >
            <Select
              options={(profile?.supported_storage_types ?? []).map((value) => ({
                value,
                label: value,
              }))}
            />
          </Form.Item>
          <Form.Item name="reclaim_policy" label={t('pool.reclaimPolicy')}>
            <Select
              options={[
                { value: 'destroy', label: t('pool.destroyAfterCooldown') },
                { value: 'sanitize_and_reuse', label: t('pool.sanitizeReuse') },
              ]}
            />
          </Form.Item>
          <Collapse
            ghost
            items={[
              {
                key: 'network',
                label: t('pool.advancedNetworkConfiguration'),
                children: (
                  <Space direction="vertical" size={16} style={{ width: '100%' }}>
                    <Alert
                      type="warning"
                      showIcon
                      message={t('pool.networkChangeWarningTitle')}
                      description={t('pool.networkChangeWarningDescription')}
                    />
                    <Row gutter={16}>
                      <Col xs={24} md={12}>
                        <Form.Item
                          name="region_id"
                          label={t('pool.regionId')}
                          rules={[{ required: true, whitespace: true }]}
                        >
                          <Input />
                        </Form.Item>
                      </Col>
                      <Col xs={24} md={12}>
                        <Form.Item
                          name="zone_id"
                          label={t('pool.zoneId')}
                          rules={[{ required: true, whitespace: true }]}
                        >
                          <Input />
                        </Form.Item>
                      </Col>
                      <Col xs={24} md={12}>
                        <Form.Item
                          name="vpc_id"
                          label={t('pool.vpcId')}
                          rules={[{ required: true, whitespace: true }]}
                        >
                          <Input />
                        </Form.Item>
                      </Col>
                      <Col xs={24} md={12}>
                        <Form.Item
                          name="vswitch_id"
                          label={t('pool.vswitchId')}
                          rules={[{ required: true, whitespace: true }]}
                        >
                          <Input />
                        </Form.Item>
                      </Col>
                    </Row>
                    <Form.Item
                      name="security_ip_list"
                      label={t('pool.pasPrivateSourceCidrs')}
                      extra={t('pool.pasPrivateSourceCidrsUpdateHint')}
                    >
                      <Input placeholder="10.0.0.0/24" />
                    </Form.Item>
                    {networkChanged && (
                      <Form.Item
                        name="network_change_confirmed"
                        valuePropName="checked"
                        rules={[
                          {
                            validator: (_, value) => value
                              ? Promise.resolve()
                              : Promise.reject(
                                new Error(t('pool.networkChangeConfirmationRequired')),
                              ),
                          },
                        ]}
                      >
                        <Checkbox>
                          {t('pool.networkChangeConfirmation')}
                        </Checkbox>
                      </Form.Item>
                    )}
                  </Space>
                ),
              },
              {
                key: 'cost-protection',
                label: t('pool.advancedCostProtection'),
                children: (
                  <Row gutter={16}>
                    <Col xs={24} lg={8}>
                      <Form.Item
                        name="max_member_purchases_per_hour"
                        label={t('pool.purchaseBudget')}
                        rules={[{ required: true }]}
                      >
                        <InputNumber min={1} precision={0} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} lg={8}>
                      <Form.Item
                        name="max_create_requests_per_agent_per_hour"
                        label={t('pool.createBudget')}
                        rules={[{ required: true }]}
                      >
                        <InputNumber min={1} precision={0} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} lg={8}>
                      <Form.Item
                        name="max_delete_requests_per_agent_per_hour"
                        label={t('pool.deleteBudget')}
                        rules={[{ required: true }]}
                      >
                        <InputNumber min={1} precision={0} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                  </Row>
                ),
              },
              {
                key: 'lifecycle',
                label: t('pool.advancedLifecycle'),
                children: (
                  <>
                    <Form.Item
                      name="delete_cooldown_duration_hours"
                      label={t('pool.defaultCooldown')}
                      extra={t('pool.cooldownInheritanceHint')}
                    >
                      <InputNumber min={1} precision={0} style={{ width: '100%' }} />
                    </Form.Item>
                    <Row gutter={16}>
                      <Col xs={24} md={12}>
                        <Form.Item
                          name="available_health_check_interval_seconds"
                          label={t('pool.healthInterval')}
                          rules={[{ required: true }]}
                        >
                          <InputNumber min={1} precision={0} style={{ width: '100%' }} />
                        </Form.Item>
                      </Col>
                      <Col xs={24} md={12}>
                        <Form.Item
                          name="available_health_stale_after_seconds"
                          label={t('pool.healthMaxAge')}
                          dependencies={['available_health_check_interval_seconds']}
                          rules={[
                            { required: true },
                            ({ getFieldValue }) => ({
                              validator(_, value) {
                                const interval = getFieldValue(
                                  'available_health_check_interval_seconds',
                                )
                                return value >= 2 * interval
                                  ? Promise.resolve()
                                  : Promise.reject(
                                    new Error(t('pool.healthMaxAgeHint')),
                                  )
                              },
                            }),
                          ]}
                        >
                          <InputNumber min={2} precision={0} style={{ width: '100%' }} />
                        </Form.Item>
                      </Col>
                    </Row>
                  </>
                ),
              },
            ]}
          />
        </Form>
      </Space>
    </Drawer>
  )
}
