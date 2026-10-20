import { useEffect, useImperativeHandle, useRef, useState, type Ref } from 'react'
import { Alert, Button, Space, Typography, notification } from 'antd'
import { useTranslation } from 'react-i18next'
import { getAgentKnowledgePermissionRefresh, refreshAgentKnowledgePermissions, type KnowledgePermissionRefreshJob } from '../../api/agents'
import { getAPIErrorMessage } from '../../api/client'

function isJob(value: unknown, agentId: string): value is KnowledgePermissionRefreshJob {
  if (!value || typeof value !== 'object') return false
  const job = value as KnowledgePermissionRefreshJob
  return typeof job.job_id === 'string' && !!job.job_id && job.agent_id === agentId
    && ['QUEUED', 'RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED'].includes(job.state)
    && Number.isInteger(job.resources_total) && job.resources_total >= 0
    && Number.isInteger(job.resources_completed) && job.resources_completed >= 0
    && job.resources_completed <= job.resources_total
}

const allSkipped = (job: KnowledgePermissionRefreshJob) => job.state === 'SUCCEEDED' && job.resources_total > 0
  && job.resources_skipped === job.resources_total
const terminal = (job: KnowledgePermissionRefreshJob) => ['SUCCEEDED', 'PARTIAL', 'FAILED'].includes(job.state)
const storageKey = (id: string) => `pas.knowledge-refresh.${id}`
function remember(agentId: string, jobId: string | null) {
  try {
    if (jobId) sessionStorage.setItem(storageKey(agentId), jobId)
    else sessionStorage.removeItem(storageKey(agentId))
  } catch { /* Refresh execution is independent of browser storage. */ }
}

export interface KnowledgePermissionRefreshHandle { refreshResource: (id: string) => void }

