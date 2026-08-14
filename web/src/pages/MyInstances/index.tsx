import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Drawer, Modal, Space, Table, Tag, Tooltip, Typography } from 'antd'
import { FileSearchOutlined, UploadOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import api, { getAPIErrorMessage } from '../../api/client'
import type { MyAgentConnection } from '../../api/agentConnections'
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
  kb_id: string
  kb_type: string
  upload_ready?: boolean
}

interface AccessibleKnowledgeSpace {
  knowledge_space_id: string
  knowledge_space_name: string
  polarrag_instance_id: string
  polarrag_instance_name: string
}

function uniqueKnowledgeSpaces(
  resources: AccessibleKnowledgeResource[],
): AccessibleKnowledgeSpace[] {
  return Array.from(
    new Map(
      resources.map((resource) => [
        `${resource.polarrag_instance_id}/${resource.knowledge_space_id}`,
        {
          knowledge_space_id: resource.knowledge_space_id,
          knowledge_space_name: resource.knowledge_space_name,
          polarrag_instance_id: resource.polarrag_instance_id,
          polarrag_instance_name: resource.polarrag_instance_name,
        },
      ]),
    ).values(),
  )
}

interface MyInstancesProps {
  isAdmin?: boolean
}

export default function MyInstances({ isAdmin = false }: MyInstancesProps) {
  const { t } = useTranslation()
  const [databaseInstances, setDatabaseInstances] = useState<
    AccessibleDatabaseInstance[]
  >([])
  const [knowledgeSpaces, setKnowledgeSpaces] = useState<
    AccessibleKnowledgeSpace[]
  >([])
  const [knowledgeResources, setKnowledgeResources] = useState<
    AccessibleKnowledgeResource[]
  >([])
  const [overviewLoading, setOverviewLoading] = useState(true)
  const [knowledgeLoading, setKnowledgeLoading] = useState(false)
  const [overviewError, setOverviewError] = useState<string | null>(null)
  const [knowledgeError, setKnowledgeError] = useState<string | null>(null)
  const [operationError, setOperationError] = useState<string | null>(null)
  const [uploadResource, setUploadResource] =
    useState<AccessibleKnowledgeResource | null>(null)
  const [uploadFile, setUploadFile] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadResult, setUploadResult] = useState<string | null>(null)
  const [manageResource, setManageResource] =
    useState<AccessibleKnowledgeResource | null>(null)
  const [selectedAgent, setSelectedAgent] = useState<MyAgentConnection | null>(null)
  const resourceRequest = useRef(0)

  const loadOverview = useCallback(() => {
    setOverviewLoading(true)
    setOverviewError(null)
    return api
      .get('/api/me/resources')
      .then((response) => {
        const resources = response.data.knowledge_resources || []
        setDatabaseInstances(response.data.database_instances || [])
        setKnowledgeSpaces(uniqueKnowledgeSpaces(resources))
      })
      .catch((requestError) => {
        setOverviewError(
          getAPIErrorMessage(
            requestError,
            'Could not load your accessible resources.',
          ),
        )
      })
      .finally(() => setOverviewLoading(false))
  }, [])

  const loadKnowledgeBases = useCallback((agentId: string) => {
    const requestId = ++resourceRequest.current
    setKnowledgeLoading(true)
    setKnowledgeError(null)
    return api
      .get('/api/me/resources', { params: { agent_id: agentId } })
      .then((response) => {
        if (requestId !== resourceRequest.current) return
        setKnowledgeResources(response.data.knowledge_resources || [])
      })
      .catch((requestError) => {
        if (requestId !== resourceRequest.current) return
        setKnowledgeError(
          getAPIErrorMessage(
            requestError,
            'Could not load your accessible resources.',
          ),
        )
      })
      .finally(() => {
        if (requestId === resourceRequest.current) setKnowledgeLoading(false)
      })
  }, [])

  useEffect(() => {
    void loadOverview()
  }, [loadOverview])

  const selectKnowledgeBases = (agent: MyAgentConnection) => {
    setSelectedAgent(agent)
    setKnowledgeResources([])
    setKnowledgeError(null)
    setUploadResource(null)
    setManageResource(null)
    void loadKnowledgeBases(agent.agent_id)
  }

  const closeKnowledgeBases = () => {
    ++resourceRequest.current
    setKnowledgeLoading(false)
    setKnowledgeError(null)
    setKnowledgeResources([])
    setSelectedAgent(null)
    setUploadResource(null)
    setManageResource(null)
  }

  const databaseColumns = [
    { title: t('myInstances.name'), dataIndex: 'name' },
    {
      title: t('myInstances.type'),
      dataIndex: 'db_type',
      render: (value: string) => <Tag>{value}</Tag>,
    },
    { title: t('myInstances.source'), dataIndex: 'source' },
    {
      title: t('myInstances.permission'),
      dataIndex: 'permission',
      render: (value: string | null) => value || t('myInstances.notGranted'),
    },
    {
      title: t('myInstances.capabilities'),
      dataIndex: 'capabilities',
      render: (values: string[]) => values.join(', '),
    },
    {
      title: t('myInstances.status'),
      dataIndex: 'status',
      render: (value: string) => (
        <Tag color={value.toLowerCase() === 'active' ? 'green' : 'orange'}>
          {value}
        </Tag>
      ),
    },
  ]

  const upload = async () => {
    if (!uploadResource || !uploadFile || !selectedAgent) return
    setUploading(true)
    setOperationError(null)
    const body = new FormData()
    body.append('agent_id', selectedAgent.agent_id)
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
      setOperationError(
        getAPIErrorMessage(requestError, 'Could not upload this document.'),
      )
    } finally {
      setUploading(false)
    }
  }

  const knowledgeColumns = [
    {
      title: t('myInstances.knowledgeResource'),
      render: (_: unknown, resource: AccessibleKnowledgeResource) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{resource.name}</Typography.Text>
          <Typography.Text type="secondary">
            {resource.knowledge_resource_id}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: t('myInstances.polarragInstance'),
      dataIndex: 'polarrag_instance_name',
    },
    { title: t('myInstances.knowledgeSpace'), dataIndex: 'knowledge_space_name' },
    {
      title: t('myInstances.type'),
      dataIndex: 'kb_type',
      render: (value: string) => <Tag>{value}</Tag>,
    },
    ...(!isAdmin
      ? [
          {
            title: t('myInstances.actions'),
            render: (_: unknown, resource: AccessibleKnowledgeResource) => (
              <Space>
                <Tooltip
                  title={
                    resource.upload_ready
                      ? undefined
                      : t('myInstances.uploadUnavailable')
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

  const knowledgeSpaceColumns = [
    { title: t('myInstances.knowledgeSpace'), dataIndex: 'knowledge_space_name' },
    {
      title: t('myInstances.polarragInstance'),
      dataIndex: 'polarrag_instance_name',
    },
  ]

  return (
    <PageContainer
      title={t('myInstances.title')}
      description={t('myInstances.combinedDescription')}
    >
      <Space direction="vertical" size={20} style={{ width: '100%' }}>
        {(overviewError || operationError) && (
          <Alert type="error" showIcon message={overviewError || operationError} />
        )}
        {uploadResult && <Alert type="success" showIcon message={uploadResult} />}
        {!isAdmin && <MCPConnections onSelectKnowledgeBases={selectKnowledgeBases} />}
        <Typography.Text strong>{t('myInstances.databaseInstances')}</Typography.Text>
        <Table
          dataSource={databaseInstances}
          columns={databaseColumns}
          rowKey="db_instance_id"
          loading={overviewLoading}
          pagination={false}
          locale={{ emptyText: 'No accessible database instances' }}
        />
        <Typography.Text strong>{t('myInstances.knowledgeSpaces')}</Typography.Text>
        <Table
          dataSource={knowledgeSpaces}
          columns={knowledgeSpaceColumns}
          rowKey={(space) => `${space.polarrag_instance_id}/${space.knowledge_space_id}`}
          loading={overviewLoading}
          pagination={{ pageSize: 10, showSizeChanger: true }}
          locale={{ emptyText: t('myInstances.noAccessibleKnowledgeSpaces') }}
        />
        <Drawer
          title={
            selectedAgent
              ? t('mcpConnections.knowledgeBasesFor', {
                  agent: selectedAgent.agent_name,
                })
              : undefined
          }
          open={selectedAgent !== null}
          width={960}
          onClose={closeKnowledgeBases}
        >
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            {knowledgeError && <Alert type="error" showIcon message={knowledgeError} />}
            <Table
              dataSource={knowledgeResources}
              columns={knowledgeColumns}
              rowKey="knowledge_resource_id"
              loading={knowledgeLoading}
              pagination={{ pageSize: 10, showSizeChanger: true }}
              locale={{ emptyText: t('myInstances.noAccessibleKnowledgeResources') }}
            />
          </Space>
        </Drawer>
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
          agentId={selectedAgent?.agent_id ?? null}
          onClose={() => setManageResource(null)}
        />
      </Space>
    </PageContainer>
  )
}
