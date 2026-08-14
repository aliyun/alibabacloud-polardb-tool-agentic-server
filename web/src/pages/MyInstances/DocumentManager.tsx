import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import { useTranslation } from 'react-i18next'
import {
  DeleteOutlined,
  ReloadOutlined,
  RetweetOutlined,
} from '@ant-design/icons'

import api, { getAPIErrorMessage } from '../../api/client'
import { formatDateTime } from '../../i18n/format'

export interface DocumentManagerResource {
  knowledge_resource_id: string
  knowledge_space_name: string
  polarrag_instance_name: string
  name: string
}

export interface ManagedDocument {
  doc_id: string
  kb_id?: string
  filename?: string
  file_size_bytes?: number
  created_at?: string
  status?: string
  chunk_count?: number
  revision_status?: string
}

interface DocumentManagerProps {
  resource: DocumentManagerResource | null
  agentId: string | null
  onClose: () => void
}

const PAGE_SIZE = 20

function documentName(document: ManagedDocument): string {
  return document.filename || document.doc_id
}

function formatFileSize(value: number | undefined): string {
  if (value === undefined) return '—'
  const units = ['B', 'KiB', 'MiB', 'GiB']
  let size = value
  let unitIndex = 0
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024
    unitIndex += 1
  }
  const digits = size >= 10 || Number.isInteger(size) ? 0 : 1
  return `${size.toFixed(digits)} ${units[unitIndex]}`
}

function formatUploadTime(
  value: string | undefined,
  locale: string,
): string {
  if (!value) return '—'
  const timestamp = new Date(value)
  if (Number.isNaN(timestamp.getTime())) return '—'
  return formatDateTime(timestamp, locale)
}

