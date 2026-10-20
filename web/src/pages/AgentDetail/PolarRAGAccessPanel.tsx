import { Tabs } from 'antd'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { AgentPolarRAGBinding } from '../../api/agents'
import InstanceBindingStep from './InstanceBindingStep'
import KnowledgeBindingsPanel from './KnowledgeBindingsPanel'
import SubjectAssignmentsStep from './SubjectAssignmentsStep'

export default function PolarRAGAccessPanel({
  agentId,
  bindings,
  onBindingsChange,
}: {
  agentId: string
  bindings: AgentPolarRAGBinding[]
  onBindingsChange: (bindings: AgentPolarRAGBinding[]) => void
}) {
  const { t } = useTranslation()
  const [scopeRevision, setScopeRevision] = useState(0)

  return (
    <Tabs
      defaultActiveKey="instances"
      destroyOnHidden={false}
      items={[
          {
            key: 'instances',
            label: `1 ${t('agentKnowledgeWorkflow.bindInstances')}`,
            children: (
              <InstanceBindingStep
                key={scopeRevision}
                agentId={agentId}
                bindings={bindings}
                onBindingsChange={onBindingsChange}
              />
            ),
          },
          {
            key: 'subjects',
            label: `2 ${t('agentKnowledgeWorkflow.configureSubjects')}`,
            children: <SubjectAssignmentsStep agentId={agentId} />,
          },
          {
            key: 'knowledge',
            label: `3 ${t('agentKnowledgeWorkflow.bindKnowledge')}`,
            children: <KnowledgeBindingsPanel
              agentId={agentId}
              onPublicScopeChanged={() => setScopeRevision((value) => value + 1)}
            />,
          },
      ]}
    />
  )
}
