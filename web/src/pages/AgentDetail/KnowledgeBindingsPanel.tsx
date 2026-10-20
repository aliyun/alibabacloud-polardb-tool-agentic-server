import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Drawer,
  Input,
  Radio,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd'
import { useTranslation } from 'react-i18next'
import ExternalKnowledgeAutoBindPanel from './ExternalKnowledgeAutoBindPanel'
import type { KnowledgePermissionRefreshHandle } from './KnowledgePermissionRefreshControls'
import PublicKnowledgeScopePanel from './PublicKnowledgeScopePanel'
import BindingActionButton from './BindingActionButton'

import {
  bindAgentKnowledgeVisibility,
  unbindAgentKnowledgeVisibility,
  getAgentKnowledgeBindingStatus,
  getAgentKnowledgeAllUsersStatus,
  updateAgentKnowledgeAllUsers,
  listAgentKnowledgeSubjectOptions,
  listAgentKnowledgeResourceOptions,
  updateAgentKnowledgeBindingsBatch,
  type AgentKnowledgeBindingStatus,
  type AgentKnowledgeBindingSubject,
  type AgentKnowledgeResourceOption,
  type AgentKnowledgeAllUsersStatus,
} from '../../api/agents'
import { getAPIErrorMessage } from '../../api/client'

const { Text, Title } = Typography
const PAGE_SIZE = 20
const SUBJECT_PAGE_SIZE = 20

type SubjectChoice = {
  availability?: 'active' | 'disabled' | 'unavailable'
  key: string
  label: string
  identitySource?: string
  subject: AgentKnowledgeBindingSubject
}

