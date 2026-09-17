import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Form,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  message,
} from 'antd'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import api, { getAPIErrorMessage } from '../../api/client'
import {
  listAccounts,
  listGrants,
  listResources,
  saveSQLGrant,
  type AccountKind,
  type Grant,
} from '../../api/accessConsole'
import {
  listInstanceCredentials,
  type InstanceCredential,
} from '../../api/credentials'
import type { Permission } from '../../api/instanceAccess'

export default function GrantPanel({
  resourceId,
  accountKind,
  accountId,
}: {
  resourceId?: string
  accountKind?: AccountKind
  accountId?: string
}) {
  const { t } = useTranslation()
  const [rows, setRows] = useState<Grant[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [open, setOpen] = useState(false)
  const [target, setTarget] = useState<Grant>()
  const [lookupSearch, setLookupSearch] = useState('')
  const [options, setOptions] = useState<{ value: string; label: string }[]>([])
  const [credentials, setCredentials] = useState<
    { value: string; label: string }[]
  >([])
  const [form] = Form.useForm()
  const kind = Form.useWatch('kind', form) as AccountKind | undefined
  const selected = Form.useWatch('target', form) as string | undefined
  const load = useCallback(async () => {
    setBusy(true)
    setError('')
    try {
      const { data } = await listGrants({
        resource_id: resourceId,
        account_kind: accountKind,
        account_id: accountId,
        offset: (page - 1) * 20,
      })
      setRows(data.items)
      setTotal(data.total)
    } catch (e) {
      setError(getAPIErrorMessage(e, t('access.failed')))
    } finally {
      setBusy(false)
    }
  }, [resourceId, accountKind, accountId, page, t])
  useEffect(() => {
    void load()
  }, [load])
  useEffect(() => {
    if (!open) return
    let active = true
    const request = resourceId
      ? listAccounts(kind || 'personal', lookupSearch)
      : listResources('database', lookupSearch)
    request
      .then(({ data }) => {
        if (active)
          setOptions(data.items.map((x) => ({ value: x.id, label: x.name })))
      })
      .catch((e) => {
        if (active) setError(getAPIErrorMessage(e, t('access.failed')))
      })
    return () => {
      active = false
    }
  }, [open, kind, resourceId, lookupSearch, t])
  const iid = resourceId || target?.resource_id || selected
  useEffect(() => {
    setCredentials([])
    if (!open || !iid) return
    let active = true
    ;(async () => {
      const items: InstanceCredential[] = []
      let total = 0
      do {
        const { data } = await listInstanceCredentials(iid, {
          offset: items.length,
          limit: 100,
        })
        total = data.total
        if (!data.items.length && items.length < total)
          throw new Error(t('access.failed'))
        items.push(...data.items)
      } while (items.length < total)
      return { data: { items } }
    })()
      .then(({ data }) => {
        if (active)
          setCredentials(
            data.items
              .filter(
                (c) => c.status === 'active' && c.purpose === 'direct_access',
              )
              .map((c) => ({
                value: c.id,
                label: `${c.name} · ${t(`access.${c.capability}`)}`,
              })),
          )
      })
      .catch((e) => {
        if (active) setError(getAPIErrorMessage(e, t('access.failed')))
      })
    return () => {
      active = false
    }
  }, [iid, open, t])
  const edit = (row?: Grant) => {
    setTarget(row)
    setError('')
    setOptions([])
    setLookupSearch('')
    form.resetFields()
    form.setFieldsValue({
      kind: accountKind || row?.account_kind || 'personal',
      target: row ? (resourceId ? row.account_id : row.resource_id) : undefined,
      credential: row?.credential_id,
      permission: row?.permission || 'readonly',
    })
    setOpen(true)
  }
  const save = async (values: {
    kind: AccountKind
    target: string
    credential: string
    permission: Permission
  }) => {
    const account = accountId || target?.account_id || values.target
    const resource = resourceId || target?.resource_id || values.target
    const type = accountKind || values.kind
    setBusy(true)
    try {
      await saveSQLGrant(
        type,
        account,
        resource,
        values.credential,
        values.permission,
      )
      setOpen(false)
      message.success(t('access.saved'))
      await load()
    } catch (e) {
      setError(getAPIErrorMessage(e, t('access.failed')))
    } finally {
      setBusy(false)
    }
  }
  const disable = (row: Grant) =>
    Modal.confirm({
      title: t('access.disableTitle'),
      content: t('access.disableHint'),
      onOk: async () => {
        try {
          if (row.account_kind === 'personal')
            await api.post(
              `/api/access/accounts/personal/${encodeURIComponent(row.account_id)}/resources/${encodeURIComponent(row.resource_id)}/disable`,
            )
          else
            await api.post(
              `/api/access/accounts/service/${encodeURIComponent(row.account_id)}/resources/${encodeURIComponent(row.resource_id)}/disable`,
            )
          await load()
        } catch (e) {
          message.error(getAPIErrorMessage(e, t('access.failed')))
          throw e
        }
      },
    })
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Alert type="info" showIcon message={t('access.grantHint')} />
      {error && <Alert type="error" message={error} />}
      <Button type="primary" onClick={() => edit()}>
        {t('access.addGrant')}
      </Button>
      <Table
        rowKey={(r) => `${r.account_kind}:${r.id}`}
        dataSource={rows}
        loading={busy}
        pagination={{ current: page, total, pageSize: 20, onChange: setPage }}
        columns={[
          {
            title: t(resourceId ? 'access.account' : 'access.resource'),
            render: (_, r) => (
              <Link
                to={
                  resourceId
                    ? r.account_kind === 'group'
                      ? '/departments'
                      : `/access?kind=${r.account_kind}&account=${r.account_id}`
                    : `/resources?resource=${r.resource_id}`
                }
              >
                {resourceId ? r.name : r.resource_name}
              </Link>
            ),
          },
          {
            title: t('access.source'),
            render: (_, r) => <Tag>{t(`access.${r.source}`)}</Tag>,
          },
          {
            title: t('access.permission'),
            render: (_, r) =>
              t(
                r.permission ? `access.${r.permission}` : 'access.metadataOnly',
              ),
          },
          {
            title: t('access.status'),
            render: (_, r) => (
              <Tag color={r.enabled ? 'green' : 'default'}>
                {t(r.enabled ? 'access.enabled' : 'access.disabled')}
              </Tag>
            ),
          },
          {
            title: t('access.actions'),
            render: (_, r) =>
              r.account_kind === 'group' ? (
                <Link to="/departments">{t('access.manageGroup')}</Link>
              ) : (
                <Space>
                  <Button type="link" onClick={() => edit(r)}>
                    {t('access.edit')}
                  </Button>
                  <Button
                    type="link"
                    danger
                    disabled={!r.enabled}
                    onClick={() => disable(r)}
                  >
                    {t('access.disable')}
                  </Button>
                </Space>
              ),
          },
        ]}
      />
      <Modal
        title={t(target ? 'access.editGrant' : 'access.addGrant')}
        open={open}
        onCancel={() => setOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={busy}
        destroyOnClose
      >
        {error && <Alert type="error" message={error} />}
        <Form form={form} layout="vertical" onFinish={save}>
          {resourceId && (
            <Form.Item name="kind" label={t('access.accountType')}>
              <Select
                disabled={!!target}
                onChange={() => form.setFieldValue('target', undefined)}
                options={['personal', 'service'].map((value) => ({
                  value,
                  label: t(`access.${value}`),
                }))}
              />
            </Form.Item>
          )}
          <Form.Item
            name="target"
            label={t(resourceId ? 'access.account' : 'access.resource')}
            rules={[{ required: true }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              disabled={!!target}
              options={
                target
                  ? [
                      {
                        value: resourceId
                          ? target.account_id
                          : target.resource_id,
                        label: resourceId ? target.name : target.resource_name,
                      },
                    ]
                  : options
              }
              filterOption={false}
              onSearch={setLookupSearch}
              onChange={() => {
                if (!resourceId) form.setFieldValue('credential', undefined)
              }}
            />
          </Form.Item>
          <Form.Item
            name="credential"
            label={t('access.credential')}
            extra={t('access.credentialHint')}
            rules={[{ required: true }]}
          >
            <Select options={credentials} />
          </Form.Item>
          <Form.Item name="permission" label={t('access.permission')}>
            <Select
              options={['readonly', 'readwrite'].map((value) => ({
                value,
                label: t(`access.${value}`),
              }))}
            />
          </Form.Item>
          <Alert type="info" message={t('access.preserveHint')} />
        </Form>
      </Modal>
    </Space>
  )
}
