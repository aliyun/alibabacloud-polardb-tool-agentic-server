import { useCallback, useEffect, useState } from 'react'
import { Card, Form, Input, InputNumber, Switch, Button, Select, Progress, message, Typography } from 'antd'
import { useTranslation } from 'react-i18next'
import { getSettings, batchUpdateSettings, testCredentials, type SettingItem } from '../../api/settings'
import { getQuotaStatus, updateGlobalQuota, type QuotaStatus } from '../../api/quota'
import PageContainer from '../../components/PageContainer'
import { schemaFieldLabel } from '../../i18n/schema'

const { Text } = Typography

const SECTIONS = {
  credentials: {
    keys: ['aliyun_credential_mode', 'aliyun_access_key_id', 'aliyun_access_key_secret',
           'aliyun_role_arn', 'aliyun_role_session_name', 'aliyun_sts_duration_seconds'],
  },
  quota: { keys: [] as string[] },
  pool: { keys: ['pool_target_size'] },
  network: { keys: ['pool_region_id', 'pool_vpc_id', 'pool_vswitch_id', 'pool_zone_id', 'pool_security_ip_list', 'pool_endpoint_net_type'] },
  cluster: { keys: ['pool_db_type', 'pool_db_version', 'pool_db_minor_version', 'pool_db_node_class', 'pool_proxy_class', 'pool_proxy_type', 'pool_architecture', 'pool_loose_polar_log_bin', 'pool_loose_x_engine', 'pool_pay_type', 'pool_serverless_type', 'pool_scale_min', 'pool_scale_max', 'pool_allow_shut_down', 'pool_scale_ro_num_min', 'pool_scale_ro_num_max', 'pool_storage_type', 'pool_storage_space'] },
  provisioning: { keys: ['provisioning_poll_timeout_seconds', 'retry_after_seconds'] },
}

const SECTION_TITLE_KEYS = {
  credentials: 'settings.cloudCredentials',
  quota: 'settings.quota',
  pool: 'settings.pool',
  network: 'settings.networkConfig',
  cluster: 'settings.clusterSpec',
  provisioning: 'settings.provisioning',
} as const

const ASSUME_ROLE_KEYS = ['aliyun_role_arn', 'aliyun_role_session_name', 'aliyun_sts_duration_seconds']

function QuotaCard() {
  const { t } = useTranslation()
  const [quota, setQuota] = useState<QuotaStatus['global'] | null>(null)
  const [newLimit, setNewLimit] = useState<number | null>(null)
  const [saving, setSaving] = useState(false)

  const fetchQuota = useCallback(async () => {
    try {
      const resp = await getQuotaStatus()
      setQuota(resp.data.global)
      setNewLimit(resp.data.global.limit)
    } catch {
      message.error(t('settings.quotaLoadFailed'))
    }
  }, [t])

  useEffect(() => { void fetchQuota() }, [fetchQuota])

  const handleSave = async () => {
    if (newLimit === null) return
    setSaving(true)
    try {
      await updateGlobalQuota(newLimit)
      message.success(t('settings.quotaUpdated'))
      fetchQuota()
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      message.error(error.response?.data?.detail || t('settings.updateFailed'))
    } finally {
      setSaving(false)
    }
  }

  if (!quota) return <Card title={t('settings.quota')} loading style={{ marginBottom: 16 }} />

  const percent = quota.limit ? Math.round((quota.current / quota.limit) * 100) : 0

  return (
    <Card title={t('settings.quota')} style={{ marginBottom: 16 }}>
      <div style={{ marginBottom: 16 }}>
        <Text>{t('settings.globalInstances', { current: quota.current, limit: quota.limit ?? '∞' })}</Text>
        {quota.limit != null && <Progress percent={percent} size="small" style={{ marginTop: 8 }} />}
      </div>
      <Form layout="inline">
        <Form.Item label={t('settings.maxLimit')}>
          <InputNumber
            value={newLimit}
            onChange={v => setNewLimit(v)}
            min={quota.current}
            style={{ width: 120 }}
          />
        </Form.Item>
        <Form.Item>
          <Button type="primary" onClick={handleSave} loading={saving}>{t('common.update')}</Button>
        </Form.Item>
      </Form>
    </Card>
  )
}

