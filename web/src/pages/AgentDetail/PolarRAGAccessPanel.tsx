import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Button, Modal, Select, Space, Table, Tag, Typography } from 'antd'
import { useTranslation } from 'react-i18next'

import {
  createAgentGroupAssignment,
  createAgentPolarRAGBinding,
  createAgentUserAssignment,
  deleteAgentGroupAssignment,
  deleteAgentUserAssignment,
  forceRevokeAgentUserToken,
  listAgentGroupAssignments,
  listAgentGroupOptions,
  listAgentUserAssignments,
  type AgentGroupAssignment,
  type AgentGroupOption,
  type AgentPolarRAGBinding,
  type AgentUserAssignment,
} from '../../api/agents'
import api, { getAPIErrorMessage } from '../../api/client'
import {
  listPolarRAGInstances,
  type PolarRAGInstance,
} from '../../api/polarrag'

const { Text, Title } = Typography

interface UserOption {
  id: string
  display_name: string
  external_id: string
  status: string
}

async function listAllUsers(): Promise<UserOption[]> {
  const users: UserOption[] = []
  let offset = 0
  while (true) {
    const response = await api.get<{
      items: UserOption[]
      total: number
    }>('/api/users', { params: { offset, limit: 100 } })
    users.push(...response.data.items)
    offset += response.data.items.length
    if (offset >= response.data.total || response.data.items.length === 0) {
      return users
    }
  }
}

function groupKey(group: AgentGroupOption): string {
  return group.group_kind === 'department'
    ? `department:${group.department_id}`
    : `enterprise:${group.identity_domain}:${group.provider}:${group.principal_id}`
}

