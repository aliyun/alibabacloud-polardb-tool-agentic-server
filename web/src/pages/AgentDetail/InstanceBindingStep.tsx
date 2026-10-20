import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Input,
  Modal,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd'
import { useTranslation } from 'react-i18next'

import {
  createAgentPolarRAGBinding,
  deleteAgentPolarRAGBinding,
  listAgentPolarRAGBindings,
  type AgentPolarRAGBinding,
} from '../../api/agents'
import { getAPIErrorMessage } from '../../api/client'
import {
  listPolarRAGInstances,
  type PolarRAGInstance,
} from '../../api/polarrag'

const { Text, Title } = Typography
const PAGE_SIZE = 20

export default function InstanceBindingStep({
  agentId,
  onBindingsChange,
}: {
  agentId: string
  bindings: AgentPolarRAGBinding[]
  onBindingsChange: (bindings: AgentPolarRAGBinding[]) => void
}) {
  const { t } = useTranslation()
  const [instanceTab, setInstanceTab] = useState<'available' | 'bound'>('available')
  const [availableInstances, setAvailableInstances] = useState<PolarRAGInstance[]>([])
  const [boundInstanceRows, setBoundInstanceRows] = useState<AgentPolarRAGBinding[]>([])
  const [instanceTotal, setInstanceTotal] = useState(0)
  const [instanceOffset, setInstanceOffset] = useState(0)
  const [instanceSearch, setInstanceSearch] = useState('')
  const [selectedInstanceIds, setSelectedInstanceIds] = useState<string[]>([])
  const [instanceLoading, setInstanceLoading] = useState(false)
  const [instanceRefreshVersion, setInstanceRefreshVersion] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const requestVersion = useRef(0)

  const loadInstancePage = useCallback(async () => {
    const version = ++requestVersion.current
    setInstanceLoading(true)
    setError(null)
    if (instanceTab === 'available') setAvailableInstances([])
    else setBoundInstanceRows([])
    try {
      if (instanceTab === 'available') {
        const response = await listPolarRAGInstances({
          offset: instanceOffset,
          limit: PAGE_SIZE,
          search: instanceSearch || undefined,
          agent_id: agentId,
          binding_status: 'unbound',
          status: 'active',
        })
        if (version !== requestVersion.current) return
        setAvailableInstances(response.data.items)
        setInstanceTotal(response.data.total)
      } else {
        const response = await listAgentPolarRAGBindings(agentId, {
          offset: instanceOffset,
          limit: PAGE_SIZE,
        })
        if (version !== requestVersion.current) return
        setBoundInstanceRows(response.data.items)
        setInstanceTotal(response.data.total)
      }
    } catch (requestError) {
      if (version !== requestVersion.current) return
      setError(
        getAPIErrorMessage(requestError, t('polarragAccess.loadFailed')),
      )
    } finally {
      if (version === requestVersion.current) setInstanceLoading(false)
    }
  }, [agentId, instanceOffset, instanceSearch, instanceTab, t])

  useEffect(() => {
    void loadInstancePage()
    return () => { requestVersion.current += 1 }
  }, [loadInstancePage, instanceRefreshVersion])

  const currentInstanceIds = (
    instanceTab === 'available' ? availableInstances : boundInstanceRows
  ).map((row) => row.id)
  const currentInstancePageSelected =
    currentInstanceIds.length > 0 &&
    currentInstanceIds.every((id) => selectedInstanceIds.includes(id))
  const refreshBindings = async () => {
    const response = await listAgentPolarRAGBindings(agentId, {
      offset: 0,
      limit: 100,
    })
    onBindingsChange(response.data.items)
  }

  const bindSelectedInstances = async () => {
    if (selectedInstanceIds.length === 0) return
    setBusy(true)
    setError(null)
    try {
      await Promise.all(
        selectedInstanceIds.map((instanceId) =>
          createAgentPolarRAGBinding(agentId, instanceId),
        ),
      )
      setSelectedInstanceIds([])
      setInstanceOffset(0)
      await refreshBindings()
      setInstanceRefreshVersion((version) => version + 1)
      message.success(t('agentKnowledgeWorkflow.instanceBindingCompleted'))
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, t('polarragAccess.updateFailed')),
      )
    } finally {
      setBusy(false)
    }
  }

  const unbindSelectedInstances = async () => {
    if (selectedInstanceIds.length === 0) return
    setBusy(true)
    setError(null)
    try {
      await Promise.all(
        selectedInstanceIds.map((bindingId) =>
          deleteAgentPolarRAGBinding(agentId, bindingId),
        ),
      )
      setSelectedInstanceIds([])
      setInstanceOffset(0)
      await refreshBindings()
      setInstanceRefreshVersion((version) => version + 1)
      message.success(t('agentKnowledgeWorkflow.instanceUnbindingCompleted'))
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, t('polarragAccess.updateFailed')),
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <div>
        <Title level={4} style={{ marginBlock: 0 }}>
          {t('agentKnowledgeWorkflow.bindInstances')}
        </Title>
        <Text type="secondary">
          {t('agentKnowledgeWorkflow.bindInstancesDescription')}
        </Text>
      </div>
      {error && <Alert type="error" showIcon message={error} />}
      <div>
        <Title level={5} style={{ marginBlock: 0 }}>
          {t('agentKnowledgeWorkflow.agentInstanceBindings')}
        </Title>
        <Text type="secondary">
          {t('agentKnowledgeWorkflow.agentInstanceBindingsDescription')}
        </Text>
      </div>
      <Tabs
        activeKey={instanceTab}
        onChange={(key) => {
          setInstanceTab(key as 'available' | 'bound')
          setInstanceOffset(0)
          setSelectedInstanceIds([])
        }}
        items={[
          {
            key: 'available',
            label: t('agentKnowledgeWorkflow.availableInstances'),
          },
          {
            key: 'bound',
            label: t('agentKnowledgeWorkflow.boundInstances'),
          },
        ]}
      />
      {instanceTab === 'available' && (
        <Input.Search
          allowClear
          placeholder={t('agentKnowledgeWorkflow.searchInstances')}
          value={instanceSearch}
          onChange={(event) => {
            setInstanceSearch(event.target.value)
            setInstanceOffset(0)
            setSelectedInstanceIds([])
          }}
        />
      )}
      <Table<PolarRAGInstance | AgentPolarRAGBinding>
        rowKey="id"
        loading={instanceLoading}
        dataSource={
          instanceTab === 'available' ? availableInstances : boundInstanceRows
        }
        rowSelection={{
          selectedRowKeys: selectedInstanceIds,
          onChange: (keys) => setSelectedInstanceIds(keys.map(String)),
        }}
        pagination={{
          current: Math.floor(instanceOffset / PAGE_SIZE) + 1,
          pageSize: PAGE_SIZE,
          total: instanceTotal,
          showSizeChanger: false,
          showTotal: (total) =>
            t('agentKnowledgeWorkflow.instanceCount', { count: total }),
          onChange: (page) => {
            setInstanceOffset((page - 1) * PAGE_SIZE)
            setSelectedInstanceIds([])
          },
        }}
        columns={[
          {
            title: t('agentKnowledgeWorkflow.polarragInstance'),
            dataIndex: 'name',
            render: (_value, row: PolarRAGInstance | AgentPolarRAGBinding) => (
              <Space direction="vertical" size={0}>
                <Text strong>
                  {'instance_name' in row ? row.instance_name : row.name}
                </Text>
                <Text type="secondary">
                  {'polarrag_instance_id' in row
                    ? row.polarrag_instance_id
                    : row.id}
                </Text>
              </Space>
            ),
          },
          {
            title: t('agentDetail.permission'),
            hidden: instanceTab !== 'bound',
            render: () => t('agentDetail.userAcl'),
          },
          {
            title: t('agentDetail.publicScope'),
            hidden: instanceTab !== 'bound',
            render: (_value, row: PolarRAGInstance | AgentPolarRAGBinding) =>
              'public_knowledge_resource_ids' in row
                ? row.public_knowledge_resource_ids === null
                  ? row.public_excluded_resource_ids?.length
                    ? t('agentDetail.allPublicResourcesExcept', { count: row.public_excluded_resource_ids.length })
                    : t('agentDetail.allPublicResources')
                  : t('agentDetail.selectedPublicResources', { count: row.public_knowledge_resource_ids.length })
                : '—',
          },
          {
            title: t('agentKnowledgeWorkflow.status'),
            render: (_value, row: PolarRAGInstance | AgentPolarRAGBinding) =>
              'instance_name' in row ? (
                <Tag color="green">{t('agentKnowledgeWorkflow.bound')}</Tag>
              ) : (
                <Tag color={row.status === 'active' ? 'green' : 'default'}>
                  {row.status}
                </Tag>
              ),
          },
          {
            title: t('agentKnowledgeWorkflow.pluginVersion'),
            hidden: instanceTab === 'bound',
            render: (_value, row: PolarRAGInstance | AgentPolarRAGBinding) =>
              'plugin_version' in row ? row.plugin_version ?? '—' : '—',
          },
        ]}
      />
      <Space style={{ justifyContent: 'flex-end', width: '100%' }}>
        <Button
          disabled={currentInstanceIds.length === 0}
          onClick={() =>
            setSelectedInstanceIds(
              currentInstancePageSelected ? [] : currentInstanceIds,
            )
          }
        >
          {t(
            currentInstancePageSelected
              ? 'agentKnowledgeWorkflow.clearCurrentInstancePage'
              : 'agentKnowledgeWorkflow.selectCurrentInstancePage',
          )}
        </Button>
        {instanceTab === 'available' ? (
          <Button
            type="primary"
            loading={busy}
            disabled={selectedInstanceIds.length === 0}
            onClick={() => void bindSelectedInstances()}
          >
            {t('agentKnowledgeWorkflow.bindSelectedInstances')}
          </Button>
        ) : (
          <Button
            danger
            loading={busy}
            disabled={selectedInstanceIds.length === 0}
            onClick={() =>
              Modal.confirm({
                title: t('agentKnowledgeWorkflow.unbindSelectedInstances'),
                okText: t('agentKnowledgeWorkflow.confirmUnbinding'),
                okButtonProps: { danger: true },
                onOk: unbindSelectedInstances,
              })
            }
          >
            {t('agentKnowledgeWorkflow.unbindSelectedInstances')}
          </Button>
        )}
      </Space>
    </Space>
  )
}
