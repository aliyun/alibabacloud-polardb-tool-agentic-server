import { useCallback, useEffect, useState } from 'react'
import { Card, Form, Input, InputNumber, Switch, Button, Select, Progress, message, Typography } from 'antd'
import { getSettings, batchUpdateSettings, testCredentials, type SettingItem } from '../../api/settings'
import { getQuotaStatus, updateGlobalQuota, type QuotaStatus } from '../../api/quota'
import PageContainer from '../../components/PageContainer'

const { Text } = Typography

const SECTIONS = {
  credentials: {
    title: 'Cloud Credentials',
    keys: ['aliyun_credential_mode', 'aliyun_access_key_id', 'aliyun_access_key_secret',
           'aliyun_role_arn', 'aliyun_role_session_name', 'aliyun_sts_duration_seconds'],
  },
  quota: { title: 'Quota', keys: [] as string[] },
  pool: { title: 'Pool', keys: ['pool_target_size'] },
  network: { title: 'Network Config', keys: ['pool_region_id', 'pool_vpc_id', 'pool_vswitch_id', 'pool_zone_id', 'pool_security_ip_list', 'pool_endpoint_net_type'] },
  cluster: { title: 'Cluster Spec', keys: ['pool_db_type', 'pool_db_version', 'pool_db_minor_version', 'pool_db_node_class', 'pool_proxy_class', 'pool_proxy_type', 'pool_architecture', 'pool_loose_polar_log_bin', 'pool_loose_x_engine', 'pool_pay_type', 'pool_serverless_type', 'pool_scale_min', 'pool_scale_max', 'pool_allow_shut_down', 'pool_scale_ro_num_min', 'pool_scale_ro_num_max', 'pool_storage_type', 'pool_storage_space'] },
  provisioning: { title: 'Provisioning', keys: ['provisioning_poll_timeout_seconds', 'retry_after_seconds'] },
}

const ASSUME_ROLE_KEYS = ['aliyun_role_arn', 'aliyun_role_session_name', 'aliyun_sts_duration_seconds']

function QuotaCard() {
  const [quota, setQuota] = useState<QuotaStatus['global'] | null>(null)
  const [newLimit, setNewLimit] = useState<number | null>(null)
  const [saving, setSaving] = useState(false)

  const fetchQuota = async () => {
    try {
      const resp = await getQuotaStatus()
      setQuota(resp.data.global)
      setNewLimit(resp.data.global.limit)
    } catch {
      message.error('Failed to load quota')
    }
  }

  useEffect(() => { fetchQuota() }, [])

  const handleSave = async () => {
    if (newLimit === null) return
    setSaving(true)
    try {
      await updateGlobalQuota(newLimit)
      message.success('Quota updated')
      fetchQuota()
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      message.error(error.response?.data?.detail || 'Update failed')
    } finally {
      setSaving(false)
    }
  }

  if (!quota) return <Card title="Quota" loading style={{ marginBottom: 16 }} />

  const percent = quota.limit ? Math.round((quota.current / quota.limit) * 100) : 0

  return (
    <Card title="Quota" style={{ marginBottom: 16 }}>
      <div style={{ marginBottom: 16 }}>
        <Text>Global instances: {quota.current} / {quota.limit ?? '∞'}</Text>
        {quota.limit != null && <Progress percent={percent} size="small" style={{ marginTop: 8 }} />}
      </div>
      <Form layout="inline">
        <Form.Item label="Max limit">
          <InputNumber
            value={newLimit}
            onChange={v => setNewLimit(v)}
            min={quota.current}
            style={{ width: 120 }}
          />
        </Form.Item>
        <Form.Item>
          <Button type="primary" onClick={handleSave} loading={saving}>Update</Button>
        </Form.Item>
      </Form>
    </Card>
  )
}

export default function Settings() {
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
      message.info('No changes')
      return
    }
    try {
      await batchUpdateSettings(updates)
      message.success('Settings saved')
      fetchSettings()
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      message.error(error.response?.data?.detail || 'Save failed')
    }
  }

  const handleTestCredentials = async () => {
    setTesting(true)
    try {
      await testCredentials()
      message.success('Connected successfully')
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      message.error(error.response?.data?.detail || 'Connection failed')
    } finally {
      setTesting(false)
    }
  }

  const renderField = (s: SettingItem) => {
    if (ASSUME_ROLE_KEYS.includes(s.key) && credMode !== 'assume_role') {
      return null
    }

    if (s.key === 'aliyun_credential_mode') {
      return (
        <Form.Item key={s.key} name={s.key} label={s.description}>
          <Select onChange={(v: string) => setCredMode(v)}>
            <Select.Option value="direct_ak">Direct Access Key</Select.Option>
            <Select.Option value="assume_role">STS AssumeRole</Select.Option>
          </Select>
        </Form.Item>
      )
    }

    if (s.key === 'pool_endpoint_net_type') {
      return (
        <Form.Item key={s.key} name={s.key} label={s.description}>
          <Select>
            <Select.Option value="Private">Private</Select.Option>
            <Select.Option value="Public">Public</Select.Option>
          </Select>
        </Form.Item>
      )
    }

    if (s.type === 'secret') {
      return (
        <Form.Item key={s.key} name={s.key} label={s.description}
          extra={<Text type="secondary">Leave empty to keep current value unchanged{secretMasks[s.key] ? ` (current: ${secretMasks[s.key]})` : ''}</Text>}
        >
          <Input.Password placeholder={secretMasks[s.key] || 'Not configured'} />
        </Form.Item>
      )
    }

    if (s.type === 'bool') {
      return (
        <Form.Item key={s.key} name={s.key} label={s.description} valuePropName="checked">
          <Switch checkedChildren="true" unCheckedChildren="false" />
        </Form.Item>
      )
    }

    if (s.type === 'int') {
      return (
        <Form.Item key={s.key} name={s.key} label={s.description}>
          <InputNumber style={{ width: '100%' }} />
        </Form.Item>
      )
    }

    return (
      <Form.Item key={s.key} name={s.key} label={s.description} rules={s.required ? [{ required: true }] : []}>
        <Input />
      </Form.Item>
    )
  }

  return (
    <PageContainer
      title="System Settings"
      description="Global configuration for provisioning and pool"
      actions={<Button type="primary" onClick={handleSave} loading={loading}>Save All</Button>}
    >
      <Form form={form} layout="vertical">
        {Object.entries(SECTIONS).map(([sectionKey, section]) => {
          if (sectionKey === 'quota') return <QuotaCard key={sectionKey} />
          if (sectionKey === 'cluster') return null

          const fields = settings.filter(s => section.keys.includes(s.key)).map(renderField).filter(Boolean)

          return (
            <Card key={sectionKey} title={section.title} style={{ marginBottom: 16 }}
              extra={sectionKey === 'credentials' ? (
                <Button onClick={handleTestCredentials} loading={testing}>Test Connection</Button>
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
