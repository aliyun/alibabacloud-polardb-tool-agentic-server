import { useEffect, useState } from 'react'
import { Alert, Button, Space, Tag, Typography } from 'antd'
import { useTranslation } from 'react-i18next'

import {
  getAgentKnowledgeVisibility,
  updateAgentKnowledgeVisibility,
  type AgentKnowledgeVisibility,
} from '../../api/agents'
import { getAPIErrorMessage } from '../../api/client'

export default function PublicKnowledgeScopePanel({
  agentId, onChanged,
}: {
  agentId: string
  onChanged: () => void
}) {
  const { t } = useTranslation()
  const [visibility, setVisibility] = useState<AgentKnowledgeVisibility | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setVisibility(null)
    void getAgentKnowledgeVisibility(agentId)
      .then((response) => { if (!cancelled) setVisibility(response.data) })
      .catch((requestError) => {
        if (!cancelled) setError(getAPIErrorMessage(requestError, t('knowledgeBindings.loadFailed')))
      })
    return () => { cancelled = true }
  }, [agentId, t])

  const apply = async (kbType: 'PUBLIC' | 'PERSONAL', operation: 'BIND' | 'UNBIND') => {
    if (busy || !visibility?.instance_count
      || visibility.statuses[kbType] === (operation === 'BIND' ? 'BOUND' : 'UNBOUND')) return
    setBusy(true)
    setError(null)
    try {
      const response = await updateAgentKnowledgeVisibility(agentId, kbType, operation)
      setVisibility(response.data)
      onChanged()
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('knowledgeBindings.updateFailed')))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      {error && <Alert type="error" showIcon message={error} />}
      <Typography.Text type="secondary">{t('agentKnowledgeWorkflow.knowledgeVisibilityHint')}</Typography.Text>
      {(['PUBLIC', 'PERSONAL'] as const).map((kbType) => (
        <Space key={kbType} wrap>
          <Typography.Text>{t(`agentKnowledgeWorkflow.knowledgeTypes.${kbType}`)}</Typography.Text>
          {visibility && <Tag>{t(`agentKnowledgeWorkflow.bindingStates.${visibility.statuses[kbType]}`)}</Tag>}
          <Button
            type="primary"
            disabled={busy || !visibility?.instance_count || visibility.statuses[kbType] === 'BOUND'}
            onClick={() => void apply(kbType, 'BIND')}
          >
            {t('agentKnowledgeWorkflow.bindAllKnowledgeType', { type: t(`agentKnowledgeWorkflow.knowledgeTypes.${kbType}`) })}
          </Button>
          <Button
            danger
            disabled={busy || !visibility?.instance_count || visibility.statuses[kbType] === 'UNBOUND'}
            onClick={() => void apply(kbType, 'UNBIND')}
          >
            {t('agentKnowledgeWorkflow.unbindAllKnowledgeType', { type: t(`agentKnowledgeWorkflow.knowledgeTypes.${kbType}`) })}
          </Button>
        </Space>
      ))}
    </Space>
  )
}