export default function Settings() {
  const { t } = useTranslation()
  const [settings, setSettings] = useState<SettingItem[]>([])
  const [loading, setLoading] = useState(false)
  const [testing, setTesting] = useState(false)
  const [credMode, setCredMode] = useState('direct_ak')
  const [secretMasks, setSecretMasks] = useState<Record<string, string>>({})
  const [form] = Form.useForm()

  const fetchSettings = useCallback(async () => {
    setLoading(true)
    try {
      const resp = await getSettings()
      setSettings(resp.data)
      const values: Record<string, string> = {}
      const masks: Record<string, string> = {}
      resp.data.forEach(s => {
        if (s.type === 'secret') {
          masks[s.key] = s.value
          values[s.key] = ''
        } else {
          values[s.key] = s.value
        }
      })
      setSecretMasks(masks)
      form.setFieldsValue(values)
      const mode = resp.data.find(s => s.key === 'aliyun_credential_mode')
      if (mode) setCredMode(mode.value)
    } finally {
      setLoading(false)
    }
  }, [form])

  useEffect(() => { void fetchSettings() }, [fetchSettings])

  const handleSave = async () => {
    const values = form.getFieldsValue()
    const updates: Record<string, string> = {}
    settings.forEach(s => {
      const newVal = String(values[s.key] ?? '')
      if (s.type === 'secret') {
        if (newVal && !newVal.includes('****')) {
          updates[s.key] = newVal
        }
        return
      }
      if (newVal !== s.value) {
        updates[s.key] = newVal
      }
    })
    if (Object.keys(updates).length === 0) {
      message.info(t('settings.noChanges'))
      return
    }
    try {
      await batchUpdateSettings(updates)
      message.success(t('settings.saved'))
      fetchSettings()
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      message.error(error.response?.data?.detail || t('settings.saveFailed'))
    }
  }

  const handleTestCredentials = async () => {
    setTesting(true)
    try {
      await testCredentials()
      message.success(t('settings.connected'))
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      message.error(error.response?.data?.detail || t('settings.connectionFailed'))
    } finally {
      setTesting(false)
    }
  }

  const renderField = (s: SettingItem) => {
    const label = schemaFieldLabel(t, 'settings', s.key, s.description)
    if (ASSUME_ROLE_KEYS.includes(s.key) && credMode !== 'assume_role') {
      return null
    }

    if (s.key === 'aliyun_credential_mode') {
      return (
        <Form.Item key={s.key} name={s.key} label={label}>
          <Select onChange={(v: string) => setCredMode(v)}>
            <Select.Option value="direct_ak">{t('settings.directAccessKey')}</Select.Option>
            <Select.Option value="assume_role">{t('settings.assumeRole')}</Select.Option>
          </Select>
        </Form.Item>
      )
    }

    if (s.key === 'pool_endpoint_net_type') {
      return (
        <Form.Item key={s.key} name={s.key} label={label}>
          <Select>
            <Select.Option value="Private">{t('settings.privateNetwork')}</Select.Option>
            <Select.Option value="Public">{t('settings.publicNetwork')}</Select.Option>
          </Select>
        </Form.Item>
      )
    }

    if (s.type === 'secret') {
      return (
        <Form.Item key={s.key} name={s.key} label={label}
          extra={<Text type="secondary">{t('settings.secretUnchanged')}{secretMasks[s.key] ? ` (${t('settings.currentSecret', { value: secretMasks[s.key] })})` : ''}</Text>}
        >
          <Input.Password placeholder={secretMasks[s.key] || t('settings.notConfigured')} />
        </Form.Item>
      )
    }

    if (s.type === 'bool') {
      return (
        <Form.Item key={s.key} name={s.key} label={label} valuePropName="checked">
          <Switch checkedChildren="true" unCheckedChildren="false" />
        </Form.Item>
      )
    }

    if (s.type === 'int') {
      return (
        <Form.Item key={s.key} name={s.key} label={label}>
          <InputNumber style={{ width: '100%' }} />
        </Form.Item>
      )
    }

    return (
      <Form.Item key={s.key} name={s.key} label={label} rules={s.required ? [{ required: true }] : []}>
        <Input />
      </Form.Item>
    )
  }

  return (
    <PageContainer
      title={t('settings.title')}
      description={t('settings.description')}
      actions={<Button type="primary" onClick={handleSave} loading={loading}>{t('settings.saveAll')}</Button>}
    >
      <Form form={form} layout="vertical">
        {Object.entries(SECTIONS).map(([sectionKey, section]) => {
          if (sectionKey === 'quota') return <QuotaCard key={sectionKey} />
          if (sectionKey === 'cluster') return null

          const fields = settings.filter(s => section.keys.includes(s.key)).map(renderField).filter(Boolean)

          return (
            <Card key={sectionKey} title={t(SECTION_TITLE_KEYS[sectionKey as keyof typeof SECTION_TITLE_KEYS])} style={{ marginBottom: 16 }}
              extra={sectionKey === 'credentials' ? (
                <Button onClick={handleTestCredentials} loading={testing}>{t('settings.testConnection')}</Button>
              ) : undefined}
            >
              {fields}
            </Card>
          )
        })}
      </Form>
    </PageContainer>
  )
}
