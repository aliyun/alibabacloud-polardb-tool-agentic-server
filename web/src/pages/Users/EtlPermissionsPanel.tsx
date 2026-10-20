import { useEffect, useState } from 'react'
import { Alert, Button, Descriptions, Space, Typography } from 'antd'
import { useTranslation } from 'react-i18next'
import api, { getAPIErrorMessage } from '../../api/client'
import { formatDateTime } from '../../i18n/format'

type Status = {
  state: string
  operation?: string
  error?: string | null
  connection?: { state: string; checked_at?: string }
  sync?: { state?: string; synced_at?: string; snapshots_processed: number; snapshots_skipped: number }
}

export default function EtlPermissionsPanel({ sourceId }: { sourceId: string }) {
  const { i18n } = useTranslation()
  const zh = i18n.language.startsWith('zh')
  const [status, setStatus] = useState<Status | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [revision, setRevision] = useState(0)
  const url = `/api/identity-sources/${encodeURIComponent(sourceId)}/etl-permissions`
  useEffect(() => {
    let active = true
    let timer: ReturnType<typeof setTimeout>
    setStatus(null)
    const load = async () => {
      try {
        const response = await api.get<Status>(url)
        if (!active) return
        setStatus(response.data)
        setError(null)
        if (response.data.state === 'RUNNING') timer = setTimeout(() => void load(), 2000)
      } catch (cause) {
        if (active) setError(getAPIErrorMessage(cause, zh ? '无法读取知识库权限同步状态' : 'Could not load knowledge permission sync status'))
      }
    }
    void load()
    return () => { active = false; clearTimeout(timer) }
  }, [url, revision, zh])
  const trigger = async (operation: 'verify' | 'sync') => {
    setBusy(true)
    try {
      await api.post(`${url}/${operation}`)
      setRevision((v) => v + 1)
    } catch (cause) {
      setError(getAPIErrorMessage(cause, zh ? '无法启动知识库权限操作' : 'Could not start the knowledge permission operation'))
    } finally { setBusy(false) }
  }
  const states: Record<string, string> = zh
    ? { UNVERIFIED: '未验证', NEVER: '尚未执行', RUNNING: '执行中', SUCCEEDED: '成功', PARTIAL: '部分成功', FAILED: '失败' }
    : { UNVERIFIED: 'Not verified', NEVER: 'Not run', RUNNING: 'Running', SUCCEEDED: 'Succeeded', PARTIAL: 'Partial', FAILED: 'Failed' }
  const time = (value?: string) => value ? formatDateTime(value, i18n.resolvedLanguage ?? i18n.language) : '—'
  return <Space direction="vertical" style={{ width: '100%' }}>
    <Alert type="info" showIcon message={zh
      ? '从权限数据源读取飞书知识库的最新访问权限，并更新已开启自动绑定的 Agent。用户对知识库内文档的权限也可用于发现对应知识库。请先绑定知识库所在空间。'
      : 'Read the latest Feishu knowledge permissions from the configured data source and update Agents with auto-binding enabled. Document permissions can also make the containing knowledge base discoverable. Bind the knowledge base Space first.'} />
    <Typography.Text type="secondary">{zh ? '同步的是知识库访问权限，不会导入文档内容；用户和用户组信息通过身份源列表中的“同步用户和用户组”更新。' : 'This sync updates knowledge access permissions without importing documents. Update users and groups with Sync users and groups on the identity source list.'}</Typography.Text>
    <Space>
      <Button loading={busy} disabled={!status || status.state === 'RUNNING'} onClick={() => void trigger('verify')}>{zh ? '验证连接' : 'Verify connection'}</Button>
      <Button type="primary" loading={busy} disabled={!status || status.state === 'RUNNING'} onClick={() => void trigger('sync')}>{zh ? '立即同步权限' : 'Sync permissions now'}</Button>
      <Button onClick={() => setRevision((v) => v + 1)}>{zh ? '刷新结果' : 'Refresh results'}</Button>
    </Space>
    {error && <Alert type="error" showIcon message={error} />}
    {status && <Descriptions column={1} size="small">
      <Descriptions.Item label={zh ? '连接验证' : 'Connection verification'}>{states[status.connection?.state ?? 'UNVERIFIED']}</Descriptions.Item>
      <Descriptions.Item label={zh ? '连接验证时间' : 'Connection checked at'}>{time(status.connection?.checked_at)}</Descriptions.Item>
      <Descriptions.Item label={zh ? '最近操作状态' : 'Latest operation status'}>{states[status.state]}</Descriptions.Item>
      <Descriptions.Item label={zh ? '最近权限同步结果' : 'Last permission sync result'}>{states[status.sync?.state ?? 'NEVER']}</Descriptions.Item>
      <Descriptions.Item label={zh ? '最近权限同步时间' : 'Last permission sync'}>{time(status.sync?.synced_at)}</Descriptions.Item>
      <Descriptions.Item label={zh ? '处理 / 跳过权限记录数' : 'Processed / skipped permission records'}>{status.sync ? `${status.sync.snapshots_processed} / ${status.sync.snapshots_skipped}` : '—'}</Descriptions.Item>
    </Descriptions>}
    {status?.error && <Typography.Text type="danger">{zh ? '操作失败，请检查权限数据源的连接信息和账号读取权限后重试。' : 'Operation failed. Check the permission data source connection and account read permissions, then retry.'}</Typography.Text>}
  </Space>
}
