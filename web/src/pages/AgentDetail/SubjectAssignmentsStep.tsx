import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Input, Modal, Radio, Space, Table, Tabs, Tag, Typography } from 'antd'
import { useTranslation } from 'react-i18next'

import {
  createAgentGroupAssignment,
  createAgentUserAssignment,
  deleteAgentGroupAssignment,
  deleteAgentUserAssignment,
  listAgentGroupOptions,
  listAgentUserOptions,
  getAgentAllSubjectAssignments,
  updateAgentAllSubjectAssignments,
  type AgentAllSubjectAssignments,
  type AgentGroupOption,
  type AgentUserOption,
} from '../../api/agents'
import { getAPIErrorMessage } from '../../api/client'

const { Text, Title } = Typography
const PAGE_SIZE = 20

type SubjectTab = 'identity_source' | 'pas_user' | 'group'
type BindingStatus = 'unbound' | 'bound'
type SubjectRow =
  | { key: string; kind: 'group-option'; value: AgentGroupOption }
  | { key: string; kind: 'user-option'; value: AgentUserOption }

function subjectName(row: SubjectRow): string {
  if (row.kind === 'user-option') return row.value.display_name
  return (
    row.value.department_name ??
    row.value.external_group_name ??
    row.value.identity_source_name ??
    row.value.principal_id ??
    row.key
  )
}

function subjectSource(row: SubjectRow): string {
  if (row.kind === 'user-option') {
    return row.value.identity_source_names?.join(' / ') || 'PAS'
  }
  return row.value.identity_source_name ?? row.value.provider ?? 'PAS'
}

