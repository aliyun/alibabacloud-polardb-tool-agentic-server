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
  status?: string
  chunk_count?: number
  active_generation?: number
  revision_status?: string
}

interface DocumentManagerProps {
  resource: DocumentManagerResource | null
  onClose: () => void
}

const PAGE_SIZE = 20

export default function DocumentManager({
  resource,
  onClose,
}: DocumentManagerProps) {
  const { t } = useTranslation()
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
    if (!resource) return
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const response = await api.post('/api/me/polarrag/documents/_list', {
        knowledge_resource_id: resource.knowledge_resource_id,
        size: PAGE_SIZE,
        after_doc_id: afterDocId,
      })
      setDocuments(response.data.documents || [])
      setNextAfterDocId(response.data.next_after_doc_id || null)
      setPageIndex(index)
      setSearchMode(false)
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, 'Could not load documents.'))
    } finally {
      setLoading(false)
    }
  }, [resource])

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
    if (resource) void loadPage(null, 0)
  }, [loadPage, resource])

  const find = async () => {
    if (!resource || !filename.trim()) return
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const response = await api.post('/api/me/polarrag/documents/_find', {
        knowledge_resource_id: resource.knowledge_resource_id,
        filename: filename.trim(),
        limit: 20,
      })
      setDocuments(response.data.documents || [])
      setSearchMode(true)
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, 'Could not find documents.'))
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
    if (!resource) return
    setWorking(`delete:${document.doc_id}`)
    setError(null)
    try {
      await api.delete(`/api/me/polarrag/documents/${encodeURIComponent(document.doc_id)}`, {
        data: { knowledge_resource_id: resource.knowledge_resource_id },
      })
      setDocuments((current) => current.filter((item) => item.doc_id !== document.doc_id))
      setResult(`${document.filename || document.doc_id} deletion was accepted by PolarRAG.`)
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, 'Could not delete this document.'))
    } finally {
      setWorking(null)
    }
  }

  const rechunk = async () => {
    if (!resource || !rechunkDocument) return
    setWorking(`rechunk:${rechunkDocument.doc_id}`)
    setError(null)
    try {
      const response = await api.post(
        `/api/me/polarrag/documents/${encodeURIComponent(rechunkDocument.doc_id)}/rechunk`,
        {
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
          ? `${rechunkDocument.filename || rechunkDocument.doc_id} already uses that strategy.`
          : `${rechunkDocument.filename || rechunkDocument.doc_id} rechunk was accepted by PolarRAG.`,
      )
      setRechunkDocument(null)
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, 'Could not rechunk this document.'))
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
        width={1200}
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
              enterButton="Find"
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
            scroll={{ x: 1080 }}
            locale={{
              emptyText: searchMode
                ? 'No documents match this filename'
                : 'No readable documents on this page',
            }}
            columns={[
              {
                title: 'Document',
                dataIndex: 'filename',
                width: 220,
                ellipsis: true,
                render: (value) => value || 'Unnamed',
              },
              {
                title: 'Document ID',
                dataIndex: 'doc_id',
                width: 280,
                ellipsis: true,
              },
              {
                title: 'Status',
                dataIndex: 'status',
                width: 140,
                render: (value) => <Tag>{value || 'UNKNOWN'}</Tag>,
              },
              {
                title: 'Chunks',
                dataIndex: 'chunk_count',
                width: 90,
                render: (value) => value ?? '—',
              },
              {
                title: 'Generation',
                dataIndex: 'active_generation',
                width: 110,
                render: (value) => value ?? '—',
              },
              {
                title: 'Actions',
                width: 240,
                fixed: 'right',
                render: (_, document) => (
                  <Space>
                    <Button
                      size="small"
                      icon={<RetweetOutlined />}
                      aria-label={`Rechunk ${document.filename || document.doc_id}`}
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
                      title={`Delete ${document.filename || document.doc_id}?`}
                      description={t('documentManager.deleteDescription')}
                      okText={t('documentManager.confirmDelete')}
                      okButtonProps={{ danger: true }}
                      onConfirm={() => void remove(document)}
                    >
                      <Button
                        size="small"
                        danger
                        icon={<DeleteOutlined />}
                        aria-label={`Delete ${document.filename || document.doc_id}`}
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
            {rechunkDocument?.filename || rechunkDocument?.doc_id}
          </Typography.Text>
          <label>
            <Typography.Text>{t('documentManager.chunkStrategy')}</Typography.Text>
            <Select
              aria-label={t('documentManager.chunkStrategy')}
              value={chunkStrategy}
              style={{ display: 'block', marginTop: 8 }}
              options={[
                { value: 'inherit', label: 'Use Space default' },
                { value: 'hybrid', label: 'Hybrid' },
                { value: 'hierarchical', label: 'Hierarchical' },
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
