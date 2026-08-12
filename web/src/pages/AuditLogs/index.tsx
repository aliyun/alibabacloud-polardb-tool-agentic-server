import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Descriptions,
  Drawer,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  type TableColumnsType,
} from 'antd'
import { Highlight, themes } from 'prism-react-renderer'
import { useTranslation } from 'react-i18next'
import api from '../../api/client'
import PageContainer from '../../components/PageContainer'

type AuditCategory = 'all' | 'sql' | 'polarrag'

interface PolarRAGAuditContext {
  instance_ids: string[]
  instance_names: string[]
  space_ids: string[]
  space_names: string[]
  kb_ids: string[]
  kb_names: string[]
  knowledge_resource_ids: string[]
  knowledge_resource_names: string[]
  hit_count: number | null
  successful_searches: number | null
  failed_searches: number | null
  partial_failure_count: number
  polarrag_status: string | null
}

interface AuditLogItem {
  id: string
  user_id: string | null
  agent_id: string | null
  category: 'sql' | 'polarrag' | 'other'
  instance_id: string | null
  action: string
  sql_text: string | null
  sql_type: string | null
  status: string
  error_message: string | null
  error_code: string | null
  duration_ms: number | null
  row_count: number | null
  client_info: string | null
  user_name: string | null
  agent_name: string | null
  instance_name: string | null
  db_name: string | null
  target_type: string | null
  target_id: string | null
  request_id: string | null
  polarrag: PolarRAGAuditContext | null
  created_at: string
}

function sqlTypeColor(type: string | null): string {
  if (!type) return 'default'
  const upper = type.toUpperCase()
  if (['SELECT', 'SHOW', 'DESCRIBE', 'EXPLAIN', 'USE'].includes(upper)) {
    return 'blue'
  }
  if (['INSERT', 'UPDATE', 'DELETE'].includes(upper)) return 'orange'
  if (['CREATE', 'ALTER', 'DROP', 'TRUNCATE'].includes(upper)) return 'red'
  return 'default'
}

function statusColor(status: string): string {
  if (status === 'success') return 'green'
  if (status === 'blocked') return 'red'
  return 'orange'
}

function actorName(item: AuditLogItem): string {
  return item.user_name || item.agent_name || item.user_id || item.agent_id || '—'
}

function displayNames(names: string[] | undefined): React.ReactNode {
  if (!names?.length) return '—'
  return (
    <Space size={[4, 4]} wrap>
      {names.map((name) => (
        <Tag key={name}>{name}</Tag>
      ))}
    </Space>
  )
}

function SqlHighlight({ code }: { code: string }) {
  return (
    <Highlight theme={themes.vsLight} code={code} language="sql">
      {({ style, tokens, getLineProps, getTokenProps }) => (
        <pre
          style={{
            ...style,
            padding: 16,
            borderRadius: 8,
            overflow: 'auto',
            fontSize: 13,
          }}
        >
          {tokens.map((line, index) => (
            <div key={index} {...getLineProps({ line })}>
              {line.map((token, key) => (
                <span key={key} {...getTokenProps({ token })} />
              ))}
            </div>
          ))}
        </pre>
      )}
    </Highlight>
  )
}

const commonColumns: TableColumnsType<AuditLogItem> = [
  {
    title: 'Actor',
    key: 'actor',
    render: (_, item) => actorName(item),
  },
  { title: 'Action', dataIndex: 'action', key: 'action' },
  {
    title: 'Status',
    dataIndex: 'status',
    key: 'status',
    render: (status: string) => (
      <Tag color={statusColor(status)}>{status}</Tag>
    ),
  },
  {
    title: 'Duration',
    dataIndex: 'duration_ms',
    key: 'duration',
    render: (duration: number | null) =>
      duration === null ? '—' : `${duration}ms`,
  },
  {
    title: 'Time',
    dataIndex: 'created_at',
    key: 'time',
    render: (time: string) => (time ? new Date(time).toLocaleString() : '—'),
  },
]

const sqlColumns: TableColumnsType<AuditLogItem> = [
  commonColumns[0],
  commonColumns[1],
  {
    title: 'SQL type',
    dataIndex: 'sql_type',
    key: 'sql_type',
    render: (type: string | null) =>
      type ? <Tag color={sqlTypeColor(type)}>{type}</Tag> : '—',
  },
  {
    title: 'SQL',
    dataIndex: 'sql_text',
    key: 'sql',
    width: 280,
    ellipsis: true,
    render: (sql: string | null) =>
      sql ? (
        <Typography.Text code style={{ fontSize: 12 }}>
          {sql.length > 80 ? `${sql.slice(0, 80)}…` : sql}
        </Typography.Text>
      ) : (
        '—'
      ),
  },
  {
    title: 'Instance',
    dataIndex: 'instance_name',
    key: 'instance',
    render: (value: string | null) => value || '—',
  },
  {
    title: 'Database',
    dataIndex: 'db_name',
    key: 'database',
    render: (value: string | null) => value || '—',
  },
  commonColumns[2],
  commonColumns[3],
  {
    title: 'Rows',
    dataIndex: 'row_count',
    key: 'rows',
    render: (rows: number | null) => (rows === null ? '—' : rows),
  },
  commonColumns[4],
]

const polarRAGColumns: TableColumnsType<AuditLogItem> = [
  commonColumns[0],
  commonColumns[1],
  {
    title: 'PolarRAG instance',
    key: 'polarrag_instance',
    render: (_, item) => displayNames(item.polarrag?.instance_names),
  },
  {
    title: 'Space',
    key: 'space',
    render: (_, item) => displayNames(item.polarrag?.space_names),
  },
  {
    title: 'KB',
    key: 'kb',
    render: (_, item) => displayNames(item.polarrag?.kb_names),
  },
  {
    title: 'Hits',
    key: 'hits',
    render: (_, item) => item.polarrag?.hit_count ?? '—',
  },
  {
    title: 'Partial failures',
    key: 'partial_failures',
    render: (_, item) => item.polarrag?.partial_failure_count ?? 0,
  },
  commonColumns[2],
  commonColumns[3],
  commonColumns[4],
]

