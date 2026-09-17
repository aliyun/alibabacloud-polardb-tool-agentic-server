import { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Drawer,
  Input,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { listResources, type Resource } from '../../api/accessConsole'
import { getAPIErrorMessage } from '../../api/client'
import { useFeatures } from '../../hooks/useFeatures'
import GrantPanel from './GrantPanel'
export default function Resources() {
  const { t } = useTranslation()
  const { knowledge, loaded } = useFeatures()
  const [params, setParams] = useSearchParams()
  const [kind, setKind] = useState(
    params.get('kind') === 'knowledge' ? 'knowledge' : 'database',
  )
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [rows, setRows] = useState<Resource[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const selected = params.get('resource')
  const resource = rows.find((x) => x.id === selected)
  useEffect(() => {
    if (loaded && !knowledge) setKind('database')
  }, [knowledge, loaded])
  useEffect(() => {
    let active = true
    setLoading(true)
    setError('')
    listResources(kind, search, (page - 1) * 20)
      .then(({ data }) => {
        if (active) {
          setRows(data.items)
          setTotal(data.total)
        }
      })
      .catch((e) => {
        if (active) setError(getAPIErrorMessage(e, t('access.failed')))
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [kind, search, page, t])
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Typography.Title level={2}>{t('access.resources')}</Typography.Title>
        <Typography.Text type="secondary">
          {t('access.resourcesHint')}
        </Typography.Text>
      </div>
      <Space wrap>
        <Input.Search
          placeholder={t('access.searchResources')}
          onSearch={(v) => {
            setSearch(v)
            setPage(1)
          }}
          allowClear
        />
        <Link to="/instances">
          <Button type="primary">{t('access.addResource')}</Button>
        </Link>
        <Link to="/settings/features">{t('access.features')}</Link>
      </Space>
      {error && <Alert type="error" message={error} />}
      <Tabs
        activeKey={kind}
        onChange={(k) => {
          setKind(k)
          setPage(1)
          setParams({})
        }}
        items={[
          { key: 'database', label: t('access.database') },
          ...(knowledge
            ? [{ key: 'knowledge', label: t('access.knowledge') }]
            : []),
        ]}
      />
      <Table
        rowKey="id"
        loading={loading}
        dataSource={rows}
        pagination={{ current: page, total, pageSize: 20, onChange: setPage }}
        columns={[
          {
            title: t('access.resource'),
            dataIndex: 'name',
            render: (name, r) => (
              <Button
                type="link"
                onClick={() => setParams({ kind, resource: r.id })}
              >
                {name}
              </Button>
            ),
          },
          {
            title: t('access.type'),
            dataIndex: 'type',
            render: (value) =>
              t(`access.resourceType.${value}`, { defaultValue: value }),
          },
          {
            title: t('access.status'),
            dataIndex: 'status',
            render: (value, r) => (
              <Tag>
                {r.enabled === false
                  ? t('access.disabled')
                  : t(`access.state.${value}`, { defaultValue: value })}
              </Tag>
            ),
          },
          {
            title: t('access.actions'),
            render: (_, r) => (
              <Space>
                <Button
                  type="link"
                  onClick={() => setParams({ kind, resource: r.id })}
                >
                  {t(
                    kind === 'database'
                      ? 'access.manageAccess'
                      : 'access.permissionSource',
                  )}
                </Button>
                <Link
                  to={
                    kind === 'database'
                      ? `/instances/${r.id}`
                      : '/instances?type=polarrag'
                  }
                >
                  {t('access.configuration')}
                </Link>
              </Space>
            ),
          },
        ]}
      />
      <Drawer
        title={resource?.name || t('access.manageAccess')}
        width={880}
        open={!!selected}
        onClose={() => setParams({})}
        destroyOnClose
      >
        {selected &&
          (kind === 'database' ? (
            <>
              <Typography.Paragraph>
                <Link to={`/instances/${selected}`}>
                  {t('access.advancedResource')}
                </Link>
              </Typography.Paragraph>
              <GrantPanel key={selected} resourceId={selected} />
            </>
          ) : (
            <Space direction="vertical">
              <Alert
                type="info"
                showIcon
                message={t('access.knowledgePolicy')}
              />
              <Link to="/users?tab=identity-sources">
                {t('access.identities')}
              </Link>
              <Link to="/instances?type=polarrag">
                {t('access.knowledgeSettings')}
              </Link>
            </Space>
          ))}
      </Drawer>
    </Space>
  )
}
