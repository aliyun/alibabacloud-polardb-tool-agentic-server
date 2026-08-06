import { useCallback, useEffect, useState } from 'react'
import { Table, Tag, Drawer, Descriptions, Typography } from 'antd'
import { Highlight, themes } from 'prism-react-renderer'
import { useTranslation } from 'react-i18next'
import api from '../../api/client'
import PageContainer from '../../components/PageContainer'
import { formatDateTime } from '../../i18n/format'

interface AuditLogItem {
  id: string
  user_id: string
  instance_id: string | null
  action: string
  sql_text: string | null
  sql_type: string | null
  status: string
  error_message: string | null
  duration_ms: number | null
  row_count: number | null
  client_info: string | null
  user_name: string | null
  instance_name: string | null
  db_name: string | null
  created_at: string
}

function sqlTypeColor(t: string | null): string {
  if (!t) return 'default'
  const upper = t.toUpperCase()
  if (['SELECT', 'SHOW', 'DESCRIBE', 'EXPLAIN', 'USE'].includes(upper)) return 'blue'
  if (['INSERT', 'UPDATE', 'DELETE'].includes(upper)) return 'orange'
  if (['CREATE', 'ALTER', 'DROP', 'TRUNCATE'].includes(upper)) return 'red'
  return 'default'
}

function SqlHighlight({ code }: { code: string }) {
  return (
    <Highlight theme={themes.vsLight} code={code} language="sql">
      {({ style, tokens, getLineProps, getTokenProps }) => (
        <pre style={{ ...style, padding: 16, borderRadius: 8, overflow: 'auto', fontSize: 13 }}>
          {tokens.map((line, i) => (
            <div key={i} {...getLineProps({ line })}>
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

export default function AuditLogs() {
  const { t, i18n } = useTranslation()
  const [logs, setLogs] = useState<AuditLogItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<AuditLogItem | null>(null)

  const fetchLogs = useCallback(async () => {
    setLoading(true)
    try {
      const resp = await api.get('/api/audit-logs', { params: { offset: (page - 1) * 50, limit: 50 } })
      setLogs(resp.data.items)
      setTotal(resp.data.total)
    } finally {
      setLoading(false)
    }
  }, [page])

  useEffect(() => { void fetchLogs() }, [fetchLogs])

  const columns = [
    { title: t('auditLogs.user'), dataIndex: 'user_name', key: 'user', render: (v: string | null) => v || '-' },
    { title: t('auditLogs.sqlType'), dataIndex: 'sql_type', key: 'sql_type', render: (value: string | null) => value ? <Tag color={sqlTypeColor(value)}>{value}</Tag> : '-' },
    { title: t('auditLogs.sql'), dataIndex: 'sql_text', key: 'sql', width: 300, ellipsis: true, render: (s: string | null) => s ? <Typography.Text code style={{ fontSize: 12 }}>{s.length > 80 ? s.slice(0, 80) + '...' : s}</Typography.Text> : '-' },
    { title: t('auditLogs.instance'), dataIndex: 'instance_name', key: 'instance', render: (v: string | null) => v || '-' },
    { title: t('auditLogs.database'), dataIndex: 'db_name', key: 'db', render: (v: string | null) => v || '-' },
    { title: t('auditLogs.status'), dataIndex: 'status', key: 'status', render: (s: string) => <Tag color={s === 'success' ? 'green' : s === 'blocked' ? 'red' : 'orange'}>{s}</Tag> },
    { title: t('auditLogs.duration'), dataIndex: 'duration_ms', key: 'duration', render: (ms: number | null) => ms !== null ? `${ms}ms` : '-' },
    { title: t('auditLogs.rows'), dataIndex: 'row_count', key: 'rows', render: (r: number | null) => r !== null ? r : '-' },
    { title: t('auditLogs.time'), dataIndex: 'created_at', key: 'time', render: (value: string) => value ? formatDateTime(value, i18n.resolvedLanguage ?? i18n.language) : '-' },
  ]

  return (
    <PageContainer title={t('auditLogs.title')} description={t('auditLogs.description')}>
      <Table
        dataSource={logs}
        columns={columns}
        rowKey="id"
        loading={loading}
        pagination={{ total, pageSize: 50, current: page, onChange: setPage, showTotal: (count) => t('auditLogs.records', { count }) }}
        onRow={(record) => ({ onClick: () => setSelected(record), style: { cursor: 'pointer' } })}
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
              <Descriptions.Item label={t('auditLogs.user')}>{selected.user_name || selected.user_id}</Descriptions.Item>
              <Descriptions.Item label={t('auditLogs.instance')}>{selected.instance_name || selected.instance_id || '-'}</Descriptions.Item>
              <Descriptions.Item label={t('auditLogs.database')}>{selected.db_name || '-'}</Descriptions.Item>
              <Descriptions.Item label={t('auditLogs.sqlType')}>{selected.sql_type || '-'}</Descriptions.Item>
              <Descriptions.Item label={t('auditLogs.action')}>{selected.action}</Descriptions.Item>
              <Descriptions.Item label={t('auditLogs.status')}><Tag color={selected.status === 'success' ? 'green' : selected.status === 'blocked' ? 'red' : 'orange'}>{selected.status}</Tag></Descriptions.Item>
              <Descriptions.Item label={t('auditLogs.duration')}>{selected.duration_ms !== null ? `${selected.duration_ms}ms` : '-'}</Descriptions.Item>
              <Descriptions.Item label={t('auditLogs.rows')}>{selected.row_count !== null ? selected.row_count : '-'}</Descriptions.Item>
              {selected.error_message && <Descriptions.Item label={t('auditLogs.error')}>{selected.error_message}</Descriptions.Item>}
              {selected.client_info && <Descriptions.Item label={t('auditLogs.clientInfo')}>{selected.client_info}</Descriptions.Item>}
              <Descriptions.Item label={t('auditLogs.time')}>{formatDateTime(selected.created_at, i18n.resolvedLanguage ?? i18n.language)}</Descriptions.Item>
            </Descriptions>
          </>
        )}
      </Drawer>
    </PageContainer>
  )
}
