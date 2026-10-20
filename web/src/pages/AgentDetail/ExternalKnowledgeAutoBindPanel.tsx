import { useEffect, useRef, useState, useCallback, type Ref } from 'react'
import { Alert, Button, Checkbox, Modal, Space, Switch, Typography } from 'antd'
import { useTranslation } from 'react-i18next'

import {
  switchAgentKnowledgeScopeMode,
  getAgentExternalKnowledgeAutoBind,
  reconcileAgentExternalKnowledge,
  updateAgentExternalKnowledgeAutoBind,
  type AgentExternalKnowledgeAutoBind,
  type AgentKnowledgeScopeMode,
} from '../../api/agents'
import { getAPIErrorMessage } from '../../api/client'
import KnowledgePermissionRefreshControls, { type KnowledgePermissionRefreshHandle } from './KnowledgePermissionRefreshControls'

export default function ExternalKnowledgeAutoBindPanel({ agentId, onChanged, refreshActionRef, onRefreshAvailable, onRefreshBusy, selectedResourceIds = [] }: {
  agentId: string
  onChanged: () => void
  selectedResourceIds?: string[]
  refreshActionRef?: Ref<KnowledgePermissionRefreshHandle>
  onRefreshAvailable?: (enabled: boolean) => void
  onRefreshBusy?: (busy: boolean) => void
}) {
  const { t } = useTranslation()
  const [settings, setSettings] = useState<AgentExternalKnowledgeAutoBind | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [result, setResult] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  const [switchOpen, setSwitchOpen] = useState(false)
  const [switchAutoBind, setSwitchAutoBind] = useState(false)
  const [targetMode, setTargetMode] = useState<AgentKnowledgeScopeMode>('SCOPED')
  const [resetAllUsers, setResetAllUsers] = useState(true)
  const request = useRef(0)

  useEffect(() => {
    const current = ++request.current
    setSwitchOpen(false)
    setSwitchAutoBind(false)
    setSettings(null)
    setLoading(true)
    setSaving(false)
    setError(null)
    setSaved(false)
    setResult(null)
    void getAgentExternalKnowledgeAutoBind(agentId)
      .then((response) => { if (current === request.current) setSettings(response.data) })
      .catch((cause) => {
        if (current === request.current) setError(getAPIErrorMessage(cause, t('knowledgeBindings.autoBindLoadFailed')))
      })
      .finally(() => { if (current === request.current) setLoading(false) })
    return () => { request.current++ }
  }, [agentId, revision, t])

  const apply = async (enabled: boolean) => {
    if (!settings || saving || loading) return
    const current = request.current
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      const response = await updateAgentExternalKnowledgeAutoBind(agentId, enabled)
      if (current !== request.current) return
      setSettings({ ...settings, enabled: response.data.enabled })
      setSaved(true)
      onChanged()
    } catch (cause) {
      if (current === request.current) setError(getAPIErrorMessage(cause, t('knowledgeBindings.autoBindSaveFailed')))
    } finally {
      if (current === request.current) setSaving(false)
    }
  }

  const reconcile = async () => {
    const current = request.current
    setSaving(true)
    setError(null)
    setResult(null)
    try {
      const response = await reconcileAgentExternalKnowledge(agentId)
      if (current !== request.current) return
      setResult(t('knowledgeBindings.reconcileResult', { snapshots: response.data.snapshots_processed, bindings: response.data.bindings_projected, skipped: response.data.snapshots_skipped }))
      onChanged()
    } catch (cause) {
      if (current === request.current) setError(getAPIErrorMessage(cause, t('knowledgeBindings.autoBindSaveFailed')))
    } finally {
      if (current === request.current) setSaving(false)
    }
  }

  useEffect(() => { onRefreshAvailable?.(!!settings && settings.agent_id === agentId && settings.knowledge_scope_mode === 'SCOPED' && settings.enabled && !saving && !loading) }, [agentId, settings, saving, loading, onRefreshAvailable])
  const refreshBusyChanged = useCallback((value: boolean) => { setRefreshing(value); onRefreshBusy?.(value) }, [onRefreshBusy])

  // Only expose the setting after the current Agent is confirmed to be SCOPED.
  if (!settings || settings.agent_id !== agentId) {
    return error ? <Alert type="error" showIcon role="alert" message={error} action={
      <Button size="small" onClick={() => setRevision((value) => value + 1)}>{t('common.retry')}</Button>
    } /> : null
  }
  const openMode = (mode: AgentKnowledgeScopeMode) => {
    setTargetMode(mode)
    setResetAllUsers(true)
    setSwitchAutoBind(mode === 'SCOPED' && settings.enabled)
    setError(null)
    setSwitchOpen(true)
  }
  const modeControls = <Space direction="vertical">
    <Space>
      <Typography.Text>{settings.knowledge_scope_mode}</Typography.Text>
      <Button disabled={saving || refreshing} onClick={() => openMode(settings.knowledge_scope_mode === 'SCOPED' ? 'LEGACY_ALL' : 'SCOPED')}>
        {t(settings.knowledge_scope_mode === 'SCOPED' ? 'knowledgeBindings.switchLegacy' : 'knowledgeBindings.switchScoped')}
      </Button>
      {settings.knowledge_scope_mode === 'SCOPED' && <Button disabled={saving || refreshing} onClick={() => openMode('SCOPED')}>{t('knowledgeBindings.resetScoped')}</Button>}
    </Space>
    <Modal title={t(targetMode === 'SCOPED' ? 'knowledgeBindings.switchScoped' : 'knowledgeBindings.switchLegacy')} open={switchOpen}
      confirmLoading={saving} cancelButtonProps={{ disabled: saving }} closable={!saving}
      maskClosable={!saving} onCancel={() => { if (!saving) setSwitchOpen(false) }}
      onOk={async () => {
        if (saving) return
        const current = request.current
        setSaving(true)
        setError(null)
        try {
          const response = await switchAgentKnowledgeScopeMode(agentId, targetMode === 'SCOPED' && switchAutoBind, targetMode, targetMode === 'SCOPED' && resetAllUsers, settings.knowledge_scope_mode)
          if (current !== request.current) return
          setSettings({ agent_id: agentId, knowledge_scope_mode: response.data.mode, enabled: response.data.enabled })
          setSwitchOpen(false)
          setResult(null)
          setSaved(false)
          onChanged()
        } catch (cause) {
          if (current === request.current) setError(getAPIErrorMessage(cause, t('knowledgeBindings.autoBindSaveFailed')))
        } finally {
          if (current === request.current) setSaving(false)
        }
      }}>
      <Space direction="vertical">
        <Alert type="warning" showIcon message={t(targetMode === 'SCOPED' ? 'knowledgeBindings.switchScopedHint' : 'knowledgeBindings.switchLegacyHint')} />
        {targetMode === 'SCOPED' && <><Checkbox checked={resetAllUsers} disabled={saving || refreshing} onChange={(event) => setResetAllUsers(event.target.checked)}>
          {t('knowledgeBindings.resetAllUsers')}
        </Checkbox>
        <Checkbox checked={switchAutoBind} disabled={saving || refreshing} onChange={(event) => setSwitchAutoBind(event.target.checked)}>
          {t('knowledgeBindings.autoBindTitle')}
        </Checkbox>
        <Typography.Text type="secondary">{t('knowledgeBindings.switchScopedAutoHint')}</Typography.Text></>}
        {error && <Alert type="error" role="alert" message={error} />}
      </Space>
    </Modal>
  </Space>
  if (settings.knowledge_scope_mode !== 'SCOPED') return modeControls

  return <Space direction="vertical" size={8} style={{ width: '100%' }}>
    {modeControls}
    <Space>
      <Typography.Text strong>{t('knowledgeBindings.autoBindTitle')}</Typography.Text>
      <Switch
        aria-label={t('knowledgeBindings.autoBindTitle')}
        checked={settings?.enabled ?? false}
        loading={loading || saving}
        disabled={loading || saving || refreshing || !settings}
        onChange={(enabled) => void apply(enabled)}
      />
      <Button loading={saving} disabled={loading || saving || refreshing || !settings.enabled} onClick={() => void reconcile()}>{t('knowledgeBindings.recomputeImportedPermissions')}</Button>
    </Space>
    <KnowledgePermissionRefreshControls agentId={agentId} actionRef={refreshActionRef} selectedResourceIds={selectedResourceIds}
      disabled={loading || saving || !settings.enabled} onChanged={onChanged} onBusyChange={refreshBusyChanged} />
    <Typography.Text type="secondary">{t('knowledgeBindings.recomputeImportedHint')}</Typography.Text>
    {result && <Alert type="success" showIcon role="status" message={result} />}
    <Typography.Text type="secondary">{t('knowledgeBindings.autoBindHint')}</Typography.Text>
    {error && <Alert type="error" showIcon role="alert" message={error} action={
      !settings ? <Button size="small" onClick={() => setRevision((value) => value + 1)}>{t('common.retry')}</Button> : undefined
    } />}
    {saved && <Alert type="success" showIcon role="status" message={t('knowledgeBindings.autoBindSaved')} />}
  </Space>
}