function groupLabel(group: AgentGroupOption): string {
  const members = `${group.member_count} member${group.member_count === 1 ? '' : 's'}`
  return group.group_kind === 'department'
    ? `Department · ${group.department_name} (${members})`
    : `Enterprise · ${group.provider} · ${group.principal_id} · ${group.identity_domain} (${members})`
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
  const { t } = useTranslation()
  const [instances, setInstances] = useState<PolarRAGInstance[]>([])
  const [users, setUsers] = useState<UserOption[]>([])
  const [assignments, setAssignments] = useState<AgentUserAssignment[]>([])
  const [groupOptions, setGroupOptions] = useState<AgentGroupOption[]>([])
  const [groupAssignments, setGroupAssignments] = useState<
    AgentGroupAssignment[]
  >([])
  const [selectedInstance, setSelectedInstance] = useState<string>()
  const [selectedUser, setSelectedUser] = useState<string>()
  const [selectedGroup, setSelectedGroup] = useState<string>()
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [
        instanceResponse,
        userResponse,
        assignmentResponse,
        groupOptionResponse,
        groupAssignmentResponse,
      ] =
        await Promise.all([
          listPolarRAGInstances(),
          listAllUsers(),
          listAgentUserAssignments(agentId),
          listAgentGroupOptions(agentId),
          listAgentGroupAssignments(agentId),
        ])
      setInstances(instanceResponse.data.items)
      setUsers(userResponse)
      setAssignments(assignmentResponse.data)
      setGroupOptions(groupOptionResponse.data)
      setGroupAssignments(groupAssignmentResponse.data)
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, 'Could not load PolarRAG access.'),
      )
    } finally {
      setLoading(false)
    }
  }, [agentId])

  useEffect(() => {
    void load()
  }, [load])

  const availableInstances = useMemo(() => {
    const bound = new Set(bindings.map((row) => row.polarrag_instance_id))
    return instances.filter((instance) => !bound.has(instance.id))
  }, [bindings, instances])
  const availableUsers = useMemo(() => {
    const assigned = new Set(assignments.map((row) => row.user_id))
    return users.filter((user) => !assigned.has(user.id))
  }, [assignments, users])
  const availableGroups = useMemo(() => {
    const assigned = new Set(groupAssignments.map(groupKey))
    return groupOptions.filter((group) => !assigned.has(groupKey(group)))
  }, [groupAssignments, groupOptions])

  const mutate = async (operation: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await operation()
      await load()
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, 'Could not update PolarRAG access.'),
      )
    } finally {
      setBusy(false)
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
          {t('polarragAccess.description')}
        </Text>
      </div>
      <Space.Compact style={{ width: '100%' }}>
        <Select
          aria-label={t('polarragAccess.instance')}
          value={selectedInstance}
          onChange={setSelectedInstance}
          placeholder={t('polarragAccess.selectInstance')}
          notFoundContent={
            instances.length > 0 && availableInstances.length === 0
              ? 'All PolarRAG instances are already bound'
              : 'No PolarRAG instances available'
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
      <div>
        <Title level={4} style={{ marginBlock: 0 }}>
          {t('polarragAccess.assignedGroups')}
        </Title>
        <Text type="secondary">
          {t('polarragAccess.groupsDescription')}
        </Text>
      </div>
      <Space.Compact style={{ width: '100%' }}>
        <Select
          aria-label={t('polarragAccess.group')}
          value={selectedGroup}
          onChange={setSelectedGroup}
          placeholder={t('polarragAccess.selectGroup')}
          showSearch
          optionFilterProp="label"
          options={availableGroups.map((group) => ({
            value: groupKey(group),
            label: groupLabel(group),
          }))}
          style={{ flex: 1 }}
        />
        <Button
          type="primary"
          disabled={!selectedGroup}
          loading={busy}
          onClick={() => {
            const group = availableGroups.find(
              (option) => groupKey(option) === selectedGroup,
            )
            if (!group) return
            void mutate(() => createAgentGroupAssignment(agentId, group)).then(
              () => setSelectedGroup(undefined),
            )
          }}
        >
          {t('polarragAccess.assignGroup')}
        </Button>
      </Space.Compact>
      <Table
        rowKey="id"
        loading={loading}
        pagination={false}
        dataSource={groupAssignments}
        locale={{ emptyText: 'No groups assigned' }}
        columns={[
          {
            title: 'Group',
            render: (_value, row: AgentGroupAssignment) => groupLabel(row),
          },
          {
            title: 'Type',
            render: (_value, row: AgentGroupAssignment) => (
              <Tag>{row.group_kind === 'department' ? 'Department' : 'Enterprise'}</Tag>
            ),
          },
          { title: 'Members', dataIndex: 'member_count' },
          {
            title: 'Actions',
            render: (_value, row: AgentGroupAssignment) => (
              <Button
                danger
                size="small"
                disabled={busy}
                onClick={() =>
                  Modal.confirm({
                    title: 'Remove group assignment?',
                    content:
                      'Users without another direct or group assignment lose Agent access immediately.',
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
        <Select
          aria-label={t('polarragAccess.user')}
          value={selectedUser}
          onChange={setSelectedUser}
          placeholder={t('polarragAccess.selectUser')}
          showSearch
          optionFilterProp="label"
          options={availableUsers.map((user) => ({
            value: user.id,
            label: `${user.display_name} (${user.external_id})`,
          }))}
          style={{ flex: 1 }}
        />
        <Button
          type="primary"
          disabled={!selectedUser}
          loading={busy}
          onClick={() => {
            if (!selectedUser) return
            void mutate(() => createAgentUserAssignment(agentId, selectedUser)).then(
              () => setSelectedUser(undefined),
            )
          }}
        >
          {t('polarragAccess.assignUser')}
        </Button>
      </Space.Compact>
      <Table
        rowKey="id"
        loading={loading}
        pagination={false}
        dataSource={assignments}
        locale={{ emptyText: 'No users assigned' }}
        columns={[
          { title: 'User', dataIndex: 'user_name' },
          {
            title: 'User status',
            dataIndex: 'user_status',
            render: (value: string) => <Tag>{value}</Tag>,
          },
          {
            title: 'Token status',
            render: (_value, row: AgentUserAssignment) =>
              row.token ? <Tag>{row.token.status}</Tag> : 'Not issued',
          },
          {
            title: 'Last used',
            render: (_value, row: AgentUserAssignment) =>
              row.token?.last_used_at
                ? new Date(row.token.last_used_at).toLocaleString()
                : '—',
          },
          {
            title: 'Actions',
            render: (_value, row: AgentUserAssignment) => (
              <Space>
                {row.token?.status === 'active' && (
                  <Button
                    danger
                    size="small"
                    disabled={busy}
                    onClick={() =>
                      Modal.confirm({
                        title: `Revoke ${row.user_name}'s Token?`,
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
                      title: `Remove ${row.user_name} from this Agent?`,
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
    </Space>
  )
}
