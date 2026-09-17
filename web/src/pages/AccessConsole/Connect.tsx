import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  InputNumber,
  Modal,
  Space,
  Table,
  Tabs,
  Typography,
  message,
} from 'antd'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import {
  issuePersonalToken,
  listPersonalTokens,
  revokePersonalToken,
  type PersonalToken,
} from '../../api/accessConsole'
import { getAPIErrorMessage } from '../../api/client'
import { formatDateTime } from '../../i18n/format'
import MyResources from './MyResources'
export default function Connect() {
  const { t, i18n } = useTranslation()
  const [url, setUrl] = useState('')
  const [oauthReady, setOAuthReady] = useState(false)
  const [tokens, setTokens] = useState<PersonalToken[]>([])
  const [secret, setSecret] = useState('')
  const [days, setDays] = useState(90)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const load = useCallback(async () => {
    setBusy(true)
    setError('')
    try {
      const { data } = await listPersonalTokens()
      setUrl(new URL(data.mcp_url, window.location.origin).toString())
      setOAuthReady(data.oauth_ready)
      setTokens(data.items)
    } catch (e) {
      setError(getAPIErrorMessage(e, t('access.failed')))
    } finally {
      setBusy(false)
    }
  }, [t])
  useEffect(() => {
    void load()
  }, [load])
  const issue = async () => {
    setBusy(true)
    setError('')
    try {
      const { data } = await issuePersonalToken(days)
      setSecret(data.token || '')
      await load()
    } catch (e) {
      setError(getAPIErrorMessage(e, t('access.failed')))
    } finally {
      setBusy(false)
    }
  }
  const revoke = (row: PersonalToken) =>
    Modal.confirm({
      title: t('access.revokeToken'),
      content: t('access.revokeHint'),
      onOk: async () => {
        try {
          await revokePersonalToken(row.id)
          setSecret('')
          await load()
        } catch (e) {
          message.error(getAPIErrorMessage(e, t('access.failed')))
          throw e
        }
      },
    })
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Typography.Title level={2}>{t('access.connect')}</Typography.Title>
        <Typography.Text type="secondary">
          {t('access.connectHint')}
        </Typography.Text>
      </div>
      {error && <Alert type="error" message={error} />}
      <Card title={t('access.personalConnection')} loading={!url && busy}>
        <Typography.Paragraph copyable={url ? { text: url } : false}>
          <Typography.Text code>{url}</Typography.Text>
        </Typography.Paragraph>
        <Tabs
          defaultActiveKey={oauthReady ? 'oauth' : 'token'}
          items={[
            {
              key: 'oauth',
              label: t('access.oauth'),
              children: (
                <Space direction="vertical">
                  {!oauthReady && (
                    <Alert type="warning" message={t('access.oauthNotReady')} />
                  )}
                  <Typography.Paragraph>
                    {t('access.oauthHint')}
                  </Typography.Paragraph>
                  <Typography.Text type="secondary">
                    {t('access.oauthResource')}
                  </Typography.Text>
                </Space>
              ),
            },
            {
              key: 'token',
              label: t('access.token'),
              children: (
                <Space direction="vertical" style={{ width: '100%' }}>
                  <Typography.Paragraph>
                    {t('access.tokenHint')}
                  </Typography.Paragraph>
                  <Space>
                    <InputNumber
                      aria-label={t('access.expiryDays')}
                      min={1}
                      max={365}
                      value={days}
                      onChange={(v) => setDays(v || 90)}
                    />
                    <Typography.Text>{t('access.expiryDays')}</Typography.Text>
                    <Button
                      type="primary"
                      loading={busy}
                      disabled={tokens.some((x) => x.status === 'active')}
                      onClick={() => void issue()}
                    >
                      {t('access.generateToken')}
                    </Button>
                  </Space>
                  <Table
                    rowKey="id"
                    dataSource={tokens}
                    pagination={false}
                    columns={[
                      {
                        title: t('access.status'),
                        dataIndex: 'status',
                        render: (v) =>
                          t(`access.state.${v}`, { defaultValue: v }),
                      },
                      {
                        title: t('access.expiresAt'),
                        dataIndex: 'expires_at',
                        render: (v) => formatDateTime(v, i18n.language),
                      },
                      {
                        title: t('access.actions'),
                        render: (_, row) => (
                          <Button
                            type="link"
                            danger
                            disabled={row.status !== 'active'}
                            onClick={() => revoke(row)}
                          >
                            {t('access.revokeToken')}
                          </Button>
                        ),
                      },
                    ]}
                  />
                </Space>
              ),
            },
          ]}
        />
      </Card>
      <MyResources compact />
      <Typography.Paragraph>
        <Link to="/my-instances">{t('access.legacy')}</Link>
      </Typography.Paragraph>
      <Modal
        title={t('access.tokenCreated')}
        open={!!secret}
        footer={
          <Button type="primary" onClick={() => setSecret('')}>
            {t('access.savedToken')}
          </Button>
        }
        onCancel={() => setSecret('')}
        destroyOnClose
      >
        <Alert type="warning" message={t('access.oneTime')} />
        <Typography.Paragraph
          style={{ marginTop: 20, overflowWrap: 'anywhere' }}
          copyable={{ text: secret }}
          code
        >
          {secret}
        </Typography.Paragraph>
        <Typography.Paragraph>{t('access.headerHint')}</Typography.Paragraph>
      </Modal>
    </Space>
  )
}