export default function SubjectAssignmentsStep({ agentId }: { agentId: string }) {
  const { t } = useTranslation()
  const [tab, setTab] = useState<SubjectTab>('identity_source')
  const [status, setStatus] = useState<BindingStatus>('unbound')
  const [rows, setRows] = useState<SubjectRow[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [search, setSearch] = useState('')
  const [selectedKeys, setSelectedKeys] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [unbindNotice, setUnbindNotice] = useState(false)
  const [allSubjects, setAllSubjects] = useState<AgentAllSubjectAssignments | null>(null)
  const [refreshVersion, setRefreshVersion] = useState(0)
  const requestVersion = useRef(0)
  const canBindAll = !!allSubjects && allSubjects.status !== 'BOUND'
    && allSubjects.eligible_source_count + allSubjects.eligible_user_count > 0
  const canUnbindAll = !!allSubjects?.assignment_count

  const load = useCallback(async () => {
    const version = ++requestVersion.current
    setLoading(true)
    setRows([])
    setError(null)
    try {
      const summary = await getAgentAllSubjectAssignments(agentId)
      if (version !== requestVersion.current) return
      setAllSubjects(summary.data)
      const params = {
        offset,
        limit: PAGE_SIZE,
        search: search || undefined,
        ...(status === 'bound' ? { binding_status: 'bound' as const } : {}),
      }
      if (tab === 'pas_user') {
        const response = await listAgentUserOptions(agentId, params)
        if (version !== requestVersion.current) return
        setRows(
          response.data.items.map((value) => ({
            key: value.id,
            kind: 'user-option' as const,
            value,
          })),
        )
        setTotal(response.data.total)
      } else {
        const category = tab === 'identity_source' ? 'identity_source' : 'group'
        const response = await listAgentGroupOptions(agentId, {
          ...params,
          category,
        })
        if (version !== requestVersion.current) return
        setRows(
          response.data.items.map((value) => ({
            key:
              value.group_kind === 'identity_source_all'
                ? `identity-source:${value.identity_source_id}`
                : `${value.group_kind}:${value.department_id ?? value.identity_source_id ?? value.identity_domain}:${value.principal_id ?? ''}`,
            kind: 'group-option' as const,
            value,
          })),
        )
        setTotal(response.data.total)
      }
    } catch (requestError) {
      if (version !== requestVersion.current) return
      setAllSubjects(null)
      setError(
        getAPIErrorMessage(requestError, t('agentKnowledgeWorkflow.subjectLoadFailed')),
      )
    } finally {
      if (version === requestVersion.current) setLoading(false)
    }
  }, [agentId, offset, search, status, t, tab])

  useEffect(() => {
    void load()
    return () => { requestVersion.current += 1 }
  }, [load, refreshVersion])

  const applyAll = async (operation: 'BIND' | 'UNBIND') => {
    if (busy || loading || (operation === 'BIND' ? !canBindAll : !canUnbindAll)) return
    setBusy(true)
    setError(null)
    try {
      await updateAgentAllSubjectAssignments(agentId, operation)
      setUnbindNotice(false)
      setSelectedKeys([])
      setRefreshVersion((version) => version + 1)
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('agentKnowledgeWorkflow.subjectUpdateFailed')))
    } finally {
      setBusy(false)
    }
  }

  const selectableRows = useMemo(
    () => rows.filter((row) => status !== 'bound' || row.value.assignment_id),
    [rows, status],
  )
  const selectedRows = useMemo(
    () => selectableRows.filter((row) => selectedKeys.includes(row.key)),
    [selectableRows, selectedKeys],
  )
  const currentPageSelected =
    selectableRows.length > 0 && selectableRows.every((row) => selectedKeys.includes(row.key))

  const apply = async (operation: BindingStatus) => {
    if (busy || loading || selectedRows.length === 0 || operation === status) return
    setBusy(true)
    setError(null)
    try {
      if (operation === 'bound') {
        await Promise.all(
          selectedRows.map((row) => {
            if (row.kind === 'user-option') {
              return createAgentUserAssignment(agentId, row.value.id)
            }
            if (row.kind === 'group-option') {
              return createAgentGroupAssignment(agentId, row.value)
            }
            return Promise.resolve()
          }),
        )
      } else {
        await Promise.all(
          selectedRows.map((row) => {
            if (!row.value.assignment_id) return Promise.resolve()
            if (row.kind === 'user-option') {
              return deleteAgentUserAssignment(agentId, row.value.assignment_id)
            }
            if (row.kind === 'group-option') {
              return deleteAgentGroupAssignment(agentId, row.value.assignment_id)
            }
            return Promise.resolve()
          }),
        )
      }
      setSelectedKeys([])
      setUnbindNotice(operation === 'unbound')
      setRefreshVersion((version) => version + 1)
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, t('agentKnowledgeWorkflow.subjectUpdateFailed')),
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <div>
        <Title level={4} style={{ marginBlock: 0 }}>
          {t('agentKnowledgeWorkflow.configureSubjects')}
        </Title>
        <Text type="secondary">
          {t('agentKnowledgeWorkflow.configureSubjectsDescription')}
        </Text>
      </div>
      <Space wrap>
        <Button
          type="primary"
          disabled={loading || busy || !canBindAll}
          loading={busy}
          onClick={() => void applyAll('BIND')}
        >
          {t('agentKnowledgeWorkflow.bindAllUsers')}
        </Button>
        <Button
          danger
          disabled={loading || busy || !canUnbindAll}
          onClick={() => Modal.confirm({
            title: t('agentKnowledgeWorkflow.unbindAllUsers'),
            content: t('agentKnowledgeWorkflow.unbindAllAgentUsersConfirm'),
            okText: t('agentKnowledgeWorkflow.unbindAllUsers'),
            okButtonProps: { danger: true },
            onOk: () => applyAll('UNBIND'),
          })}
        >
          {t('agentKnowledgeWorkflow.unbindAllUsers')}
        </Button>
      </Space>
      <Text type="secondary">{t('agentKnowledgeWorkflow.allAgentUsersHint')}</Text>
      {error && <Alert type="error" showIcon message={error} />}
      {unbindNotice && <Alert type="info" showIcon message={t('agentKnowledgeWorkflow.unbindInheritedWarning')} />}
      {status === 'bound' && rows.some((row) => row.value.inherited_source_names?.length) && (
        <Alert type="info" showIcon message={t('agentKnowledgeWorkflow.inheritedBindingHelp')} />
      )}
      <Tabs
        activeKey={tab}
        onChange={(key) => {
          setTab(key as SubjectTab)
          setOffset(0)
          setSelectedKeys([])
        }}
        items={[
          { key: 'identity_source', label: t('agentKnowledgeWorkflow.enterpriseSources') },
          { key: 'pas_user', label: t('agentKnowledgeWorkflow.users') },
          { key: 'group', label: t('agentKnowledgeWorkflow.departmentsAndGroups') },
        ]}
      />
      <Space wrap style={{ justifyContent: 'space-between', width: '100%' }}>
        <Input.Search
          allowClear
          placeholder={t('agentKnowledgeWorkflow.searchSubjects')}
          value={search}
          onChange={(event) => {
            setSearch(event.target.value)
            setOffset(0)
          }}
          style={{ width: 360 }}
        />
        <Radio.Group
          optionType="button"
          value={status}
          onChange={(event) => {
            setStatus(event.target.value)
            setOffset(0)
            setSelectedKeys([])
          }}
          options={[
            { value: 'unbound', label: t('agentKnowledgeWorkflow.unboundOnly') },
            { value: 'bound', label: t('agentKnowledgeWorkflow.boundOnly') },
          ]}
        />
      </Space>
      <Table
        rowKey="key"
        loading={loading}
        dataSource={rows}
        rowSelection={{
          selectedRowKeys: selectedKeys,
          onChange: (keys) => setSelectedKeys(keys.map(String)),
          getCheckboxProps: (row) => ({ disabled: busy || loading || !selectableRows.includes(row) }),
        }}
        pagination={{
          current: Math.floor(offset / PAGE_SIZE) + 1,
          pageSize: PAGE_SIZE,
          total,
          showSizeChanger: false,
          onChange: (page) => {
            setOffset((page - 1) * PAGE_SIZE)
            setSelectedKeys([])
          },
        }}
        columns={[
          {
            title: t('agentKnowledgeWorkflow.subject'),
            render: (_value, row: SubjectRow) => (
              <Space wrap>
                {subjectName(row)}
                {row.kind === 'user-option' && row.value.availability && row.value.availability !== 'active' && (
                  <Tag>{t(`agentKnowledgeWorkflow.userAvailability.${row.value.availability}`)}</Tag>
                )}
              </Space>
            ),
          },
          {
            title: t('agentKnowledgeWorkflow.subjectType'),
            render: () => t(`agentKnowledgeWorkflow.subjectTypes.${tab}`),
          },
          {
            title: t('agentKnowledgeWorkflow.memberCount'),
            render: (_value, row: SubjectRow) =>
              row.kind === 'group-option'
                ? row.value.member_count
                : '—',
          },
          {
            title: t('agentKnowledgeWorkflow.source'),
            render: (_value, row: SubjectRow) => subjectSource(row),
          },
          {
            title: t('agentKnowledgeWorkflow.bindingStatus'),
            render: (_value, row: SubjectRow) => (
              <Tag color={status === 'bound' ? 'green' : undefined}>
                {row.value.inherited_source_names?.length
                  ? t('agentKnowledgeWorkflow.inheritedFromSource', {
                      sources: row.value.inherited_source_names.join(' / '),
                    })
                  : t(status === 'bound' ? 'agentKnowledgeWorkflow.boundOnly' : 'agentKnowledgeWorkflow.unboundOnly')}
              </Tag>
            ),
          },
        ]}
      />
      <Space style={{ justifyContent: 'space-between', width: '100%' }}>
        <Button
          aria-label={t(
            currentPageSelected
              ? 'agentKnowledgeWorkflow.clearCurrentPage'
              : 'agentKnowledgeWorkflow.selectCurrentPage',
          )}
          onClick={() =>
            setSelectedKeys(currentPageSelected ? [] : selectableRows.map((row) => row.key))
          }
          disabled={busy || loading || selectableRows.length === 0}
        >
          {t(
            currentPageSelected
              ? 'agentKnowledgeWorkflow.clearCurrentPage'
              : 'agentKnowledgeWorkflow.selectCurrentPage',
          )}
        </Button>
        <Space>
          <Button
            type={status === 'unbound' ? 'primary' : 'default'}
            danger={status === 'bound'}
            loading={busy}
            disabled={busy || loading || selectedRows.length === 0}
            onClick={() => {
              if (status === 'unbound') {
                void apply('bound')
              } else {
                Modal.confirm({
                  title: t('agentKnowledgeWorkflow.unbindSelected'),
                  content: t('agentKnowledgeWorkflow.unbindInheritedWarning'),
                  okText: t('agentKnowledgeWorkflow.unbindSelected'),
                  okButtonProps: { danger: true },
                  onOk: () => apply('unbound'),
                })
              }
            }}
          >
            {t(status === 'unbound' ? 'agentKnowledgeWorkflow.bindSelected' : 'agentKnowledgeWorkflow.unbindSelected')}
          </Button>
        </Space>
      </Space>
    </Space>
  )
}