export default function KnowledgePermissionRefreshControls({ agentId, disabled, onChanged, onBusyChange, actionRef, selectedResourceIds = [] }: {
  agentId: string
  actionRef?: Ref<KnowledgePermissionRefreshHandle>
  selectedResourceIds?: string[]
  disabled: boolean
  onChanged: () => void
  onBusyChange: (busy: boolean) => void
}) {
  const { t } = useTranslation()
  const [notifier, notificationContext] = notification.useNotification()
  const rowRequest = useRef(false)
  const notifyRow = (value: KnowledgePermissionRefreshJob) => {
    if (!rowRequest.current || !terminal(value)) return
    rowRequest.current = false
    notifier.open({
      message: t(allSkipped(value) ? 'knowledgeBindings.refreshNothingToApply' : `knowledgeBindings.refreshStates.${value.state}`),
      description: [
        t('knowledgeBindings.refreshJobId', { id: value.job_id }),
        value.resources_skipped ? t('knowledgeBindings.refreshSkipped', { count: value.resources_skipped }) : '',
        value.state !== 'SUCCEEDED' ? t('knowledgeBindings.refreshFailedHint') : '',
      ].filter(Boolean).join(' '),
      type: allSkipped(value) ? 'warning' : value.state === 'SUCCEEDED' ? 'success' : 'error',
    })
  }
  const [job, setJob] = useState<KnowledgePermissionRefreshJob | null>(null)
  const [posting, setPosting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [retry, setRetry] = useState(0)
  const generation = useRef(0)
  const inFlight = useRef(false)
  const changed = useRef(onChanged)
  changed.current = onChanged
  const busy = posting || !!(job && !terminal(job))
  const activeJobId = job && !terminal(job) ? job.job_id : null
  const placeholder = (id: string): KnowledgePermissionRefreshJob => ({ job_id: id, agent_id: agentId, state: 'QUEUED', resources_total: 0, resources_completed: 0 })

  useEffect(() => {
    generation.current++
    rowRequest.current = false
    inFlight.current = false
    setPosting(false)
    setError(null)
    let id: string | null = null
    try { id = sessionStorage.getItem(storageKey(agentId)) } catch { /* Optional persistence. */ }
    setJob(id ? { job_id: id, agent_id: agentId, state: 'QUEUED', resources_total: 0, resources_completed: 0 } : null)
    return () => { generation.current++ }
  }, [agentId])

  useEffect(() => { onBusyChange(busy) }, [busy, onBusyChange])

  useEffect(() => {
    if (!activeJobId) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const response = await getAgentKnowledgePermissionRefresh(agentId, activeJobId)
        if (cancelled) return
        if (!isJob(response.data, agentId)) throw new Error(t('knowledgeBindings.refreshStatusFailed'))
        setError(null)
        setJob(response.data)
        notifyRow(response.data)
        if (terminal(response.data)) {
          remember(agentId, null)
          if (response.data.state !== 'FAILED') changed.current()
        } else timer = setTimeout(() => void poll(), 1000)
      } catch (cause) {
        if (!cancelled) {
          setError(getAPIErrorMessage(cause, t('knowledgeBindings.refreshStatusFailed')))
          if ((cause as { response?: { status?: number } }).response?.status === 404) {
            remember(agentId, null)
            setJob(null)
          }
        }
      }
    }
    timer = setTimeout(() => void poll(), 1000)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [agentId, activeJobId, retry, t])

  const start = async (resourceId?: string | string[]) => {
    if (disabled || busy || inFlight.current) return
    inFlight.current = true
    const current = generation.current
    setPosting(true)
    setJob(null)
    setError(null)
    try {
      const response = await refreshAgentKnowledgePermissions(agentId, resourceId)
      if (current !== generation.current) return
      if (!isJob(response.data, agentId)) {
        const detail = response.data as unknown as { detail?: { code?: string; job_id?: string } }
        if (detail?.detail?.code === 'REFRESH_ALREADY_RUNNING' && detail.detail.job_id) {
          setJob(placeholder(detail.detail.job_id))
          remember(agentId, detail.detail.job_id)
          return
        }
        throw new Error(t('knowledgeBindings.refreshStartFailed'))
      }
      setJob(response.data)
      notifyRow(response.data)
      remember(agentId, terminal(response.data) ? null : response.data.job_id)
      if (terminal(response.data) && response.data.state !== 'FAILED') changed.current()
    } catch (cause) {
      if (current !== generation.current) return
      const detail = (cause as { response?: { data?: { detail?: { code?: string; job_id?: string } } } }).response?.data?.detail
      if (detail?.code === 'REFRESH_ALREADY_RUNNING' && detail.job_id) {
        setJob(placeholder(detail.job_id))
        remember(agentId, detail.job_id)
      } else {
        const text = getAPIErrorMessage(cause, t('knowledgeBindings.refreshStartFailed'))
        setError(text)
        if (rowRequest.current) notifier.error({ message: text })
        rowRequest.current = false
      }
    } finally {
      if (current === generation.current) { setPosting(false); inFlight.current = false }
    }
  }

  useImperativeHandle(actionRef, () => ({ refreshResource: (id: string) => { if (!disabled && !busy && !inFlight.current) { rowRequest.current = true; void start(id) } } }))

  const failed = job?.state === 'FAILED' || job?.state === 'PARTIAL'
  return <Space direction="vertical" style={{ width: '100%' }}>
    {notificationContext}
    <Space wrap>
      <Button type="primary" loading={posting} disabled={disabled || busy} onClick={() => void start()}>{t('knowledgeBindings.refreshPermissions')}</Button>
      <Button disabled={disabled || busy || !selectedResourceIds.length} onClick={() => void start(selectedResourceIds)}>{t('knowledgeBindings.refreshSelectedPermissions')}</Button>
    </Space>
    <Typography.Text type="secondary">{t('knowledgeBindings.refreshPermissionsHint')}</Typography.Text>
    {job && <Alert role="status" showIcon type={failed ? 'error' : allSkipped(job) ? 'warning' : terminal(job) ? 'success' : 'info'}
      message={t(allSkipped(job) ? 'knowledgeBindings.refreshNothingToApply' : `knowledgeBindings.refreshStates.${job.state}`)}
      description={<Space direction="vertical"><span>{t('knowledgeBindings.refreshProgress', { completed: job.resources_completed, total: job.resources_total })}</span>
        {!!job.resources_skipped && <span>{t('knowledgeBindings.refreshSkipped', { count: job.resources_skipped })}</span>}
        {failed && <span>{t('knowledgeBindings.refreshFailedHint')}</span>}
        <Typography.Text type="secondary">{t('knowledgeBindings.refreshJobId', { id: job.job_id })}</Typography.Text>
      </Space>} />}
    {error && <Alert role="alert" type="error" showIcon message={error} action={activeJobId ? <Button onClick={() => setRetry((v) => v + 1)}>{t('common.retry')}</Button> : undefined} />}
  </Space>
}