export default function KnowledgeBindingsPanel({ agentId, onPublicScopeChanged }: {
  agentId: string
  onPublicScopeChanged?: () => void
}) {
  const { t } = useTranslation()
  const refreshActionRef = useRef<KnowledgePermissionRefreshHandle>(null)
  const [refreshAvailable, setRefreshAvailable] = useState(false)
  const [refreshBusy, setRefreshBusy] = useState(false)
  const [resourceFilter, setResourceFilter] = useState<'bound' | 'unbound'>('bound')
  const resourceRequest = useRef(0)
  const [resources, setResources] = useState<AgentKnowledgeResourceOption[]>([])
  const [resourceTotal, setResourceTotal] = useState(0)
  const [resourceOffset, setResourceOffset] = useState(0)
  const [resourceSearch, setResourceSearch] = useState('')
  const [selectedResourceIds, setSelectedResourceIds] = useState<string[]>([])
  const [managedResources, setManagedResources] = useState<AgentKnowledgeResourceOption[]>([])
  const managedResourceIds = useMemo(() => managedResources.map((resource) => resource.knowledge_resource_id), [managedResources])
  const [allUsersStatuses, setAllUsersStatuses] = useState<AgentKnowledgeAllUsersStatus[]>([])
  const [allUsersLoading, setAllUsersLoading] = useState(false)
  const [statuses, setStatuses] = useState<Map<string, AgentKnowledgeBindingStatus>>(new Map())
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [statusRevision, setStatusRevision] = useState(0)
  const [visibilityRevision, setVisibilityRevision] = useState(0)
  const [statusLoading, setStatusLoading] = useState(false)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [pickerTab, setPickerTab] = useState<'users' | 'groups' | 'sources'>('users')
  const [pickerRows, setPickerRows] = useState<SubjectChoice[]>([])
  const [pickerFilter, setPickerFilter] = useState<'available' | 'bound'>('available')
  const [pickerNextOffset, setPickerNextOffset] = useState<number | null>(null)
  const [pickerPreviousOffsets, setPickerPreviousOffsets] = useState<number[]>([])
  const [pickerOffset, setPickerOffset] = useState(0)
  const [pickerSearch, setPickerSearch] = useState('')
  const [debouncedPickerSearch, setDebouncedPickerSearch] = useState('')
  const [pickerLoading, setPickerLoading] = useState(false)
  const pickerRequest = useRef(0)
  const [pickerStatuses, setPickerStatuses] = useState<
    Map<string, AgentKnowledgeBindingStatus['status']>
  >(new Map())
  const [pickerInheritance, setPickerInheritance] = useState<Map<string, string[]>>(new Map())
  const [draftSubjects, setDraftSubjects] = useState<SubjectChoice[]>([])

  const selectedResources = useMemo(
    () =>
      resources.filter((resource) =>
        selectedResourceIds.includes(resource.knowledge_resource_id) && resource.agent_bound !== false,
      ),
    [resources, selectedResourceIds],
  )
  const operationSubjects = draftSubjects
  const subjectStatusesReady = operationSubjects.length > 0 && !statusLoading
    && managedResourceIds.length > 0 && managedResourceIds.every((id) => statuses.has(id))
  const canAddSubjects = subjectStatusesReady
    && operationSubjects.every((choice) => !choice.availability || choice.availability === 'active')
    && [...statuses.values()].some((row) => row.status !== 'BOUND')
  const canRemoveSubjects = subjectStatusesReady && [...statuses.values()].some((row) => row.status !== 'UNBOUND')
  const allUsersReady = !allUsersLoading && managedResourceIds.length > 0
    && managedResourceIds.every((id) => allUsersStatuses.some((row) => row.knowledge_resource_id === id))
  const canBindAllUsers = allUsersReady && allUsersStatuses.some((row) => row.status !== 'BOUND')
  const canUnbindAllUsers = allUsersReady && allUsersStatuses.some((row) => row.status !== 'UNBOUND')

  useEffect(() => {
    if (!pickerOpen) return
    let cancelled = false
    setAllUsersLoading(true)
    setAllUsersStatuses([])
    void getAgentKnowledgeAllUsersStatus(agentId, managedResourceIds)
      .then((response) => { if (!cancelled) setAllUsersStatuses(response.data.items) })
      .catch((requestError) => {
        if (!cancelled) setError(getAPIErrorMessage(requestError, t('knowledgeBindings.loadFailed')))
      })
      .finally(() => { if (!cancelled) setAllUsersLoading(false) })
    return () => { cancelled = true }
  }, [agentId, managedResourceIds, pickerOpen, statusRevision, t])

  const loadResources = useCallback(async (preserveRows = false) => {
    const request = ++resourceRequest.current
    setLoading(true)
    if (!preserveRows) {
      setSelectedResourceIds([])
      setResources([])
    }
    setError(null)
    try {
      const response = await listAgentKnowledgeResourceOptions(agentId, {
        offset: resourceOffset,
        limit: PAGE_SIZE,
        search: resourceSearch || undefined,
        binding_status: resourceFilter,
      })
      if (request !== resourceRequest.current) return
      if (resourceOffset > 0 && resourceOffset >= response.data.total) {
        setResourceOffset(Math.max(0, Math.floor((response.data.total - 1) / PAGE_SIZE) * PAGE_SIZE))
        return
      }
      setResources(response.data.items)
      setResourceTotal(response.data.total)
      if (preserveRows) {
        const visibleIds = new Set(response.data.items.map((item) => item.knowledge_resource_id))
        setSelectedResourceIds((ids) => ids.filter((id) => visibleIds.has(id)))
      } else setSelectedResourceIds([])
    } catch (requestError) {
      if (request === resourceRequest.current) {
        const detail = getAPIErrorMessage(requestError, preserveRows ? '' : t('knowledgeBindings.loadFailed'))
        setError(preserveRows ? [t('knowledgeBindings.reloadFailedStale'), detail].filter(Boolean).join(' ') : detail)
      }
    } finally {
      if (request === resourceRequest.current) setLoading(false)
    }
  }, [agentId, resourceFilter, resourceOffset, resourceSearch, t])

  useEffect(() => {
    void loadResources()
    return () => { resourceRequest.current += 1 }
  }, [loadResources])

  useEffect(() => {
    if (
      !pickerOpen ||
      managedResourceIds.length === 0 ||
      operationSubjects.length === 0
    ) {
      setStatuses(new Map())
      setStatusLoading(false)
      return
    }
    let cancelled = false
    setStatusLoading(true)
    setStatuses(new Map())
    void getAgentKnowledgeBindingStatus(
      agentId,
      operationSubjects.map((choice) => choice.subject),
      managedResourceIds,
    )
      .then((response) => {
        if (!cancelled) setStatuses(
          new Map(response.data.items.map((item) => [item.knowledge_resource_id, item])),
        )
      })
      .catch((requestError) => {
        if (!cancelled) setError(getAPIErrorMessage(requestError, t('knowledgeBindings.loadFailed')))
      })
      .finally(() => { if (!cancelled) setStatusLoading(false) })
    return () => { cancelled = true }
  }, [agentId, operationSubjects, pickerOpen, managedResourceIds, statusRevision, t])

  useEffect(() => {
    if (!pickerOpen || pickerSearch === debouncedPickerSearch) return
    const timer = window.setTimeout(() => setDebouncedPickerSearch(pickerSearch), 300)
    return () => window.clearTimeout(timer)
  }, [pickerOpen, pickerSearch, debouncedPickerSearch])

  const loadPicker = useCallback(async () => {
    if (!pickerOpen || pickerSearch !== debouncedPickerSearch) return
    const request = ++pickerRequest.current
    setPickerLoading(true)
    setPickerRows([])
    setPickerStatuses(new Map())
    setPickerInheritance(new Map())
    setPickerNextOffset(null)
    try {
      const response = await listAgentKnowledgeSubjectOptions(agentId, {
        knowledge_resource_ids: managedResourceIds,
        category: pickerTab,
        binding_status: pickerFilter,
        offset: pickerOffset,
        limit: SUBJECT_PAGE_SIZE,
        search: debouncedPickerSearch || undefined,
      })
      if (request !== pickerRequest.current) return
      const rows = response.data.items.map((item) => ({
        key: JSON.stringify(item.subject),
        label: item.label,
        identitySource: item.identity_source ?? undefined,
        availability: item.availability,
        subject: item.subject,
      }))
      setPickerRows(rows)
      setPickerNextOffset(response.data.next_offset)
      setPickerStatuses(new Map(rows.map((row, index) => [row.key, response.data.items[index].status])))
      setPickerInheritance(new Map(rows.map((row, index) => [row.key, response.data.items[index].inheritance_sources])))
    } catch (requestError) {
      if (request === pickerRequest.current) setError(getAPIErrorMessage(requestError, t('knowledgeBindings.loadFailed')))
    } finally {
      if (request === pickerRequest.current) setPickerLoading(false)
    }
  }, [agentId, managedResourceIds, pickerFilter, pickerOffset, pickerOpen, pickerSearch, debouncedPickerSearch, pickerTab, t])

  useEffect(() => {
    void loadPicker()
    return () => { pickerRequest.current += 1 }
  }, [loadPicker, statusRevision])

  const currentPageSelected =
    resources.length > 0 &&
    resources.every((resource) => selectedResourceIds.includes(resource.knowledge_resource_id))

  const applyResourceVisibility = async (ids: string[], operation: 'BIND' | 'UNBIND') => {
    if (busy || loading || ids.length === 0) return
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      await (operation === 'BIND' ? bindAgentKnowledgeVisibility : unbindAgentKnowledgeVisibility)(agentId, ids)
      await loadResources()
      setVisibilityRevision((value) => value + 1)
      onPublicScopeChanged?.()
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('knowledgeBindings.updateFailed')))
    } finally {
      setBusy(false)
    }
  }

  const apply = async (operation: 'BIND' | 'UNBIND') => {
    if (busy || (operation === 'BIND' ? !canAddSubjects : !canRemoveSubjects)) return
    const operations = managedResourceIds.map((resourceId) => ({
      operation,
      subjects: operationSubjects.map((choice) => choice.subject),
      targets: [{ knowledge_resource_id: resourceId }],
    }))
    if (operations.length === 0) {
      setNotice(t('agentKnowledgeWorkflow.noBindingChanges'))
      return
    }
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      await updateAgentKnowledgeBindingsBatch(agentId, {
        operations,
        activate_scoped_mode: false,
      })
      setSelectedResourceIds([])
      resetPickerContext()
      setStatusRevision((value) => value + 1)
      await loadResources()
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('knowledgeBindings.updateFailed')))
    } finally {
      setBusy(false)
    }
  }

  const applyAllUsers = async (operation: 'BIND' | 'UNBIND') => {
    if (busy || (operation === 'BIND' ? !canBindAllUsers : !canUnbindAllUsers)) return
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      await updateAgentKnowledgeAllUsers(agentId, managedResourceIds, operation)
      resetPickerContext()
      setStatusRevision((value) => value + 1)
      await loadResources()
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('knowledgeBindings.updateFailed')))
    } finally {
      setBusy(false)
    }
  }

  const openManager = (targets: AgentKnowledgeResourceOption[]) => {
    setManagedResources(targets)
    setDraftSubjects([])
    setStatuses(new Map())
    setPickerOffset(0)
    setPickerPreviousOffsets([])
    setPickerFilter('available')
    setPickerSearch('')
    setDebouncedPickerSearch('')
    setError(null)
    setNotice(null)
    setPickerOpen(true)
  }
  const inheritanceLabel = (sources: string[]) => sources.map((source) =>
    source === 'ALL_AGENT_KNOWLEDGE' ? t('knowledgeBindings.legacyMode')
      : source === 'SELECT_ALL' ? t('agentKnowledgeWorkflow.allUsers')
        : source === 'SELECT_ALL_EXCEPT' ? t('agentKnowledgeWorkflow.selectAllExcept') : source,
  ).join(' / ')

  const updateDraftSelection = (keys: React.Key[]) => {
    setNotice(null)
    setStatuses(new Map())
    const selected = new Set(keys.map(String))
    const currentKeys = new Set(pickerRows.map((row) => row.key))
    const retained = draftSubjects.filter((choice) => !currentKeys.has(choice.key))
    setDraftSubjects([
      ...retained,
      ...pickerRows.filter((choice) => selected.has(choice.key)),
    ])
  }

  const resetPickerContext = () => {
    pickerRequest.current += 1
    setPickerOffset(0)
    setPickerPreviousOffsets([])
    setPickerNextOffset(null)
    setPickerRows([])
    setPickerStatuses(new Map())
    setPickerInheritance(new Map())
    setDraftSubjects([])
    setStatuses(new Map())
    setNotice(null)
    setError(null)
  }

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <div>
        <Title level={4} style={{ marginBlock: 0 }}>
          {t('agentKnowledgeWorkflow.bindKnowledge')}
        </Title>
        <Text type="secondary">{t('agentKnowledgeWorkflow.bindKnowledgeDescription')}</Text>
      </div>
      {error && <Alert type="error" showIcon message={error} />}
      {notice && <Alert type="info" showIcon message={notice} closable onClose={() => setNotice(null)} />}
      <PublicKnowledgeScopePanel
        key={`${agentId}:${visibilityRevision}`}
        agentId={agentId}
        onChanged={() => {
          setSelectedResourceIds([])
          setPickerOpen(false)
          setStatusRevision((value) => value + 1)
          void loadResources()
          onPublicScopeChanged?.()
        }}
      />
      <ExternalKnowledgeAutoBindPanel
        key={agentId}
        agentId={agentId}
        refreshActionRef={refreshActionRef}
        selectedResourceIds={selectedResources.map((resource) => resource.knowledge_resource_id)}
        onRefreshAvailable={setRefreshAvailable}
        onRefreshBusy={setRefreshBusy}
        onChanged={() => { void loadResources(true); setStatusRevision((value) => value + 1) }}
      />
      <Input.Search
        allowClear
        placeholder={t('agentKnowledgeWorkflow.searchKnowledge')}
        value={resourceSearch}
        onChange={(event) => {
          setSelectedResourceIds([])
          setResourceSearch(event.target.value)
          setResourceOffset(0)
        }}
      />
      <Radio.Group
        value={resourceFilter}
        disabled={busy}
        onChange={(event) => {
          setResourceFilter(event.target.value)
          setResourceOffset(0)
          setSelectedResourceIds([])
          setNotice(null)
        }}
        optionType="button"
        options={[
          { value: 'unbound', label: t('agentKnowledgeWorkflow.bindingStates.UNBOUND') },
          { value: 'bound', label: t('agentKnowledgeWorkflow.bindingStates.BOUND') },
        ]}
      />
      <Table
        rowKey="knowledge_resource_id"
        loading={loading}
        dataSource={resources}
        rowSelection={{
                selectedRowKeys: selectedResourceIds,
                getCheckboxProps: () => ({ disabled: busy || loading }),
                onChange: (keys) => setSelectedResourceIds(keys.map(String)),
              }}
        pagination={{
          current: Math.floor(resourceOffset / PAGE_SIZE) + 1,
          pageSize: PAGE_SIZE,
          total: resourceTotal,
          showSizeChanger: false,
          onChange: (page) => {
            setSelectedResourceIds([])
            setResourceOffset((page - 1) * PAGE_SIZE)
          },
        }}
        columns={[
          {
            title: t('knowledgeBindings.knowledgeBases'),
            render: (_value, resource: AgentKnowledgeResourceOption) => (
              <Space direction="vertical" size={0}>
                <Text strong>{resource.name ?? resource.knowledge_resource_id}</Text>
                <Text type="secondary">{resource.kb_id}</Text>
              </Space>
            ),
          },
          { title: 'Space', dataIndex: 'space_name' },
          { title: t('agentKnowledgeWorkflow.knowledgeVisibility'), render: (_value, resource: AgentKnowledgeResourceOption) => (
            <Tag color={resource.agent_bound === false ? 'default' : 'green'}>
              {t(`agentKnowledgeWorkflow.bindingStates.${resource.agent_bound === false ? 'UNBOUND' : 'BOUND'}`)}
            </Tag>
          ) },
          {
            title: t('agentKnowledgeWorkflow.accessRange'),
            render: (_value, resource: AgentKnowledgeResourceOption) => (
              <Space direction="vertical" size={4}>
                <Tag color={resource.access_scope === 'ALL_USERS' ? 'green' : resource.access_scope === 'SELECTED' ? 'blue' : 'default'}>
                  {t(`agentKnowledgeWorkflow.accessRanges.${resource.access_scope ?? 'UNCONFIGURED'}`)}
                </Tag>
                {!!resource.inheritance_sources?.length && <Text type="secondary">
                  {t('agentKnowledgeWorkflow.knowledgeInheritedFrom', { sources: inheritanceLabel(resource.inheritance_sources) })}
                </Text>}
              </Space>
            ),
          },
          {
            title: t('agentKnowledgeWorkflow.identitySourceId'),
            render: (_value, resource: AgentKnowledgeResourceOption) => {
              if (resource.identity_source_ids.length === 0) return '—'
              return (
                <Space wrap>
                  {resource.identity_source_ids.map((sourceId) => (
                    <Tag key={sourceId}>{sourceId}</Tag>
                  ))}
                </Space>
              )
            },
          },
          {
            title: t('common.actions'),
            render: (_value: unknown, resource: AgentKnowledgeResourceOption) => resource.agent_bound === false ? <Button
              type="link"
              disabled={busy || loading}
              onClick={() => void applyResourceVisibility([resource.knowledge_resource_id], 'BIND')}
            >{t('agentKnowledgeWorkflow.bindSingleKnowledge')}</Button> : <Space><Button
              type="link"
              disabled={busy || loading}
              onClick={() => openManager([resource])}
            >{t('agentKnowledgeWorkflow.bindUsers')}</Button>
              {refreshAvailable && <Button type="link" disabled={busy || loading || refreshBusy} onClick={() => refreshActionRef.current?.refreshResource(resource.knowledge_resource_id)}>{t('knowledgeBindings.refreshSinglePermissions')}</Button>}
            </Space>,
          },
        ]}
      />
      <Space style={{ justifyContent: 'space-between', width: '100%' }}>
          <Button
            aria-label={t(currentPageSelected ? 'agentKnowledgeWorkflow.clearCurrentPage' : 'agentKnowledgeWorkflow.selectCurrentPage')}
            disabled={busy || loading || resources.length === 0}
            onClick={() => setSelectedResourceIds(currentPageSelected ? [] : resources.map((resource) => resource.knowledge_resource_id))}
          >
            {t(currentPageSelected ? 'agentKnowledgeWorkflow.clearCurrentPage' : 'agentKnowledgeWorkflow.selectCurrentPage')}
          </Button>
          {selectedResourceIds.length > 0 && <Space>
            <Button
              type="primary"
              danger={resourceFilter === 'bound'}
              disabled={busy || loading}
              loading={busy}
              onClick={() => void applyResourceVisibility(selectedResourceIds, resourceFilter === 'bound' ? 'UNBIND' : 'BIND')}
            >
              {t(resourceFilter === 'bound' ? 'agentKnowledgeWorkflow.unbindSelectedKnowledge' : 'agentKnowledgeWorkflow.bindSelectedKnowledge')}
            </Button>
            {resourceFilter === 'bound' && selectedResources.length > 1 &&
            <Button
              disabled={busy || loading}
              onClick={() => openManager(selectedResources)}
            >
              {t('agentKnowledgeWorkflow.bulkManageBindings')}
            </Button>}
          </Space>}
        </Space>

      <Drawer
        open={pickerOpen}
        width={760}
        title={managedResources.length === 1
          ? t('agentKnowledgeWorkflow.manageKnowledgeUsers', { name: managedResources[0].name ?? managedResources[0].kb_id })
          : t('agentKnowledgeWorkflow.bulkManageBindings')}
        onClose={() => { if (!busy) setPickerOpen(false) }}
        footer={
          <Space style={{ justifyContent: 'flex-end', width: '100%' }}>
            <Button disabled={busy} onClick={() => setPickerOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button
              type={pickerFilter === 'available' ? 'primary' : 'default'}
              danger={pickerFilter === 'bound'}
              loading={busy}
              disabled={busy || (pickerFilter === 'available' ? !canAddSubjects : !canRemoveSubjects)}
              onClick={() => void apply(pickerFilter === 'available' ? 'BIND' : 'UNBIND')}
            >
              {t(pickerFilter === 'available' ? 'agentKnowledgeWorkflow.addToKnowledge' : 'agentKnowledgeWorkflow.removeFromKnowledge')}
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Alert
            type="info"
            showIcon
            message={t('agentKnowledgeWorkflow.selectedKnowledgeCount', {
              count: managedResourceIds.length,
            })}
            description={t('agentKnowledgeWorkflow.addRemoveHint')}
          />
          {error && <Alert type="error" showIcon message={error} />}
          {notice && <Alert type="info" showIcon message={notice} />}
          <Alert type="info" showIcon message={t('agentKnowledgeWorkflow.knowledgeInheritanceHint')} />
          {(
            <>
              <Space wrap>
                <BindingActionButton
                  canBind={canBindAllUsers}
                  canUnbind={canUnbindAllUsers}
                  disabled={!allUsersReady}
                  loading={busy}
                  bindLabel={t('agentKnowledgeWorkflow.bindAllUsers')}
                  unbindLabel={t('agentKnowledgeWorkflow.unbindAllUsers')}
                  onBind={() => void applyAllUsers('BIND')}
                  onUnbind={() => void applyAllUsers('UNBIND')}
                />
              </Space>
              <Text type="secondary">{t('agentKnowledgeWorkflow.selectAllExclusionsHint')}</Text>
                <>
                  <Tabs
                    activeKey={pickerTab}
                    onChange={(key) => {
                      resetPickerContext()
                      setPickerTab(key as 'users' | 'groups' | 'sources')
                    }}
                    items={[
                      { key: 'users', label: t('agentKnowledgeWorkflow.users') },
                      { key: 'groups', label: t('agentKnowledgeWorkflow.departmentsAndGroups') },
                      { key: 'sources', label: t('agentKnowledgeWorkflow.enterpriseSources') },
                    ]}
                  />
                  <Input.Search
                    allowClear
                    placeholder={t('agentKnowledgeWorkflow.searchSubjects')}
                    value={pickerSearch}
                    onChange={(event) => {
                      resetPickerContext()
                      setPickerLoading(true)
                      setPickerSearch(event.target.value)
                    }}
                  />
                  <Text type="secondary">{t('agentKnowledgeWorkflow.knowledgeAssignedSubjectsHint')}</Text>
                  <Radio.Group
                    optionType="button"
                    value={pickerFilter}
                    onChange={(event) => {
                      resetPickerContext()
                      setPickerFilter(event.target.value)
                    }}
                    options={[
                      { value: 'available', label: t('agentKnowledgeWorkflow.availableKnowledgeSubjects') },
                      { value: 'bound', label: t('agentKnowledgeWorkflow.boundOnly') },
                    ]}
                  />
                  <Table
                    rowKey="key"
                    size="small"
                    loading={pickerLoading}
                    dataSource={pickerRows}
                    rowSelection={{
                      preserveSelectedRowKeys: true,
                      selectedRowKeys: draftSubjects.map((choice) => choice.key),
                      onChange: updateDraftSelection,
                    }}
                    pagination={false}
                    columns={[
                      {
                        title: t('agentKnowledgeWorkflow.subject'),
                        dataIndex: 'label',
                        render: (_value, choice: SubjectChoice) => (
                          <Space wrap>
                            {choice.label}
                            {choice.availability && choice.availability !== 'active' && (
                              <Tag>{t(`agentKnowledgeWorkflow.userAvailability.${choice.availability}`)}</Tag>
                            )}
                          </Space>
                        ),
                      },
                      ...(pickerTab === 'users' ? [{
                        title: t('agentKnowledgeWorkflow.identitySourceName'),
                        dataIndex: 'identitySource',
                      }] : []),
                      {
                        title: t('agentKnowledgeWorkflow.selectedKnowledgeStatus'),
                        render: (_value, choice: SubjectChoice) => {
                          const status = pickerStatuses.get(choice.key)
                          if (!status) return '—'
                          return (
                            <Space direction="vertical" size={4}>
                            <Tag
                              color={
                                status === 'BOUND'
                                  ? 'green'
                                  : status === 'PARTIAL'
                                    ? 'gold'
                                    : 'default'
                              }
                            >
                              {t(
                                `agentKnowledgeWorkflow.${managedResourceIds.length > 1 ? 'bulkBindingStates' : 'bindingStates'}.${status}`,
                              )}
                            </Tag>
                            {!!pickerInheritance.get(choice.key)?.length && <Text type="secondary">
                              {t('agentKnowledgeWorkflow.knowledgeInheritedFrom', { sources: inheritanceLabel(pickerInheritance.get(choice.key)!) })}
                            </Text>}
                            </Space>
                          )
                        },
                      },
                    ]}
                  />
                  <Space>
                    <Button disabled={pickerLoading} onClick={() => updateDraftSelection([...draftSubjects.map((choice) => choice.key), ...pickerRows.map((choice) => choice.key)])}>
                      {t('agentKnowledgeWorkflow.selectCurrentPage')}
                    </Button>
                    <Button onClick={() => { setDraftSubjects([]); setStatuses(new Map()); setNotice(null) }}>{t('polarragAccess.clearAllUsers')}</Button>
                    <Button disabled={pickerLoading || pickerPreviousOffsets.length === 0} onClick={() => {
                      setPickerOffset(pickerPreviousOffsets[pickerPreviousOffsets.length - 1])
                      setPickerPreviousOffsets((values) => values.slice(0, -1))
                      setNotice(null)
                    }}>{t('polarragAccess.previousCandidatePage')}</Button>
                    <Button disabled={pickerLoading || pickerNextOffset === null} onClick={() => {
                      if (pickerNextOffset === null) return
                      setPickerPreviousOffsets((values) => [...values, pickerOffset])
                      setPickerOffset(pickerNextOffset)
                      setNotice(null)
                    }}>{t('polarragAccess.nextCandidatePage')}</Button>
                  </Space>
                </>
            </>
          )}
        </Space>
      </Drawer>
    </Space>
  )
}