const allColumns: TableColumnsType<AuditLogItem> = [
  {
    title: 'Type',
    dataIndex: 'category',
    key: 'category',
    render: (category: AuditLogItem['category']) => (
      <Tag color={category === 'polarrag' ? 'cyan' : category === 'sql' ? 'blue' : 'default'}>
        {category === 'polarrag' ? 'PolarRAG' : category.toUpperCase()}
      </Tag>
    ),
  },
  ...commonColumns,
]

export default function AuditLogs() {
  const { t } = useTranslation()
  const [logs, setLogs] = useState<AuditLogItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [page, setPage] = useState(1)
  const [category, setCategory] = useState<AuditCategory>('all')
  const [selected, setSelected] = useState<AuditLogItem | null>(null)

  const fetchLogs = useCallback(async () => {
    setLoading(true)
    try {
      const params: Record<string, string | number> = {
        offset: (page - 1) * 50,
        limit: 50,
      }
      if (category !== 'all') params.category = category
      const response = await api.get('/api/audit-logs', { params })
      setLogs(response.data.items)
      setTotal(response.data.total)
    } finally {
      setLoading(false)
    }
  }, [category, page])

  useEffect(() => {
    void fetchLogs()
  }, [fetchLogs])

  const columns = useMemo(() => {
    if (category === 'sql') return sqlColumns
    if (category === 'polarrag') return polarRAGColumns
    return allColumns
  }, [category])

  return (
    <PageContainer
      title={t('auditLogs.title')}
      description={t('auditLogs.combinedDescription')}
    >
      <Tabs
        activeKey={category}
        items={[
          { key: 'all', label: 'All' },
          { key: 'sql', label: 'SQL' },
          { key: 'polarrag', label: 'PolarRAG' },
        ]}
        onChange={(key) => {
          setCategory(key as AuditCategory)
          setPage(1)
          setSelected(null)
        }}
      />
      <Table
        dataSource={logs}
        columns={columns}
        rowKey="id"
        loading={loading}
        scroll={{ x: category === 'polarrag' ? 1180 : 980 }}
        pagination={{
          total,
          pageSize: 50,
          current: page,
          onChange: setPage,
          showTotal: (count) => `${count} records`,
        }}
        onRow={(record) => ({
          onClick: () => setSelected(record),
          style: { cursor: 'pointer' },
        })}
      />
      <Drawer
        title={t('auditLogs.detail')}
        placement="right"
        width={640}
        open={!!selected}
        onClose={() => setSelected(null)}
      >
        {selected && (
          <>
            {selected.sql_text && (
              <div style={{ marginBottom: 24 }}>
                <Typography.Title level={5}>{t('auditLogs.sql')}</Typography.Title>
                <SqlHighlight code={selected.sql_text} />
              </div>
            )}
            <Descriptions column={1} bordered size="small">
              <Descriptions.Item label={t('auditLogs.actor')}>
                {actorName(selected)}
              </Descriptions.Item>
              <Descriptions.Item label={t('auditLogs.action')}>
                {selected.action}
              </Descriptions.Item>
              {selected.category === 'sql' && (
                <>
                  <Descriptions.Item label={t('auditLogs.instance')}>
                    {selected.instance_name || selected.instance_id || '—'}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('auditLogs.database')}>
                    {selected.db_name || '—'}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('auditLogs.sqlType')}>
                    {selected.sql_type || '—'}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('auditLogs.rows')}>
                    {selected.row_count ?? '—'}
                  </Descriptions.Item>
                </>
              )}
              {selected.polarrag && (
                <>
                  <Descriptions.Item label={t('auditLogs.polarragInstance')}>
                    {displayNames(selected.polarrag.instance_names)}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('auditLogs.space')}>
                    {displayNames(selected.polarrag.space_names)}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('auditLogs.kb')}>
                    {displayNames(selected.polarrag.kb_names)}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('auditLogs.knowledgeResource')}>
                    {displayNames(selected.polarrag.knowledge_resource_names)}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('auditLogs.hits')}>
                    {selected.polarrag.hit_count ?? '—'}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('auditLogs.successfulSearches')}>
                    {selected.polarrag.successful_searches ?? '—'}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('auditLogs.failedSearches')}>
                    {selected.polarrag.failed_searches ?? '—'}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('auditLogs.partialFailures')}>
                    {selected.polarrag.partial_failure_count}
                  </Descriptions.Item>
                </>
              )}
              <Descriptions.Item label={t('auditLogs.status')}>
                <Tag color={statusColor(selected.status)}>{selected.status}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t('auditLogs.duration')}>
                {selected.duration_ms === null ? '—' : `${selected.duration_ms}ms`}
              </Descriptions.Item>
              {selected.error_code && (
                <Descriptions.Item label={t('auditLogs.errorCode')}>
                  {selected.error_code}
                </Descriptions.Item>
              )}
              {selected.error_message && (
                <Descriptions.Item label={t('auditLogs.error')}>
                  {selected.error_message}
                </Descriptions.Item>
              )}
              {selected.request_id && (
                <Descriptions.Item label={t('auditLogs.requestId')}>
                  {selected.request_id}
                </Descriptions.Item>
              )}
              <Descriptions.Item label={t('auditLogs.time')}>
                {new Date(selected.created_at).toLocaleString()}
              </Descriptions.Item>
            </Descriptions>
          </>
        )}
      </Drawer>
    </PageContainer>
  )
}
