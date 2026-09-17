import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Card, Form, Input, InputNumber, Select, Space, Steps, Switch, Tag, Typography } from 'antd'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import api, { getAPIErrorMessage } from '../../api/client'
import { executeConfig } from '../../api/configuration'
import { createIdempotencyKey } from '../Setup/workflowSafety'
import PageContainer from '../../components/PageContainer'

interface Status {
  state: string
  managed: boolean
  available: boolean
  desired_enabled: boolean
  desired_revision: number
  loaded_revision: number
  error_code: string | null
  replicas: { id: string; loaded_revision: number; prepared: boolean; error_code: string | null }[]
  blockers: { in_flight: number; pending_cleanup: number; pending_uploads: number; catalog_syncs: number }
}
interface Options {
  connections: { id: string; name: string; status: string }[]
  resources: { id: string; name: string; instance_id: string; space_id: string }[]
  users: { id: string; name: string; external_id: string }[]
}
interface Connection {
  name: string; scheme: string; host: string; port: number; username: string; password: string; tls_verify: boolean
}
const root = '/api/features/knowledge'

export default function Features() {
  const { t } = useTranslation()
  const [status, setStatus] = useState<Status>()
  const [options, setOptions] = useState<Options>({ connections: [], resources: [], users: [] })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()
  const [notice, setNotice] = useState<string>()
  const [editing, setEditing] = useState(false)
  const [newConnection, setNewConnection] = useState(false)
  const [connection, setConnection] = useState<string>()
  const [spaces, setSpaces] = useState<{ id: string; name: string }[]>([])
  const [space, setSpace] = useState<string>()
  const [resource, setResource] = useState<string>()
  const [user, setUser] = useState<string>()
  const [verified, setVerified] = useState(false)
  const [form] = Form.useForm<Connection>()
  const refresh = useCallback(async () => {
    const response = await api.get(root)
    setStatus(response.data)
  }, [])
  const loadOptions = useCallback(async () => { setOptions((await api.get(root + '/options')).data) }, [])
  useEffect(() => {
    let mounted = true
    const poll = () => { if (mounted) void refresh().catch(e => setError(getAPIErrorMessage(e, t('features.failed')))) }
    poll()
    const timer = window.setInterval(poll, 5000)
    return () => { mounted = false; window.clearInterval(timer) }
  }, [refresh, t])
  const run = async (action: () => Promise<void>) => {
    setBusy(true); setError(undefined); setNotice(undefined)
    try { await action(); await refresh() }
    catch (e) { setError(getAPIErrorMessage(e, t('features.failed'))) }
    finally { setBusy(false) }
  }
  const apply = async (enabled: boolean) => {
    const described = await executeConfig({ action: 'describe', module: 'knowledge' })
    const config = enabled
      ? { enabled: true, validation_resource_id: resource, validation_user_id: user }
      : { ...described.module?.effective?.config, enabled: false }
    const saved = await executeConfig({ action: 'save_draft', module: 'knowledge', expected_revision: described.module!.revision, config })
    const checked = await executeConfig({ action: 'validate', module: 'knowledge', expected_revision: saved.module!.revision })
    await executeConfig({ action: 'activate', module: 'knowledge', expected_revision: checked.module!.revision,
      validation_id: checked.validation!.validation_id, idempotency_key: createIdempotencyKey() })
    setEditing(false)
    setNotice(t(status?.managed ? 'features.managedRestart' : 'features.restart'))
  }
  const blockers = status ? Object.values(status.blockers).reduce((a, b) => a + b, 0) : 0
  return <PageContainer title={t('features.title')} description={t('features.description')}>
    <Space direction="vertical" size="large" style={{ width: '100%', maxWidth: 900 }}>
      {error && <Alert type="error" showIcon message={error} />}
      {notice && <Alert type="success" showIcon message={notice} />}
      <Card title={t('features.knowledge')} extra={<Tag color={status?.available ? 'green' : 'default'}>{t(`features.states.${status?.state ?? 'LOADING'}`)}</Tag>}>
        <Typography.Paragraph>{t('features.knowledgeDescription')}</Typography.Paragraph>
        {status?.managed && <Alert style={{ marginBottom: 16 }} showIcon type="info" message={t('features.managed')} />}
        <Space wrap>
          {(!status?.desired_enabled || status.state === 'FAILED') && <Button type="primary" disabled={!status} onClick={() => void run(async () => { await loadOptions(); setEditing(true) })}>{t('features.enable')}</Button>}
          {status?.desired_enabled && status.state !== 'DRAINING' && status.state !== 'FAILED' && <Button loading={busy} onClick={() => void run(async () => { await api.post(root + '/drain') })}>{t('features.disable')}</Button>}
          {(status?.state === 'DRAINING' || status?.state === 'FAILED') && <>
            <Button danger loading={busy} disabled={blockers > 0} onClick={() => void run(() => apply(false))}>{t('features.finishDisable')}</Button>
            <Button onClick={() => void run(async () => { await api.post(root + '/cancel-drain', { expected_revision: status.desired_revision }) })}>{t('features.cancelDrain')}</Button>
          </>}
          {!status?.managed && status?.state === 'ACTIVATING' && <Button loading={busy} onClick={() => void run(async () => { await api.post(root + '/confirm-activation', { expected_revision: status.desired_revision, replica_ids: status.replicas.map(r => r.id) }) })}>{t('features.confirmReplicas')}</Button>}
          {status?.available && <Link to="/instances?type=polarrag">{t('features.manageKnowledge')}</Link>}
        </Space>
        {status?.state === 'PENDING_RESTART' && <Alert style={{ marginTop: 16 }} type="info" showIcon message={t(status.managed ? 'features.managedRestart' : 'features.restart')} />}
        {status?.state === 'ACTIVATING' && <Alert style={{ marginTop: 16 }} type="info" showIcon message={t(status.managed ? 'features.managedActivating' : 'features.activating')} />}
        {status?.state === 'DRAINING' && <Alert style={{ marginTop: 16 }} type="warning" showIcon message={t('features.draining', status.blockers)} />}
        {status?.error_code && <Alert style={{ marginTop: 16 }} type="error" message={status.error_code} description={t('features.failureRecovery')} />}
      </Card>
      {editing && <Card title={t('features.setup')}>
        <Steps size="small" current={verified ? 2 : space ? 1 : 0} items={[{ title: t('features.connection') }, { title: t('features.readAccess') }, { title: t('features.activate') }]} style={{ marginBottom: 24 }} />
        <Alert type="info" showIcon message={t('features.existingService')} style={{ marginBottom: 20 }} />
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Select aria-label={t('features.connection')} style={{ width: '100%' }} placeholder={t('features.chooseConnection')} value={connection}
            options={options.connections.map(c => ({ value: c.id, label: c.name }))}
            onChange={value => { setConnection(value); setSpace(undefined); setSpaces([]); setResource(undefined); setVerified(false) }} />
          <Space>
            <Button disabled={!connection} loading={busy} onClick={() => void run(async () => { setSpaces((await api.post(root + `/connections/${connection}/check`)).data.spaces) })}>{t('features.checkConnection')}</Button>
            <Button onClick={() => setNewConnection(true)}>{t('features.addConnection')}</Button>
          </Space>
          {newConnection && <Form form={form} layout="vertical" initialValues={{ scheme: 'https', port: 9200, tls_verify: true }} onFinish={values => void run(async () => {
            const saved = await api.post(root + '/connections', values)
            setConnection(saved.data.id); setNewConnection(false); form.resetFields(); await loadOptions(); setNotice(t('features.draftSaved'))
          })}>
            {(['name', 'host', 'username'] as const).map(name => <Form.Item key={name} name={name} label={t(`features.fields.${name}`)} rules={[{ required: true }]}><Input autoComplete="off" /></Form.Item>)}
            <Form.Item name="scheme" label={t('features.fields.scheme')}><Select options={[{ value: 'https', label: 'HTTPS' }, { value: 'http', label: 'HTTP' }]} /></Form.Item>
            <Form.Item name="port" label={t('features.fields.port')} rules={[{ required: true }]}><InputNumber min={1} max={65535} /></Form.Item>
            <Form.Item name="password" label={t('features.fields.password')} rules={[{ required: true }]}><Input.Password autoComplete="new-password" /></Form.Item>
            <Form.Item name="tls_verify" label={t('features.fields.tls')} valuePropName="checked"><Switch /></Form.Item>
            <Button htmlType="submit" loading={busy}>{t('features.saveDraft')}</Button>
          </Form>}
          <Select aria-label={t('features.space')} style={{ width: '100%' }} placeholder={t('features.space')} value={space} disabled={!spaces.length}
            options={spaces.map(s => ({ value: s.id, label: s.name }))} onChange={value => void run(async () => {
              setSpace(value); setResource(undefined); setVerified(false)
              await api.post(root + `/connections/${connection}/spaces`, { space_id: value }); await loadOptions()
            })} />
          <Select aria-label={t('features.resource')} style={{ width: '100%' }} placeholder={t('features.resource')} value={resource}
            options={options.resources.filter(r => r.instance_id === connection && r.space_id === space).map(r => ({ value: r.id, label: r.name }))}
            onChange={value => { setResource(value); setVerified(false) }} />
          <Select aria-label={t('features.user')} style={{ width: '100%' }} placeholder={t('features.user')} showSearch optionFilterProp="label" value={user}
            options={options.users.map(u => ({ value: u.id, label: `${u.name} (${u.external_id})` }))} onChange={value => { setUser(value); setVerified(false) }} />
          <Typography.Paragraph type="secondary">{t('features.identityHelp')}</Typography.Paragraph>
          <Space wrap>
            <Button loading={busy} disabled={!resource || !user} onClick={() => void run(async () => {
              await api.post(root + '/test', { validation_resource_id: resource, validation_user_id: user }); setVerified(true); setNotice(t('features.readVerified'))
            })}>{t('features.testRead')}</Button>
            <Button type="primary" loading={busy} disabled={!verified} onClick={() => void run(() => apply(true))}>{t(status?.managed ? 'features.saveConfiguration' : 'features.saveEnable')}</Button>
            <Button onClick={() => setEditing(false)}>{t('common.cancel')}</Button>
          </Space>
        </Space>
      </Card>}
    </Space>
  </PageContainer>
}
