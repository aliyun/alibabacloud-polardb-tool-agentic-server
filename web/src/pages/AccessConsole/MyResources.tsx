import { useEffect, useState } from 'react'
import { Alert, Button, Empty, Space, Table, Tag, Typography } from 'antd'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import {
  personalResources,
  type PersonalPage,
  type PersonalResource,
} from '../../api/accessConsole'
import { getAPIErrorMessage } from '../../api/client'
import { useFeatures } from '../../hooks/useFeatures'
export default function MyResources({
  compact = false,
}: {
  compact?: boolean
}) {
  const { t } = useTranslation()
  const { knowledge } = useFeatures()
  const [data, setData] = useState<PersonalPage>()
  const [cursor, setCursor] = useState<string>()
  const [history, setHistory] = useState<(string | undefined)[]>([])
  const [offset, setOffset] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)
  useEffect(() => {
    let active = true
    setBusy(true)
    setError('')
    personalResources(cursor, offset)
      .then(({ data }) => {
        if (active) setData(data)
      })
      .catch((e) => {
        if (active) setError(getAPIErrorMessage(e, t('access.failed')))
      })
      .finally(() => {
        if (active) setBusy(false)
      })
    return () => {
      active = false
    }
  }, [cursor, offset, refresh, t, knowledge])
  const columns = [
    { title: t('access.resource'), dataIndex: 'name' },
    {
      title: t('access.status'),
      dataIndex: 'status',
      render: (value: string) =>
        t(`access.state.${value?.toLowerCase()}`, {
          defaultValue: value || '',
        }),
    },
    {
      title: t('access.permission'),
      render: (_: unknown, r: PersonalResource) =>
        r.permission ? t(`access.${r.permission}`) : t('access.metadataOnly'),
    },
    {
      title: t('access.source'),
      render: (_: unknown, r: PersonalResource) =>
        t(`access.${r.access_source || 'admin'}`),
    },
  ]
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Typography.Title level={compact ? 4 : 2}>
          {t('access.myResources')}
        </Typography.Title>
        <Typography.Text type="secondary">
          {t('access.myResourcesHint')}
        </Typography.Text>
      </div>
      {!compact && (
        <Space>
          <Link to="/connect">
            <Button type="primary">{t('access.connect')}</Button>
          </Link>
          <Button onClick={() => setRefresh((v) => v + 1)}>
            {t('access.refresh')}
          </Button>
        </Space>
      )}
      {error && <Alert type="error" message={error} />}
      <Table
        rowKey="db_instance_id"
        loading={busy}
        dataSource={data?.database_instances || []}
        pagination={false}
        columns={columns}
        locale={{ emptyText: <Empty description={t('access.noResources')} /> }}
      />
      <Space>
        <Button
          disabled={!history.length}
          onClick={() => {
            setCursor(history[history.length - 1])
            setHistory(history.slice(0, -1))
          }}
        >
          {t('access.previous')}
        </Button>
        <Button
          disabled={!data?.database_has_more}
          onClick={() => {
            setHistory([...history, cursor])
            setCursor(data?.database_next_cursor || undefined)
          }}
        >
          {t('access.next')}
        </Button>
      </Space>
      {knowledge && (
        <>
          <Typography.Title level={4}>{t('access.knowledge')}</Typography.Title>
          <Alert type="info" message={t('access.knowledgePolicy')} />
          <Table
            rowKey="knowledge_resource_id"
            loading={busy}
            dataSource={data?.knowledge_resources || []}
            pagination={{
              current: offset / 20 + 1,
              pageSize: 20,
              total: data?.knowledge_resource_total || 0,
              onChange: (page) => setOffset((page - 1) * 20),
            }}
            columns={[
              { title: t('access.resource'), dataIndex: 'name' },
              { title: t('access.space'), dataIndex: 'knowledge_space_name' },
              {
                title: t('access.permission'),
                render: () => <Tag>{t('access.knowledgeRead')}</Tag>,
              },
            ]}
          />
        </>
      )}
    </Space>
  )
}
