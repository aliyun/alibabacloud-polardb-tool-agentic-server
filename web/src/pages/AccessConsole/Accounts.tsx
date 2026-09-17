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
import {
  listAccounts,
  type Account,
  type AccountKind,
} from '../../api/accessConsole'
import { getAPIErrorMessage } from '../../api/client'
import GrantPanel from './GrantPanel'
export default function Accounts() {
  const { t } = useTranslation()
  const [params, setParams] = useSearchParams()
  const kind: AccountKind =
    params.get('kind') === 'service' ? 'service' : 'personal'
  const selected = params.get('account')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [rows, setRows] = useState<Account[]>([])
  const [total, setTotal] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    setBusy(true)
    setError('')
    listAccounts(kind, search, (page - 1) * 20)
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
        if (active) setBusy(false)
      })
    return () => {
      active = false
    }
  }, [kind, search, page, t])
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Typography.Title level={2}>{t('access.accounts')}</Typography.Title>
        <Typography.Text type="secondary">
          {t('access.accountsHint')}
        </Typography.Text>
      </div>
      <Tabs
        activeKey={kind}
        onChange={(k) => {
          setParams({ kind: k })
          setPage(1)
          setSearch('')
        }}
        items={['personal', 'service'].map((key) => ({
          key,
          label: t(`access.${key}`),
        }))}
      />
      <Alert
        type="info"
        showIcon
        message={t(
          kind === 'personal' ? 'access.personalHint' : 'access.serviceHint',
        )}
      />
      <Space wrap>
        <Input.Search
          key={kind}
          placeholder={t('access.searchAccounts')}
          onSearch={(v) => {
            setSearch(v)
            setPage(1)
          }}
          allowClear
        />
        <Link to={kind === 'personal' ? '/users' : '/agents'}>
          <Button type="primary">{t('access.manageAccounts')}</Button>
        </Link>
        <Link to="/departments">{t('access.groups')}</Link>
      </Space>
      {error && <Alert type="error" message={error} />}
      <Table
        rowKey="id"
        dataSource={rows}
        loading={busy}
        pagination={{ current: page, total, pageSize: 20, onChange: setPage }}
        columns={[
          {
            title: t('access.account'),
            dataIndex: 'name',
            render: (name, r) => (
              <Button
                type="link"
                onClick={() => setParams({ kind, account: r.id })}
              >
                {name}
              </Button>
            ),
          },
          {
            title: t('access.authentication'),
            dataIndex: 'authentication',
            render: (value) =>
              t(`access.auth.${value}`, { defaultValue: value }),
          },
          {
            title: t('access.status'),
            dataIndex: 'status',
            render: (value) => (
              <Tag color={value === 'active' ? 'green' : 'default'}>
                {t(`access.state.${value}`, { defaultValue: value })}
              </Tag>
            ),
          },
          {
            title: t('access.actions'),
            render: (_, r) => (
              <Space>
                <Button
                  type="link"
                  onClick={() => setParams({ kind, account: r.id })}
                >
                  {t('access.manageAccess')}
                </Button>
                {kind === 'service' && (
                  <Link to={`/agents/${r.id}`}>
                    {t('access.connectionAndAdvanced')}
                  </Link>
                )}
              </Space>
            ),
          },
        ]}
      />
      <Drawer
        title={
          rows.find((x) => x.id === selected)?.name || t('access.manageAccess')
        }
        width={880}
        open={!!selected}
        onClose={() => setParams({ kind })}
        destroyOnClose
      >
        {selected && (
          <>
            <Typography.Paragraph>
              {t('access.accountGrantHint')}
            </Typography.Paragraph>
            <GrantPanel
              key={`${kind}:${selected}`}
              accountKind={kind}
              accountId={selected}
            />
          </>
        )}
      </Drawer>
    </Space>
  )
}
