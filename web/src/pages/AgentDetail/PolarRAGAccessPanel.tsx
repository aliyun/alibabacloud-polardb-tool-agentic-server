import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Checkbox,
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
  createAgentGroupAssignment,
  createAgentPolarRAGBinding,
  createAgentUserAssignment,
  createAllAgentUserAssignments,
  deleteAgentGroupAssignment,
  deleteAgentUserAssignment,
  forceRevokeAgentUserToken,
  listAgentGroupAssignments,
  listAgentGroupOptions,
  listAgentPolarRAGBindings,
  listAgentUserAssignments,
  listAgentUserOptions,
  type AgentGroupOption,
  type AgentGroupAssignment,
  type AgentPolarRAGBinding,
  type AgentUserOption,
  type AgentUserAssignment,
} from '../../api/agents'
import { getAPIErrorMessage } from '../../api/client'
import { formatDateTime } from '../../i18n/format'
import {
  listPolarRAGInstances,
  type PolarRAGInstance,
} from '../../api/polarrag'
import EnterpriseAccessDrawer from './EnterpriseAccessDrawer'
import KnowledgeBindingsPanel from './KnowledgeBindingsPanel'

const { Text, Title } = Typography

const ASSIGNMENT_PAGE_SIZE = 20
const CANDIDATE_PAGE_SIZE = 50

function groupKey(group: AgentGroupOption): string {
  if (group.group_kind === 'department') {
    return `department:${group.department_id}`
  }
  if (group.group_kind === 'identity_source') {
    return `identity-source:${group.identity_source_id}:${group.principal_id}`
  }
  if (group.group_kind === 'identity_source_all') {
    return `identity-source-all:${group.identity_source_id}`
  }
  return `enterprise:${group.identity_domain}:${group.provider}:${group.principal_id}`
}

