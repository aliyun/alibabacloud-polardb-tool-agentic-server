import { useEffect, useState } from 'react'
import { Alert, Button, Modal, Space, Table, Tag, Tooltip, Typography } from 'antd'
import { FileSearchOutlined, UploadOutlined } from '@ant-design/icons'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import api, { getAPIErrorMessage } from '../../api/client'
import PageContainer from '../../components/PageContainer'
import MCPConnections from './MCPConnections'
import DocumentManager from './DocumentManager'

interface AccessibleDatabaseInstance {
  db_instance_id: string
  name: string
  db_type: string
  source: string
  status: string
  permission: string | null
  capabilities: string[]
}

interface AccessibleKnowledgeResource {
  knowledge_resource_id: string
  knowledge_space_id: string
  knowledge_space_name: string
  polarrag_instance_id: string
  polarrag_instance_name: string
  name: string
  kb_type: string
  usage: string | null
  upload_ready?: boolean
}

interface MyInstancesProps {
  isAdmin?: boolean
}

export default function MyInstances({ isAdmin = false }: MyInstancesProps) {
  const { t } = useTranslation()
  const [databaseInstances, setDatabaseInstances] = useState<
    AccessibleDatabaseInstance[]
  >([])
  const [knowledgeResources, setKnowledgeResources] = useState<
    AccessibleKnowledgeResource[]
  >([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [uploadResource, setUploadResource] =
    useState<AccessibleKnowledgeResource | null>(null)
  const [uploadFile, setUploadFile] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadResult, setUploadResult] = useState<string | null>(null)
  const [manageResource, setManageResource] =
    useState<AccessibleKnowledgeResource | null>(null)

  useEffect(() => {
    api
      .get('/api/me/resources')
      .then((response) => {
        setDatabaseInstances(response.data.database_instances || [])
        setKnowledgeResources(response.data.knowledge_resources || [])
      })
      .catch((requestError) =>
        setError(
          getAPIErrorMessage(
            requestError,
            'Could not load your accessible resources.',
          ),
        ),
      )
      .finally(() => setLoading(false))
  }, [])

  const databaseColumns = [
    { title: 'Name', dataIndex: 'name' },
    {
      title: 'Type',
      dataIndex: 'db_type',
      render: (value: string) => <Tag>{value}</Tag>,
    },
    { title: 'Source', dataIndex: 'source' },
    {
      title: 'Permission',
      dataIndex: 'permission',
      render: (value: string | null) => value || 'Not granted',
    },
    {
      title: 'Capabilities',
      dataIndex: 'capabilities',
      render: (values: string[]) => values.join(', '),
    },
    {
      title: 'Status',
      dataIndex: 'status',
      render: (value: string) => (
        <Tag color={value.toLowerCase() === 'active' ? 'green' : 'orange'}>
          {value}
        </Tag>
      ),
    },
  ]

  const upload = async () => {
    if (!uploadResource || !uploadFile) return
    setUploading(true)
    setError(null)
    const body = new FormData()
    body.append('knowledge_resource_id', uploadResource.knowledge_resource_id)
    body.append('file', uploadFile)
    try {
      const response = await api.post('/api/me/polarrag/documents', body)
      setUploadResult(
        `${response.data.filename} was accepted by PolarRAG with status ${response.data.status}.`,
      )
      setUploadResource(null)
      setUploadFile(null)
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, 'Could not upload this document.'))
    } finally {
      setUploading(false)
    }
  }

  const knowledgeColumns = [
    { title: 'Knowledge resource', dataIndex: 'name' },
    { title: 'PolarRAG instance', dataIndex: 'polarrag_instance_name' },
    { title: 'Space', dataIndex: 'knowledge_space_name' },
    {
      title: 'Type',
      dataIndex: 'kb_type',
      render: (value: string) => <Tag>{value}</Tag>,
    },
    { title: 'Usage', dataIndex: 'usage', render: (value: string | null) => value || 'Not specified' },
    ...(!isAdmin
      ? [
          {
            title: 'Actions',
            render: (_: unknown, resource: AccessibleKnowledgeResource) => (
              <Space>
                <Tooltip
                  title={
                    resource.upload_ready
                      ? undefined
                      : 'Ask an administrator to configure and validate the OSS AccessKey credentials for this Space.'
                  }
                >
                  <span>
                    <Button
                      size="small"
                      icon={<UploadOutlined />}
                      aria-label={`Upload to ${resource.name}`}
                      disabled={!resource.upload_ready}
                      onClick={() => {
                        setUploadResult(null)
                        setUploadResource(resource)
                      }}
                    >
                      {t('myInstances.upload')}
                    </Button>
                  </span>
                </Tooltip>
                <Button
                  size="small"
                  icon={<FileSearchOutlined />}
                  aria-label={`Manage documents for ${resource.name}`}
                  onClick={() => setManageResource(resource)}
                >
                  {t('myInstances.manageDocuments')}
                </Button>
              </Space>
            ),
          },
        ]
      : []),
  ]

  return (
    <PageContainer
      title={t('myInstances.title')}
      description={t('myInstances.combinedDescription')}
    >
      <Space direction="vertical" size={20} style={{ width: '100%' }}>
        {error && <Alert type="error" showIcon message={error} />}
        {uploadResult && <Alert type="success" showIcon message={uploadResult} />}
        {!isAdmin && <MCPConnections />}
        <Typography.Text strong>{t('myInstances.databaseInstances')}</Typography.Text>
        <Table
          dataSource={databaseInstances}
          columns={databaseColumns}
          rowKey="db_instance_id"
          loading={loading}
          pagination={false}
          locale={{ emptyText: 'No accessible database instances' }}
        />
        <Typography.Text strong>{t('myInstances.knowledgeResources')}</Typography.Text>
        {!loading && !error && knowledgeResources.length === 0 && (
          <Alert
            type="info"
            showIcon
            message={
              isAdmin
                ? 'No PolarRAG knowledge resources assigned to this administrator'
                : 'No accessible PolarRAG knowledge resources'
            }
            description={
              isAdmin ? (
                <>
                  {t('myInstances.adminNoAccess')}{' '}
                  <Link to="/instances?type=polarrag">
                    {t('myInstances.managePolarrag')}
                  </Link>{' '}
                  {t('myInstances.adminGuidance')}
                </>
              ) : (
                "Registering an instance does not grant user access. An administrator must enable and synchronize a Space, then map this PAS user to a principal in that Space's identity domain."
              )
            }
          />
        )}
        <Table
          dataSource={knowledgeResources}
          columns={knowledgeColumns}
          rowKey="knowledge_resource_id"
          loading={loading}
          pagination={false}
          locale={{ emptyText: 'No accessible knowledge resources' }}
        />
        <Modal
          title={t('myInstances.uploadDocument')}
          open={uploadResource !== null}
          okText={t('myInstances.upload')}
          okButtonProps={{ disabled: !uploadFile }}
          confirmLoading={uploading}
          onOk={() => void upload()}
          onCancel={() => {
            if (!uploading) {
              setUploadResource(null)
              setUploadFile(null)
            }
          }}
          destroyOnHidden
        >
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Alert
              type="info"
              showIcon
              message={uploadResource?.name}
              description={`${uploadResource?.polarrag_instance_name} · ${uploadResource?.knowledge_space_name}`}
            />
            <label>
              <Typography.Text strong>{t('myInstances.documentFile')}</Typography.Text>
              <input
                aria-label={t('myInstances.documentFile')}
                type="file"
                onChange={(event) => setUploadFile(event.target.files?.[0] ?? null)}
                style={{ display: 'block', marginTop: 8 }}
              />
            </label>
            <Typography.Text type="secondary">
              {t('myInstances.uploadHint')}
            </Typography.Text>
          </Space>
        </Modal>
        <DocumentManager
          resource={manageResource}
          onClose={() => setManageResource(null)}
        />
      </Space>
    </PageContainer>
  )
}