export default function DocumentManager({
  resource,
  agentId,
  onClose,
}: DocumentManagerProps) {
  const { t, i18n } = useTranslation()
  const [filename, setFilename] = useState('')
  const [documents, setDocuments] = useState<ManagedDocument[]>([])
  const [loading, setLoading] = useState(false)
  const [searchMode, setSearchMode] = useState(false)
  const [pageIndex, setPageIndex] = useState(0)
  const [pageCursors, setPageCursors] = useState<(string | null)[]>([null])
  const [nextAfterDocId, setNextAfterDocId] = useState<string | null>(null)
  const [working, setWorking] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<string | null>(null)
  const [rechunkDocument, setRechunkDocument] = useState<ManagedDocument | null>(null)
  const [chunkStrategy, setChunkStrategy] = useState('inherit')
  const [chunkMaxTokens, setChunkMaxTokens] = useState<number | null>(null)

  const loadPage = useCallback(async (afterDocId: string | null, index: number) => {
    if (!resource || !agentId) return
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const response = await api.post('/api/me/polarrag/documents/_list', {
        agent_id: agentId,
        knowledge_resource_id: resource.knowledge_resource_id,
        size: PAGE_SIZE,
        after_doc_id: afterDocId,
      })
      setDocuments(response.data.documents || [])
      setNextAfterDocId(response.data.next_after_doc_id || null)
      setPageIndex(index)
      setSearchMode(false)
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('documentManager.loadFailed')))
    } finally {
      setLoading(false)
    }
  }, [agentId, resource, t])

  useEffect(() => {
    setFilename('')
    setDocuments([])
    setSearchMode(false)
    setPageIndex(0)
    setPageCursors([null])
    setNextAfterDocId(null)
    setError(null)
    setResult(null)
    setRechunkDocument(null)
    if (resource && agentId) void loadPage(null, 0)
  }, [agentId, loadPage, resource])

  const find = async () => {
    if (!resource || !agentId || !filename.trim()) return
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const response = await api.post('/api/me/polarrag/documents/_find', {
        agent_id: agentId,
        knowledge_resource_id: resource.knowledge_resource_id,
        filename: filename.trim(),
        limit: 20,
      })
      setDocuments(response.data.documents || [])
      setSearchMode(true)
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('documentManager.findFailed')))
    } finally {
      setLoading(false)
    }
  }

  const nextPage = () => {
    if (!nextAfterDocId) return
    const index = pageIndex + 1
    setPageCursors((current) => [
      ...current.slice(0, index),
      nextAfterDocId,
    ])
    void loadPage(nextAfterDocId, index)
  }

  const previousPage = () => {
    if (pageIndex === 0) return
    const index = pageIndex - 1
    void loadPage(pageCursors[index] || null, index)
  }

  const refresh = () => {
    if (searchMode) {
      void find()
      return
    }
    void loadPage(pageCursors[pageIndex] || null, pageIndex)
  }

  const showAll = () => {
    setFilename('')
    setPageCursors([null])
    void loadPage(null, 0)
  }

  const remove = async (document: ManagedDocument) => {
    if (!resource || !agentId) return
    setWorking(`delete:${document.doc_id}`)
    setError(null)
    try {
      await api.delete(`/api/me/polarrag/documents/${encodeURIComponent(document.doc_id)}`, {
        data: {
          agent_id: agentId,
          knowledge_resource_id: resource.knowledge_resource_id,
        },
      })
      setDocuments((current) => current.filter((item) => item.doc_id !== document.doc_id))
      setResult(t('documentManager.deleteAccepted', { name: documentName(document) }))
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('documentManager.deleteFailed')))
    } finally {
      setWorking(null)
    }
  }

  const rechunk = async () => {
    if (!resource || !agentId || !rechunkDocument) return
    setWorking(`rechunk:${rechunkDocument.doc_id}`)
    setError(null)
    try {
      const response = await api.post(
        `/api/me/polarrag/documents/${encodeURIComponent(rechunkDocument.doc_id)}/rechunk`,
        {
          agent_id: agentId,
          knowledge_resource_id: resource.knowledge_resource_id,
          chunk_strategy: chunkStrategy,
          chunk_max_tokens: chunkStrategy === 'inherit' ? undefined : chunkMaxTokens ?? undefined,
        },
      )
      setDocuments((current) =>
        current.map((item) =>
          item.doc_id === rechunkDocument.doc_id
            ? { ...item, status: response.data.status || item.status }
            : item,
        ),
      )
      setResult(
        response.data.noop
          ? t('documentManager.strategyUnchanged', { name: documentName(rechunkDocument) })
          : t('documentManager.rechunkAccepted', { name: documentName(rechunkDocument) }),
      )
      setRechunkDocument(null)
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('documentManager.rechunkFailed')))
    } finally {
      setWorking(null)
    }
  }

  return (
    <>
      <Modal
        title={t('documentManager.title')}
        open={resource !== null}
        footer={null}
        width={1400}
        onCancel={() => working === null && onClose()}
        destroyOnHidden
      >
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Alert
            type="info"
            showIcon
            message={resource?.name}
            description={`${resource?.polarrag_instance_name} · ${resource?.knowledge_space_name}. PolarRAG remains the authority for READ, MANAGE, and EXECUTE permissions.`}
          />
          {error && <Alert type="error" showIcon message={error} />}
          {result && <Alert type="success" showIcon message={result} />}
          <Space wrap style={{ width: '100%' }}>
            <Input.Search
              aria-label={t('documentManager.filename')}
              placeholder={t('documentManager.searchPlaceholder')}
              value={filename}
              enterButton={t('documentManager.find')}
              loading={loading && searchMode}
              style={{ flex: 1, minWidth: 280 }}
              onChange={(event) => setFilename(event.target.value)}
              onSearch={() => void find()}
            />
            <Button
              icon={<ReloadOutlined />}
              loading={loading}
              onClick={refresh}
            >
              {t('documentManager.refresh')}
            </Button>
            {searchMode && <Button onClick={showAll}>{t('documentManager.showAll')}</Button>}
          </Space>
          <Table
            dataSource={documents}
            rowKey="doc_id"
            loading={loading}
            pagination={false}
            tableLayout="fixed"
            scroll={{ x: 1280 }}
            locale={{
              emptyText: searchMode
                ? t('documentManager.noMatches')
                : t('documentManager.noDocuments'),
            }}
            columns={[
              {
                title: t('documentManager.document'),
                dataIndex: 'filename',
                width: 220,
                ellipsis: true,
                render: (value) => value || t('documentManager.unnamed'),
              },
              {
                title: t('documentManager.documentId'),
                dataIndex: 'doc_id',
                width: 280,
                ellipsis: true,
              },
              {
                title: t('documentManager.fileSize'),
                dataIndex: 'file_size_bytes',
                width: 110,
                render: (value) => formatFileSize(value),
              },
              {
                title: t('documentManager.chunks'),
                dataIndex: 'chunk_count',
                width: 100,
                render: (value) => value ?? '—',
              },
              {
                title: t('documentManager.uploadedAt'),
                dataIndex: 'created_at',
                width: 190,
                render: (value) => formatUploadTime(
                  value,
                  i18n.resolvedLanguage ?? i18n.language,
                ),
              },
              {
                title: t('documentManager.status'),
                dataIndex: 'status',
                width: 140,
                render: (value) => <Tag>{value || t('documentManager.unknown')}</Tag>,
              },
              {
                title: t('documentManager.actions'),
                width: 240,
                fixed: 'right',
                render: (_, document) => (
                  <Space>
                    <Button
                      size="small"
                      icon={<RetweetOutlined />}
                      aria-label={t('documentManager.rechunkDocument', { name: documentName(document) })}
                      loading={working === `rechunk:${document.doc_id}`}
                      onClick={() => {
                        setChunkStrategy('inherit')
                        setChunkMaxTokens(null)
                        setRechunkDocument(document)
                      }}
                    >
                      {t('documentManager.rechunk')}
                    </Button>
                    <Popconfirm
                      title={t('documentManager.deleteTitle', { name: documentName(document) })}
                      description={t('documentManager.deleteDescription')}
                      okText={t('documentManager.confirmDelete')}
                      okButtonProps={{ danger: true }}
                      onConfirm={() => void remove(document)}
                    >
                      <Button
                        size="small"
                        danger
                        icon={<DeleteOutlined />}
                        aria-label={t('documentManager.deleteDocument', { name: documentName(document) })}
                        loading={working === `delete:${document.doc_id}`}
                      >
                        {t('documentManager.delete')}
                      </Button>
                    </Popconfirm>
                  </Space>
                ),
              },
            ]}
          />
          {!searchMode && (
            <Space style={{ display: 'flex', justifyContent: 'center' }}>
              <Button
                disabled={pageIndex === 0 || loading}
                onClick={previousPage}
              >
                {t('documentManager.previous')}
              </Button>
              <Typography.Text>{t('documentManager.page', { page: pageIndex + 1 })}</Typography.Text>
              <Button
                disabled={!nextAfterDocId || loading}
                onClick={nextPage}
              >
                {t('documentManager.next')}
              </Button>
            </Space>
          )}
        </Space>
      </Modal>
      <Modal
        title={t('documentManager.rechunkTitle')}
        open={rechunkDocument !== null}
        okText={t('documentManager.startRechunk')}
        confirmLoading={working?.startsWith('rechunk:')}
        onOk={() => void rechunk()}
        onCancel={() => working === null && setRechunkDocument(null)}
        destroyOnHidden
      >
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Typography.Text strong>
            {rechunkDocument ? documentName(rechunkDocument) : ''}
          </Typography.Text>
          <label>
            <Typography.Text>{t('documentManager.chunkStrategy')}</Typography.Text>
            <Select
              aria-label={t('documentManager.chunkStrategy')}
              value={chunkStrategy}
              style={{ display: 'block', marginTop: 8 }}
              options={[
                { value: 'inherit', label: t('documentManager.useSpaceDefault') },
                { value: 'hybrid', label: t('documentManager.hybrid') },
                { value: 'hierarchical', label: t('documentManager.hierarchical') },
              ]}
              onChange={setChunkStrategy}
            />
          </label>
          <label>
            <Typography.Text>{t('documentManager.maxTokensOptional')}</Typography.Text>
            <InputNumber
              aria-label={t('documentManager.maxTokens')}
              min={1}
              max={100000}
              value={chunkMaxTokens}
              disabled={chunkStrategy === 'inherit'}
              style={{ display: 'block', marginTop: 8, width: '100%' }}
              onChange={setChunkMaxTokens}
            />
          </label>
        </Space>
      </Modal>
    </>
  )
}
