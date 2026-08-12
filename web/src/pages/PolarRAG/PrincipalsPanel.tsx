import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Empty,
  Form,
  Input,
  Modal,
  Select,
  Skeleton,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { useTranslation } from 'react-i18next'
import {
  DeleteOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  TeamOutlined,
  UserOutlined,
} from '@ant-design/icons'

import { getAPIErrorMessage } from '../../api/client'
import {
  createEnterprisePrincipal,
  deleteEnterprisePrincipal,
  listEnterprisePrincipals,
  updateEnterprisePrincipal,
  type CreateEnterprisePrincipalInput,
  type EnterprisePrincipal,
} from '../../api/polarrag'
import './PolarRAG.css'

const { Text } = Typography

interface PrincipalsPanelProps {
  userId: string
  userName: string
}

export default function PrincipalsPanel({
  userId,
  userName,
}: PrincipalsPanelProps) {
  const { t } = useTranslation()
  const [principals, setPrincipals] = useState<EnterprisePrincipal[]>([])
  const [principalsLoading, setPrincipalsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [addOpen, setAddOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [updatingId, setUpdatingId] = useState<string | null>(null)
  const [form] = Form.useForm<CreateEnterprisePrincipalInput>()

  const loadPrincipals = useCallback(async (userId: string) => {
    setPrincipalsLoading(true)
    setError(null)
    try {
      const response = await listEnterprisePrincipals(userId)
      setPrincipals(response.data.items)
    } catch (requestError) {
      setError(
        getAPIErrorMessage(
          requestError,
          'Could not load enterprise principals.',
        ),
      )
    } finally {
      setPrincipalsLoading(false)
    }
  }, [])

  useEffect(() => {
    setPrincipals([])
    void loadPrincipals(userId)
  }, [loadPrincipals, userId])

  const addPrincipal = async (values: CreateEnterprisePrincipalInput) => {
    setSaving(true)
    setError(null)
    try {
      await createEnterprisePrincipal(userId, {
        ...values,
        identity_domain: values.identity_domain.trim(),
        principal_id: values.principal_id.trim(),
        valid_until: values.valid_until || null,
      })
      message.success('Enterprise principal added')
      setAddOpen(false)
      form.resetFields()
      await loadPrincipals(userId)
    } catch (requestError) {
      setError(
        getAPIErrorMessage(
          requestError,
          'Could not add this enterprise principal.',
        ),
      )
    } finally {
      setSaving(false)
    }
  }

  const toggleStatus = async (principal: EnterprisePrincipal) => {
    const nextStatus =
      principal.status === 'active' ? 'disabled' : 'active'
    setUpdatingId(principal.id)
    try {
      await updateEnterprisePrincipal(
        userId,
        principal.id,
        { status: nextStatus },
      )
      message.success(`Principal ${nextStatus}`)
      await loadPrincipals(userId)
    } catch (requestError) {
      setError(
        getAPIErrorMessage(
          requestError,
          'Could not update principal status.',
        ),
      )
    } finally {
      setUpdatingId(null)
    }
  }

  const remove = (principal: EnterprisePrincipal) => {
    Modal.confirm({
      title: 'Delete enterprise principal?',
      content: t('principals.removeDescription', {
        principalId: principal.principal_id,
        userName,
      }),
      okText: 'Delete',
      okButtonProps: { danger: true },
      async onOk() {
        await deleteEnterprisePrincipal(userId, principal.id)
        message.success('Enterprise principal deleted')
        await loadPrincipals(userId)
      },
    })
  }

  return (
    <Space direction="vertical" size={20} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message={t('principals.identityTitle')}
        description={t('principals.identityDescription')}
      />

      {error && <Alert type="error" showIcon message={error} />}

      {principalsLoading ? (
        <Skeleton active paragraph={{ rows: 5 }} />
      ) : (
        <>
          <div className="polarrag-section-heading">
            <Space direction="vertical" size={0}>
              <Text strong>{userName}</Text>
              <Text type="secondary">
                {t('principals.assignmentCount', { count: principals.length })}
              </Text>
            </Space>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => {
                form.resetFields()
                setAddOpen(true)
              }}
            >
              {t('principals.addPrincipal')}
            </Button>
          </div>
          {principals.length === 0 ? (
            <Empty description={t('principals.empty')}>
              <Button type="primary" onClick={() => setAddOpen(true)}>
                {t('principals.addPrincipal')}
              </Button>
            </Empty>
          ) : (
            <Table
              rowKey="id"
              dataSource={principals}
              pagination={false}
              columns={[
                {
                  title: 'Principal',
                  render: (_: unknown, record) => (
                    <Space>
                      {record.principal_type === 'user' ? (
                        <UserOutlined />
                      ) : (
                        <TeamOutlined />
                      )}
                      <Space direction="vertical" size={0}>
                        <Text strong>{record.principal_id}</Text>
                        <Text type="secondary">
                          {record.provider} · {record.principal_type}
                        </Text>
                      </Space>
                    </Space>
                  ),
                },
                {
                  title: 'Identity domain',
                  dataIndex: 'identity_domain',
                  render: (value: string) => <Tag>{value}</Tag>,
                },
                {
                  title: 'Source',
                  dataIndex: 'source',
                  render: (value: string) => <Tag>{value}</Tag>,
                },
                {
                  title: 'Status',
                  dataIndex: 'status',
                  render: (value: EnterprisePrincipal['status']) => (
                    <Tag color={value === 'active' ? 'green' : 'red'}>
                      {value}
                    </Tag>
                  ),
                },
                {
                  title: 'Valid until',
                  dataIndex: 'valid_until',
                  render: (value: string | null) =>
                    value ? new Date(value).toLocaleString() : 'No expiry',
                },
                {
                  title: 'Actions',
                  render: (_: unknown, record) => (
                    <Space>
                      <Button
                        size="small"
                        icon={
                          record.status === 'active' ? (
                            <PauseCircleOutlined />
                          ) : (
                            <PlayCircleOutlined />
                          )
                        }
                        loading={updatingId === record.id}
                        aria-label={`${record.status === 'active' ? 'Disable' : 'Enable'} ${record.principal_id}`}
                        onClick={() => void toggleStatus(record)}
                      >
                        {record.status === 'active' ? 'Disable' : 'Enable'}
                      </Button>
                      <Button
                        size="small"
                        danger
                        icon={<DeleteOutlined />}
                        aria-label={`Delete ${record.principal_id}`}
                        onClick={() => remove(record)}
                      >
                        {t('principals.delete')}
                      </Button>
                    </Space>
                  ),
                },
              ]}
            />
          )}
        </>
      )}

      <Modal
        title={t('principals.addTitle')}
        open={addOpen}
        okText={t('principals.add')}
        confirmLoading={saving}
        onOk={() => form.submit()}
        onCancel={() => {
          if (!saving) {
            setAddOpen(false)
            form.resetFields()
          }
        }}
        destroyOnHidden
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={{
            provider: 'feishu',
            principal_type: 'user',
            valid_until: null,
          }}
          onFinish={(values) => void addPrincipal(values)}
        >
          <Form.Item
            name="identity_domain"
            label={t('principals.identityDomain')}
            rules={[{ required: true }]}
          >
            <Input placeholder={t('principals.domainPlaceholder')} />
          </Form.Item>
          <Space align="start" style={{ display: 'flex' }}>
            <Form.Item
              name="provider"
              label={t('principals.provider')}
              rules={[{ required: true }]}
              style={{ flex: 1 }}
            >
              <Select
                aria-label={t('principals.provider')}
                options={[
                  { value: 'feishu', label: 'Feishu' },
                  { value: 'sharepoint', label: 'SharePoint' },
                ]}
              />
            </Form.Item>
            <Form.Item
              name="principal_type"
              label={t('principals.principalType')}
              rules={[{ required: true }]}
              style={{ flex: 1 }}
            >
              <Select
                options={[
                  { value: 'user', label: 'User' },
                  { value: 'group', label: 'Group' },
                ]}
              />
            </Form.Item>
          </Space>
          <Form.Item
            name="principal_id"
            label={t('principals.principalId')}
            rules={[{ required: true }]}
            extra={t('principals.principalIdHint')}
          >
            <Input autoComplete="off" />
          </Form.Item>
          <Form.Item name="valid_until" label={t('principals.validUntil')}>
            <Input type="datetime-local" />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  )
}