export default function PolarRAGAccessPanel({
  agentId,
  bindings,
  onBindingsChange,
}: {
  agentId: string
  bindings: AgentPolarRAGBinding[]
  onBindingsChange: (bindings: AgentPolarRAGBinding[]) => void
}) {
  const { t, i18n } = useTranslation()
  const [instances, setInstances] = useState<PolarRAGInstance[]>([])
  const [assignments, setAssignments] = useState<AgentUserAssignment[]>([])
  const [assignmentTotal, setAssignmentTotal] = useState(0)
  const [assignmentOffset, setAssignmentOffset] = useState(0)
  const [assignmentSearch, setAssignmentSearch] = useState('')
  const [groupAssignments, setGroupAssignments] = useState<
    AgentGroupAssignment[]
  >([])
  const [groupAssignmentTotal, setGroupAssignmentTotal] = useState(0)
  const [groupAssignmentOffset, setGroupAssignmentOffset] = useState(0)
  const [groupAssignmentSearch, setGroupAssignmentSearch] = useState('')
  const [userOptions, setUserOptions] = useState<AgentUserOption[]>([])
  const [userOptionTotal, setUserOptionTotal] = useState(0)
  const [userOptionOffset, setUserOptionOffset] = useState(0)
  const [userOptionSearch, setUserOptionSearch] = useState('')
  const [groupOptions, setGroupOptions] = useState<AgentGroupOption[]>([])
  const [groupOptionTotal, setGroupOptionTotal] = useState(0)
  const [groupOptionOffset, setGroupOptionOffset] = useState(0)
  const [groupOptionSearch, setGroupOptionSearch] = useState('')
  const [selectedUsers, setSelectedUsers] = useState<string[]>([])
  const [selectedUserOptions, setSelectedUserOptions] = useState<
    AgentUserOption[]
  >([])
  const [selectedGroups, setSelectedGroups] = useState<string[]>([])
  const [selectedGroupOptions, setSelectedGroupOptions] = useState<
    AgentGroupOption[]
  >([])
  const [allUsersSelected, setAllUsersSelected] = useState(false)
  const [selectedInstance, setSelectedInstance] = useState<string>()
  const [loading, setLoading] = useState(true)
  const [candidateLoading, setCandidateLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [enterpriseAccessOpen, setEnterpriseAccessOpen] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [
        instanceResponse,
        assignmentResponse,
        groupAssignmentResponse,
      ] =
        await Promise.all([
          listPolarRAGInstances(),
          listAgentUserAssignments(agentId, {
            search: assignmentSearch || undefined,
            offset: assignmentOffset,
            limit: ASSIGNMENT_PAGE_SIZE,
          }),
          listAgentGroupAssignments(agentId, {
            search: groupAssignmentSearch || undefined,
            offset: groupAssignmentOffset,
            limit: ASSIGNMENT_PAGE_SIZE,
          }),
        ])
      setInstances(instanceResponse.data.items)
      setAssignments(assignmentResponse.data.items)
      setAssignmentTotal(assignmentResponse.data.total)
      setGroupAssignments(groupAssignmentResponse.data.items)
      setGroupAssignmentTotal(groupAssignmentResponse.data.total)
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, t('polarragAccess.loadFailed')),
      )
    } finally {
      setLoading(false)
    }
  }, [
    agentId,
    assignmentSearch,
    assignmentOffset,
    groupAssignmentSearch,
    groupAssignmentOffset,
    t,
  ])

  useEffect(() => {
    void load()
  }, [load])

  const loadUserOptions = useCallback(async () => {
    setCandidateLoading(true)
    try {
      const response = await listAgentUserOptions(agentId, {
        search: userOptionSearch || undefined,
        offset: userOptionOffset,
        limit: CANDIDATE_PAGE_SIZE,
      })
      setUserOptions(response.data.items)
      setUserOptionTotal(response.data.total)
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, t('polarragAccess.loadFailed')),
      )
    } finally {
      setCandidateLoading(false)
    }
  }, [agentId, t, userOptionOffset, userOptionSearch])

  const loadGroupOptions = useCallback(async () => {
    setCandidateLoading(true)
    try {
      const response = await listAgentGroupOptions(agentId, {
        search: groupOptionSearch || undefined,
        offset: groupOptionOffset,
        limit: CANDIDATE_PAGE_SIZE,
      })
      setGroupOptions(response.data.items)
      setGroupOptionTotal(response.data.total)
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, t('polarragAccess.loadFailed')),
      )
    } finally {
      setCandidateLoading(false)
    }
  }, [agentId, groupOptionOffset, groupOptionSearch, t])

  useEffect(() => {
    void loadUserOptions()
  }, [loadUserOptions])

  useEffect(() => {
    void loadGroupOptions()
  }, [loadGroupOptions])

  const availableInstances = useMemo(() => {
    const bound = new Set(bindings.map((row) => row.polarrag_instance_id))
    return instances.filter((instance) => !bound.has(instance.id))
  }, [bindings, instances])
  const candidateUsers = useMemo(() => {
    const byId = new Map(selectedUserOptions.map((user) => [user.id, user]))
    for (const user of userOptions) byId.set(user.id, user)
    return [...byId.values()]
  }, [selectedUserOptions, userOptions])
  const candidateGroups = useMemo(() => {
    const byKey = new Map(
      selectedGroupOptions.map((group) => [groupKey(group), group]),
    )
    for (const group of groupOptions) byKey.set(groupKey(group), group)
    return [...byKey.values()]
  }, [groupOptions, selectedGroupOptions])
  const groupLabel = (group: AgentGroupOption): string => {
    const members = t('polarragAccess.memberCount', {
      count: group.member_count,
    })
    if (group.group_kind === 'department') {
      return `${t('polarragAccess.department')} · ${group.department_name} (${members})`
    }
    if (group.group_kind === 'identity_source_all') {
      return `${t('polarragAccess.identitySource')} · ${group.identity_source_name} · ${t('polarragAccess.allSourceUsers')} (${members})`
    }
    if (group.group_kind === 'identity_source') {
      return `${t('polarragAccess.identitySource')} · ${group.identity_source_name} · ${group.external_group_name} (${members})`
    }
    return `${t('polarragAccess.enterprise')} · ${group.provider} · ${group.principal_id} · ${group.identity_domain} (${members})`
  }

  const changeSelectedUsers = (ids: string[]) => {
    const byId = new Map(candidateUsers.map((user) => [user.id, user]))
    setSelectedUsers(ids)
    setSelectedUserOptions(
      ids.flatMap((id) => {
        const user = byId.get(id)
        return user ? [user] : []
      }),
    )
  }

  const changeSelectedGroups = (keys: string[]) => {
    const byKey = new Map(
      candidateGroups.map((group) => [groupKey(group), group]),
    )
    setSelectedGroups(keys)
    setSelectedGroupOptions(
      keys.flatMap((key) => {
        const group = byKey.get(key)
        return group ? [group] : []
      }),
    )
  }

  const mutate = async (operation: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await operation()
      await load()
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, t('polarragAccess.updateFailed')),
      )
    } finally {
      setBusy(false)
    }
  }

  const refreshAfterEnterpriseAccess = async () => {
    await load()
    try {
      const bindingResponse = await listAgentPolarRAGBindings(agentId)
      onBindingsChange(bindingResponse.data.items)
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, t('polarragAccess.loadFailed')),
      )
    }
  }

  return (
    <Space direction="vertical" size={20} style={{ width: '100%' }}>
      {error && <Alert type="error" showIcon message={error} />}
      <div>
        <Title level={4} style={{ marginBlock: 0 }}>
          {t('polarragAccess.title')}
        </Title>
        <Text type="secondary">
          {t('polarragAccess.tabDescription')}
        </Text>
      </div>
      <Alert
        type="info"
        showIcon
        message={t('enterpriseAccess.configure')}
        description={t('enterpriseAccess.entryDescription')}
        action={
          <Button type="primary" onClick={() => setEnterpriseAccessOpen(true)}>
            {t('enterpriseAccess.configure')}
          </Button>
        }
      />
      <Space.Compact style={{ width: '100%' }}>
        <Select
          aria-label={t('polarragAccess.instance')}
          value={selectedInstance}
          onChange={setSelectedInstance}
          placeholder={t('polarragAccess.selectInstance')}
          notFoundContent={
            instances.length > 0 && availableInstances.length === 0
              ? t('polarragAccess.allBound')
              : t('polarragAccess.noneAvailable')
          }
          options={availableInstances.map((instance) => ({
            value: instance.id,
            label: instance.name,
          }))}
          style={{ flex: 1 }}
        />
        <Button
          type="primary"
          disabled={!selectedInstance}
          loading={busy}
          onClick={() => {
            if (!selectedInstance) return
            void mutate(async () => {
              const response = await createAgentPolarRAGBinding(
                agentId,
                selectedInstance,
              )
              onBindingsChange([...bindings, response.data])
            }).then(() => setSelectedInstance(undefined))
          }}
        >
          {t('polarragAccess.bindInstance')}
        </Button>
      </Space.Compact>
      <KnowledgeBindingsPanel agentId={agentId} />
      <div>
        <Title level={4} style={{ marginBlock: 0 }}>
          {t('polarragAccess.assignedGroups')}
        </Title>
        <Text type="secondary">
          {t('polarragAccess.groupsDescription')}
        </Text>
      </div>
      <Space.Compact style={{ width: '100%' }}>
        <Checkbox
          aria-label={t('polarragAccess.selectCurrentGroupPage')}
          checked={
            groupOptions.length > 0 &&
            groupOptions
              .filter((group) => group.group_kind !== 'identity_source_all')
              .every((group) => selectedGroups.includes(groupKey(group)))
          }
          onChange={(event) => {
            const pageKeys = groupOptions
              .filter((group) => group.group_kind !== 'identity_source_all')
              .map(groupKey)
            changeSelectedGroups(
              event.target.checked
                ? [...new Set([...selectedGroups, ...pageKeys])]
                : selectedGroups.filter((key) => !pageKeys.includes(key)),
            )
          }}
          style={{ alignSelf: 'center', paddingInline: 8 }}
        >
          {t('polarragAccess.selectCurrentGroupPage')}
        </Checkbox>
        <Select
          aria-label={t('polarragAccess.group')}
          mode="multiple"
          value={selectedGroups}
          placeholder={t('polarragAccess.selectGroup')}
          options={candidateGroups.map((group) => ({
            value: groupKey(group),
            label: groupLabel(group),
          }))}
          onChange={changeSelectedGroups}
          showSearch
          filterOption={false}
          loading={candidateLoading}
          onSearch={(search) => {
            setGroupOptionSearch(search)
            setGroupOptionOffset(0)
          }}
          popupRender={(menu) => (
            <>
              {menu}
              <Space style={{ display: 'flex', justifyContent: 'center', padding: 8 }}>
                <Button
                  aria-label={t('polarragAccess.previousCandidatePage')}
                  disabled={groupOptionOffset === 0}
                  size="small"
                  type="text"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() =>
                    setGroupOptionOffset(
                      Math.max(0, groupOptionOffset - CANDIDATE_PAGE_SIZE),
                    )
                  }
                >
                  ‹
                </Button>
                <Text type="secondary">
                  {Math.floor(groupOptionOffset / CANDIDATE_PAGE_SIZE) + 1} /{' '}
                  {Math.max(1, Math.ceil(groupOptionTotal / CANDIDATE_PAGE_SIZE))}
                </Text>
                <Button
                  aria-label={t('polarragAccess.nextCandidatePage')}
                  disabled={groupOptionOffset + CANDIDATE_PAGE_SIZE >= groupOptionTotal}
                  size="small"
                  type="text"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() =>
                    setGroupOptionOffset(groupOptionOffset + CANDIDATE_PAGE_SIZE)
                  }
                >
                  ›
                </Button>
              </Space>
            </>
          )}
          style={{ flex: 1 }}
        />
        <Button
          type="primary"
          disabled={selectedGroups.length === 0 || busy}
          loading={busy}
          onClick={() => {
            const groups = selectedGroupOptions
            if (groups.length === 0) return
            void mutate(() =>
              Promise.all(
                groups.map((group) => createAgentGroupAssignment(agentId, group)),
              ),
            ).then(() => changeSelectedGroups([]))
          }}
        >
          {t('polarragAccess.assignGroups')}
        </Button>
      </Space.Compact>
      <Input.Search
        allowClear
        aria-label={t('polarragAccess.searchAssignedGroups')}
        placeholder={t('polarragAccess.searchAssignedGroups')}
        value={groupAssignmentSearch}
        onChange={(event) => {
          setGroupAssignmentSearch(event.target.value)
          setGroupAssignmentOffset(0)
        }}
      />
      <Table
        rowKey="id"
        loading={loading}
        pagination={{
          current:
            Math.floor(groupAssignmentOffset / ASSIGNMENT_PAGE_SIZE) + 1,
          pageSize: ASSIGNMENT_PAGE_SIZE,
          total: groupAssignmentTotal,
          showSizeChanger: false,
          onChange: (page) =>
            setGroupAssignmentOffset((page - 1) * ASSIGNMENT_PAGE_SIZE),
        }}
        dataSource={groupAssignments}
        locale={{ emptyText: t('polarragAccess.noGroups') }}
        columns={[
          {
            title: t('polarragAccess.group'),
            render: (_value, row: AgentGroupAssignment) => groupLabel(row),
          },
          {
            title: t('polarragAccess.type'),
            render: (_value, row: AgentGroupAssignment) => (
              <Tag>
                {row.group_kind === 'department'
                  ? t('polarragAccess.department')
                  : row.group_kind === 'identity_source' ||
                      row.group_kind === 'identity_source_all'
                    ? t('polarragAccess.identitySource')
                    : t('polarragAccess.enterprise')}
              </Tag>
            ),
          },
          { title: t('polarragAccess.members'), dataIndex: 'member_count' },
          {
            title: t('polarragAccess.actions'),
            render: (_value, row: AgentGroupAssignment) => (
              <Button
                danger
                size="small"
                disabled={busy}
                onClick={() =>
                  Modal.confirm({
                    title: t('polarragAccess.removeGroupTitle'),
                    content: (
                      <Space direction="vertical">
                        <Text>{t('polarragAccess.removeGroupWarning')}</Text>
                        <Text>
                          {t('polarragAccess.removeAssignmentWarning')}
                        </Text>
                      </Space>
                    ),
                    okButtonProps: { danger: true },
                    onOk: () =>
                      mutate(() =>
                        deleteAgentGroupAssignment(agentId, row.id),
                      ),
                  })
                }
              >
                {t('polarragAccess.removeGroup')}
              </Button>
            ),
          },
        ]}
      />

      <div>
        <Title level={4} style={{ marginBlock: 0 }}>
          {t('polarragAccess.assignedUsers')}
        </Title>
        <Text type="secondary">
          {t('polarragAccess.usersDescription')}
        </Text>
      </div>
      <Space.Compact style={{ width: '100%' }}>
        <Checkbox
          aria-label={t('polarragAccess.selectCurrentUserPage')}
          checked={
            userOptions.length > 0 &&
            userOptions.every((user) => selectedUsers.includes(user.id))
          }
          disabled={allUsersSelected}
          onChange={(event) => {
            const pageIds = userOptions.map((user) => user.id)
            changeSelectedUsers(
              event.target.checked
                ? [...new Set([...selectedUsers, ...pageIds])]
                : selectedUsers.filter((id) => !pageIds.includes(id)),
            )
          }}
          style={{ alignSelf: 'center', paddingInline: 8 }}
        >
          {t('polarragAccess.selectCurrentUserPage')}
        </Checkbox>
        <Select
          aria-label={t('polarragAccess.user')}
          mode="multiple"
          value={selectedUsers}
          placeholder={t('polarragAccess.selectUser')}
          options={candidateUsers.map((user) => ({
            value: user.id,
            label: `${user.display_name} (${user.external_id})`,
          }))}
          onChange={changeSelectedUsers}
          showSearch
          filterOption={false}
          disabled={allUsersSelected}
          loading={candidateLoading}
          onSearch={(search) => {
            setUserOptionSearch(search)
            setUserOptionOffset(0)
          }}
          popupRender={(menu) => (
            <>
              {menu}
              <Space style={{ display: 'flex', justifyContent: 'center', padding: 8 }}>
                <Button
                  aria-label={t('polarragAccess.previousCandidatePage')}
                  disabled={userOptionOffset === 0}
                  size="small"
                  type="text"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() =>
                    setUserOptionOffset(
                      Math.max(0, userOptionOffset - CANDIDATE_PAGE_SIZE),
                    )
                  }
                >
                  ‹
                </Button>
                <Text type="secondary">
                  {Math.floor(userOptionOffset / CANDIDATE_PAGE_SIZE) + 1} /{' '}
                  {Math.max(1, Math.ceil(userOptionTotal / CANDIDATE_PAGE_SIZE))}
                </Text>
                <Button
                  aria-label={t('polarragAccess.nextCandidatePage')}
                  disabled={userOptionOffset + CANDIDATE_PAGE_SIZE >= userOptionTotal}
                  size="small"
                  type="text"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() =>
                    setUserOptionOffset(userOptionOffset + CANDIDATE_PAGE_SIZE)
                  }
                >
                  ›
                </Button>
              </Space>
            </>
          )}
          style={{ flex: 1 }}
        />
        <Button
          disabled={busy}
          onClick={() => {
            setAllUsersSelected((current) => !current)
            changeSelectedUsers([])
          }}
        >
          {t(
            allUsersSelected
              ? 'polarragAccess.clearAllUsers'
              : 'polarragAccess.selectAllUsers',
          )}
        </Button>
        <Button
          type="primary"
          disabled={(!allUsersSelected && selectedUsers.length === 0) || busy}
          loading={busy}
          onClick={() => {
            void mutate(() =>
              allUsersSelected
                ? createAllAgentUserAssignments(agentId)
                : Promise.all(
                    selectedUsers.map((userId) =>
                      createAgentUserAssignment(agentId, userId),
                    ),
                  ),
            ).then(() => {
              setAllUsersSelected(false)
              changeSelectedUsers([])
            })
          }}
        >
          {t('polarragAccess.assignUsers')}
        </Button>
      </Space.Compact>
      <Input.Search
        allowClear
        aria-label={t('polarragAccess.searchAssignedUsers')}
        placeholder={t('polarragAccess.searchAssignedUsers')}
        value={assignmentSearch}
        onChange={(event) => {
          setAssignmentSearch(event.target.value)
          setAssignmentOffset(0)
        }}
      />
      <Table
        rowKey="id"
        loading={loading}
        pagination={{
          current: Math.floor(assignmentOffset / ASSIGNMENT_PAGE_SIZE) + 1,
          pageSize: ASSIGNMENT_PAGE_SIZE,
          total: assignmentTotal,
          showSizeChanger: false,
          onChange: (page) =>
            setAssignmentOffset((page - 1) * ASSIGNMENT_PAGE_SIZE),
        }}
        dataSource={assignments}
        locale={{ emptyText: t('polarragAccess.noUsers') }}
        columns={[
          { title: t('polarragAccess.user'), dataIndex: 'user_name' },
          {
            title: t('polarragAccess.userStatus'),
            dataIndex: 'user_status',
            render: (value: string) => <Tag>{value}</Tag>,
          },
          {
            title: t('polarragAccess.tokenStatus'),
            render: (_value, row: AgentUserAssignment) =>
              row.token ? <Tag>{row.token.status}</Tag> : t('polarragAccess.notIssued'),
          },
          {
            title: t('polarragAccess.lastUsed'),
            render: (_value, row: AgentUserAssignment) =>
              row.token?.last_used_at
                ? formatDateTime(
                    row.token.last_used_at,
                    i18n.resolvedLanguage ?? i18n.language,
                  )
                : '—',
          },
          {
            title: t('polarragAccess.actions'),
            render: (_value, row: AgentUserAssignment) => (
              <Space>
                {row.token?.status === 'active' && (
                  <Button
                    danger
                    size="small"
                    disabled={busy}
                    onClick={() =>
                      Modal.confirm({
                        title: t('polarragAccess.revokeUserTokenTitle', {
                          name: row.user_name,
                        }),
                        okButtonProps: { danger: true },
                        onOk: () =>
                          mutate(() =>
                            forceRevokeAgentUserToken(agentId, row.id),
                          ),
                      })
                    }
                  >
                    {t('polarragAccess.revokeToken')}
                  </Button>
                )}
                <Button
                  danger
                  size="small"
                  disabled={busy}
                  onClick={() =>
                    Modal.confirm({
                      title: t('polarragAccess.removeUserTitle', {
                        name: row.user_name,
                      }),
                      content: t('polarragAccess.removeAssignmentWarning'),
                      okButtonProps: { danger: true },
                      onOk: () =>
                        mutate(() =>
                          deleteAgentUserAssignment(agentId, row.id),
                        ),
                    })
                  }
                >
                  {t('polarragAccess.removeUser')}
                </Button>
              </Space>
            ),
          },
        ]}
      />
      <EnterpriseAccessDrawer
        agentId={agentId}
        bindings={bindings}
        instances={instances}
        open={enterpriseAccessOpen}
        onClose={() => setEnterpriseAccessOpen(false)}
        onApplied={refreshAfterEnterpriseAccess}
      />
    </Space>
  )
}
