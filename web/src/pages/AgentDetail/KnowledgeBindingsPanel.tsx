import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import { useTranslation } from 'react-i18next'

import {
  listAgentGroupAssignments,
  listAgentKnowledgeBindings,
  listAgentKnowledgeResourceOptions,
  listAgentUserAssignments,
  updateAgentKnowledgeBindingsBatch,
  type AgentGroupAssignment,
  type AgentKnowledgeBinding,
  type AgentKnowledgeBindingSubject,
  type AgentKnowledgeResourceOption,
  type AgentUserAssignment,
} from '../../api/agents'
import { getAPIErrorMessage } from '../../api/client'

const { Text, Title } = Typography

const PAGE_SIZE = 20
const OPTION_PAGE_SIZE = 50

type Operation = 'BIND' | 'UNBIND'

function groupKey(group: AgentGroupAssignment): string {
  return `${group.group_kind}:${group.id}`
}

function resourceLabel(resource: AgentKnowledgeResourceOption): string {
  const name = resource.name ?? resource.knowledge_resource_id
  const space = resource.space_name ?? resource.space_id ?? '—'
  return `${name} · ${space} / ${resource.kb_id ?? '—'}`
}

export default function KnowledgeBindingsPanel({ agentId }: { agentId: string }) {
  const { t } = useTranslation()
  const [bindings, setBindings] = useState<AgentKnowledgeBinding[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [search, setSearch] = useState('')
  const [mode, setMode] = useState<'LEGACY_ALL' | 'SCOPED'>('LEGACY_ALL')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [operation, setOperation] = useState<Operation | null>(null)
  const [fixedBinding, setFixedBinding] = useState<AgentKnowledgeBinding | null>(null)
  const [users, setUsers] = useState<AgentUserAssignment[]>([])
  const [groups, setGroups] = useState<AgentGroupAssignment[]>([])
  const [resources, setResources] = useState<AgentKnowledgeResourceOption[]>([])
  const [selectedUsers, setSelectedUsers] = useState<string[]>([])
  const [selectedGroups, setSelectedGroups] = useState<string[]>([])
  const [selectedResources, setSelectedResources] = useState<string[]>([])

  const mergeBy = <T,>(current: T[], incoming: T[], key: (item: T) => string): T[] => {
    const values = new Map(current.map((item) => [key(item), item]))
    for (const item of incoming) values.set(key(item), item)
    return [...values.values()]
  }

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const response = await listAgentKnowledgeBindings(agentId, {
        offset,
        limit: PAGE_SIZE,
        search: search || undefined,
      })
      setBindings(response.data.items)
      setTotal(response.data.total)
      setMode(response.data.knowledge_scope_mode)
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('knowledgeBindings.loadFailed')))
    } finally {
      setLoading(false)
    }
  }, [agentId, offset, search, t])

  useEffect(() => {
    void load()
  }, [load])

  const loadOptions = useCallback(async () => {
    try {
      const [userResponse, groupResponse, resourceResponse] = await Promise.all([
        listAgentUserAssignments(agentId, { offset: 0, limit: OPTION_PAGE_SIZE }),
        listAgentGroupAssignments(agentId, { offset: 0, limit: OPTION_PAGE_SIZE }),
        listAgentKnowledgeResourceOptions(agentId, {
          offset: 0,
          limit: OPTION_PAGE_SIZE,
        }),
      ])
      setUsers(userResponse.data.items)
      setGroups(
        groupResponse.data.items.filter((group) =>
          ['department', 'identity_source'].includes(group.group_kind),
        ),
      )
      setResources((current) =>
        mergeBy(
          current,
          resourceResponse.data.items,
          (item) => item.knowledge_resource_id,
        ),
      )
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('knowledgeBindings.loadFailed')))
    }
  }, [agentId, t])

  const searchUsers = async (value: string) => {
    const response = await listAgentUserAssignments(agentId, {
      search: value || undefined,
      offset: 0,
      limit: OPTION_PAGE_SIZE,
    })
    setUsers((current) => mergeBy(current, response.data.items, (item) => item.user_id))
  }

  const searchGroups = async (value: string) => {
    const response = await listAgentGroupAssignments(agentId, {
      search: value || undefined,
      offset: 0,
      limit: OPTION_PAGE_SIZE,
    })
    const supported = response.data.items.filter((group) =>
      ['department', 'identity_source'].includes(group.group_kind),
    )
    setGroups((current) => mergeBy(current, supported, groupKey))
  }

  const searchResources = async (value: string) => {
    const response = await listAgentKnowledgeResourceOptions(agentId, {
      search: value || undefined,
      offset: 0,
      limit: OPTION_PAGE_SIZE,
    })
    setResources((current) =>
      mergeBy(
        current,
        response.data.items,
        (item) => item.knowledge_resource_id,
      ),
    )
  }

  const supportedGroups = useMemo(
    () => new Map(groups.map((group) => [groupKey(group), group])),
    [groups],
  )
  const resourcesById = useMemo(
    () => new Map(resources.map((resource) => [resource.knowledge_resource_id, resource])),
    [resources],
  )

  const openEditor = (nextOperation: Operation, binding?: AgentKnowledgeBinding) => {
    setOperation(nextOperation)
    setFixedBinding(binding ?? null)
    setSelectedUsers(binding?.subject.user_id ? [binding.subject.user_id] : [])
    setSelectedGroups([])
    setSelectedResources(
      binding?.knowledge_resources.map((resource) => resource.knowledge_resource_id) ?? [],
    )
    setUsers([])
    setGroups([])
    setResources(binding?.knowledge_resources ?? [])
    void loadOptions()
  }

  const closeEditor = () => {
    setOperation(null)
    setFixedBinding(null)
    setSelectedUsers([])
    setSelectedGroups([])
    setSelectedResources([])
  }

  const subjects = (): AgentKnowledgeBindingSubject[] => {
    if (fixedBinding) {
      const { type, user_id, department_id, identity_source_id, group_id } = fixedBinding.subject
      return [{ type, user_id, department_id, identity_source_id, group_id }]
    }
    const selectedSubjects: AgentKnowledgeBindingSubject[] = selectedUsers.map(
      (userId) => ({ type: 'USER', user_id: userId }),
    )
    for (const key of selectedGroups) {
      const group = supportedGroups.get(key)
      if (!group) continue
      if (group.group_kind === 'department' && group.department_id) {
        selectedSubjects.push({
          type: 'DEPARTMENT',
          department_id: group.department_id,
        })
      }
      if (
        group.group_kind === 'identity_source' &&
        group.identity_source_id &&
        group.principal_id
      ) {
        selectedSubjects.push({
          type: 'GROUP',
          identity_source_id: group.identity_source_id,
          group_id: group.principal_id,
        })
      }
    }
    return selectedSubjects
  }

  const submit = async () => {
    if (!operation) return
    const selectedSubjects = subjects()
    const targets: Array<
      { space_id: string; kb_id: string } | { knowledge_resource_id: string }
    > = []
    for (const resourceId of selectedResources) {
      const resource = resourcesById.get(resourceId)
      if (resource?.space_id && resource.kb_id) {
        targets.push({ space_id: resource.space_id, kb_id: resource.kb_id })
      } else {
        targets.push({ knowledge_resource_id: resourceId })
      }
    }
    if (selectedSubjects.length === 0 || targets.length === 0) return
    setBusy(true)
    setError(null)
    try {
      await updateAgentKnowledgeBindingsBatch(agentId, {
        operations: [{ operation, subjects: selectedSubjects, targets }],
        activate_scoped_mode: operation === 'BIND',
      })
      closeEditor()
      await load()
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('knowledgeBindings.updateFailed')))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Space align="start" style={{ justifyContent: 'space-between', width: '100%' }}>
        <div>
          <Space>
            <Title level={4} style={{ marginBlock: 0 }}>
              {t('knowledgeBindings.title')}
            </Title>
            <Tag color={mode === 'SCOPED' ? 'blue' : 'default'}>
              {t(
                mode === 'SCOPED'
                  ? 'knowledgeBindings.scopedMode'
                  : 'knowledgeBindings.legacyMode',
              )}
            </Tag>
          </Space>
          <div>
            <Text type="secondary">{t('knowledgeBindings.description')}</Text>
          </div>
        </div>
        <Space>
          <Button danger onClick={() => openEditor('UNBIND')}>
            {t('knowledgeBindings.unbind')}
          </Button>
          <Button type="primary" onClick={() => openEditor('BIND')}>
            {t('knowledgeBindings.bind')}
          </Button>
        </Space>
      </Space>
      {error && <Alert type="error" showIcon message={error} />}
      <Input.Search
        allowClear
        aria-label={t('knowledgeBindings.search')}
        placeholder={t('knowledgeBindings.search')}
        value={search}
        onChange={(event) => {
          setSearch(event.target.value)
          setOffset(0)
        }}
      />
      <Table
        rowKey="binding_id"
        loading={loading}
        dataSource={bindings}
        locale={{ emptyText: t('knowledgeBindings.empty') }}
        pagination={{
          current: Math.floor(offset / PAGE_SIZE) + 1,
          pageSize: PAGE_SIZE,
          total,
          showSizeChanger: false,
          onChange: (page) => setOffset((page - 1) * PAGE_SIZE),
        }}
        columns={[
          {
            title: t('knowledgeBindings.source'),
            render: (_value, row: AgentKnowledgeBinding) => (
              <Tag color={row.origin === 'MANUAL' ? 'blue' : 'gold'}>
                {t(
                  row.origin === 'MANUAL'
                    ? 'knowledgeBindings.manual'
                    : 'knowledgeBindings.externalSync',
                )}
              </Tag>
            ),
          },
          {
            title: t('knowledgeBindings.subject'),
            render: (_value, row: AgentKnowledgeBinding) => (
              <Space direction="vertical" size={0}>
                <Text>{row.subject.display_name}</Text>
                <Text type="secondary">{t(`knowledgeBindings.${row.subject.type.toLowerCase()}`)}</Text>
              </Space>
            ),
          },
          {
            title: t('knowledgeBindings.knowledgeBases'),
            render: (_value, row: AgentKnowledgeBinding) => (
              <Space wrap size={[4, 4]}>
                {row.knowledge_resources.map((resource) => (
                  <Tag key={resource.knowledge_resource_id}>{resource.name ?? resource.knowledge_resource_id}</Tag>
                ))}
              </Space>
            ),
          },
          {
            title: t('knowledgeBindings.actions'),
            render: (_value, row: AgentKnowledgeBinding) =>
              row.origin === 'MANUAL' ? (
                <Button
                  danger
                  size="small"
                  aria-label={t('knowledgeBindings.unbindSubject', {
                    name: row.subject.display_name,
                  })}
                  onClick={() => openEditor('UNBIND', row)}
                >
                  {t('knowledgeBindings.unbind')}
                </Button>
              ) : (
                <Text type="secondary">{t('knowledgeBindings.externallyManaged')}</Text>
              ),
          },
        ]}
      />
      <Modal
        open={operation !== null}
        title={t(
          operation === 'UNBIND'
            ? 'knowledgeBindings.unbindTitle'
            : 'knowledgeBindings.bindTitle',
        )}
        okText={t(
          operation === 'UNBIND'
            ? 'knowledgeBindings.unbind'
            : 'knowledgeBindings.bind',
        )}
        okButtonProps={{
          danger: operation === 'UNBIND',
          disabled: subjects().length === 0 || selectedResources.length === 0,
        }}
        confirmLoading={busy}
        onOk={() => void submit()}
        onCancel={closeEditor}
      >
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          {fixedBinding && (
            <Alert
              type="warning"
              showIcon
              message={t('knowledgeBindings.fixedSubject', {
                name: fixedBinding.subject.display_name,
              })}
            />
          )}
          <Select
            aria-label={t('knowledgeBindings.users')}
            mode="multiple"
            showSearch
            filterOption={false}
            value={selectedUsers}
            onChange={setSelectedUsers}
            onSearch={(value) => void searchUsers(value)}
            disabled={fixedBinding !== null}
            placeholder={t('knowledgeBindings.selectUsers')}
            options={users.map((user) => ({
              value: user.user_id,
              label: `${user.user_name} (${user.user_id})`,
            }))}
            style={{ width: '100%' }}
          />
          <Select
            aria-label={t('knowledgeBindings.groups')}
            mode="multiple"
            showSearch
            filterOption={false}
            value={selectedGroups}
            onChange={setSelectedGroups}
            onSearch={(value) => void searchGroups(value)}
            disabled={fixedBinding !== null}
            placeholder={t('knowledgeBindings.selectGroups')}
            options={groups.map((group) => ({
              value: groupKey(group),
              label:
                group.group_kind === 'department'
                  ? group.department_name
                  : group.external_group_name ?? group.principal_id,
            }))}
            style={{ width: '100%' }}
          />
          <Select
            aria-label={t('knowledgeBindings.knowledgeBases')}
            mode="multiple"
            showSearch
            filterOption={false}
            value={selectedResources}
            onChange={setSelectedResources}
            onSearch={(value) => void searchResources(value)}
            placeholder={t('knowledgeBindings.selectKnowledgeBases')}
            options={resources.map((resource) => ({
              value: resource.knowledge_resource_id,
              label: resourceLabel(resource),
            }))}
            style={{ width: '100%' }}
          />
        </Space>
      </Modal>
    </Space>
  )
}
