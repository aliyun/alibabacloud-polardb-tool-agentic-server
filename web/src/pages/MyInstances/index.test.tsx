import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from '../../api/client'
import MyInstances from './index'

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    default: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  }
})

describe('My Instances page', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows current-user database instances and PolarRAG resources', async () => {
    vi.mocked(api.get).mockImplementation((url) =>
      Promise.resolve({
        data:
          url === '/api/me/agent-connections'
            ? []
            : {
                database_instances: [
                  {
                    db_instance_id: 'db-1',
                    name: 'Orders',
                    db_type: 'polardb_mysql',
                    source: 'bound',
                    status: 'ACTIVE',
                    permission: 'readonly',
                    capabilities: ['list', 'describe'],
                  },
                ],
                knowledge_resources: [
                  {
                    knowledge_resource_id: 'resource-1',
                    knowledge_space_id: 'space-1',
                    knowledge_space_name: 'Enterprise Space',
                    polarrag_instance_id: 'rag-1',
                    polarrag_instance_name: 'Primary RAG',
                    name: 'Public KB',
                    kb_type: 'PUBLIC',
                    usage: null,
                    upload_ready: true,
                  },
                ],
              },
      } as never),
    )

    render(<MyInstances isAdmin />)

    expect(await screen.findByText('Orders')).toBeInTheDocument()
    expect(screen.getByText('Public KB')).toBeInTheDocument()
    expect(screen.getByText('Primary RAG')).toBeInTheDocument()
    expect(api.get).toHaveBeenCalledWith('/api/me/resources')
  })

  it('lets a member upload to an upload-ready visible KB', async () => {
    const user = userEvent.setup()
    vi.mocked(api.get).mockImplementation((url) =>
      Promise.resolve({
        data:
          url === '/api/me/agent-connections'
            ? []
            : {
                database_instances: [],
                knowledge_resources: [
                  {
                    knowledge_resource_id: 'resource-1',
                    knowledge_space_id: 'space-1',
                    knowledge_space_name: 'Engineering',
                    polarrag_instance_id: 'rag-1',
                    polarrag_instance_name: 'Primary RAG',
                    name: 'Public KB',
                    kb_type: 'PUBLIC',
                    usage: null,
                    upload_ready: true,
                  },
                ],
              },
      } as never),
    )
    vi.mocked(api.post).mockResolvedValue({
      data: { doc_id: 'doc-1', filename: 'guide.md', status: 'DISPATCHED' },
    } as never)

    render(
      <MemoryRouter>
        <MyInstances />
      </MemoryRouter>,
    )
    await user.click(await screen.findByRole('button', { name: /upload to Public KB/i }))
    const dialog = screen.getByRole('dialog', { name: /upload document/i })
    const file = new File(['document body'], 'guide.md', { type: 'text/markdown' })
    await user.upload(within(dialog).getByLabelText(/document file/i), file)
    await user.click(within(dialog).getByRole('button', { name: /^upload$/i }))

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith(
        '/api/me/polarrag/documents',
        expect.any(FormData),
      ),
    )
    const [url, body] = vi.mocked(api.post).mock.calls[0]
    expect(url).toBe('/api/me/polarrag/documents')
    expect(body).toBeInstanceOf(FormData)
    expect((body as FormData).get('knowledge_resource_id')).toBe('resource-1')
    expect((body as FormData).get('file')).toBe(file)
    expect(await screen.findByText(/guide.md was accepted/i)).toBeInTheDocument()
    expect(
      screen.queryByRole('dialog', { name: /manage documents/i }),
    ).not.toBeInTheDocument()
  })

  it('explains why document upload is unavailable', async () => {
    const user = userEvent.setup()
    vi.mocked(api.get).mockImplementation((url) =>
      Promise.resolve({
        data:
          url === '/api/me/agent-connections'
            ? []
            : {
                database_instances: [],
                knowledge_resources: [
                  {
                    knowledge_resource_id: 'resource-1',
                    knowledge_space_id: 'space-1',
                    knowledge_space_name: 'Engineering',
                    polarrag_instance_id: 'rag-1',
                    polarrag_instance_name: 'Primary RAG',
                    name: 'Public KB',
                    kb_type: 'PUBLIC',
                    usage: null,
                    upload_ready: false,
                  },
                ],
              },
      } as never),
    )

    render(
      <MemoryRouter>
        <MyInstances />
      </MemoryRouter>,
    )

    const upload = await screen.findByRole('button', {
      name: /upload to Public KB/i,
    })
    expect(upload).toBeDisabled()
    await user.hover(upload.parentElement!)
    expect(await screen.findByRole('tooltip')).toHaveTextContent(
      'Ask an administrator to configure and validate the OSS AccessKey credentials for this Space.',
    )
  })

  it('never shows document upload actions to an administrator', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: {
        database_instances: [],
        knowledge_resources: [
          {
            knowledge_resource_id: 'resource-1',
            knowledge_space_id: 'space-1',
            knowledge_space_name: 'Engineering',
            polarrag_instance_id: 'rag-1',
            polarrag_instance_name: 'Primary RAG',
            name: 'Public KB',
            kb_type: 'PUBLIC',
            usage: null,
            upload_ready: true,
          },
        ],
      },
    } as never)

    render(<MyInstances isAdmin />)

    expect(await screen.findByText('Public KB')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /upload/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /manage documents/i })).not.toBeInTheDocument()
  })

  it('loads, refreshes, and paginates readable documents on demand', async () => {
    const user = userEvent.setup()
    vi.mocked(api.get).mockImplementation((url) =>
      Promise.resolve({
        data:
          url === '/api/me/agent-connections'
            ? []
            : {
                database_instances: [],
                knowledge_resources: [
                  {
                    knowledge_resource_id: 'resource-1',
                    knowledge_space_id: 'space-1',
                    knowledge_space_name: 'Engineering',
                    polarrag_instance_id: 'rag-1',
                    polarrag_instance_name: 'Primary RAG',
                    name: 'Public KB',
                    kb_type: 'PUBLIC',
                    usage: null,
                    upload_ready: true,
                  },
                ],
              },
      } as never),
    )
    vi.mocked(api.post).mockImplementation((_url, body) => {
      const afterDocId = (body as { after_doc_id?: string | null }).after_doc_id
      return Promise.resolve({
        data: afterDocId
          ? {
              documents: [
                {
                  doc_id: 'doc-b',
                  filename: 'second.md',
                  status: 'COMPLETED',
                  chunk_count: 12,
                },
              ],
              has_more: false,
              next_after_doc_id: null,
            }
          : {
              documents: [
                {
                  doc_id: 'doc-a',
                  filename: 'first.md',
                  status: 'CHUNKING',
                },
              ],
              has_more: true,
              next_after_doc_id: 'doc-a',
            },
      } as never)
    })

    render(
      <MemoryRouter>
        <MyInstances />
      </MemoryRouter>,
    )
    await user.click(
      await screen.findByRole('button', {
        name: /manage documents for Public KB/i,
      }),
    )
    const manager = screen.getByRole('dialog', { name: /manage documents/i })

    expect(await within(manager).findByText('first.md')).toBeInTheDocument()
    expect(within(manager).getByText('CHUNKING')).toBeInTheDocument()
    expect(api.post).toHaveBeenCalledWith(
      '/api/me/polarrag/documents/_list',
      {
        knowledge_resource_id: 'resource-1',
        size: 20,
        after_doc_id: null,
      },
    )

    await user.click(within(manager).getByRole('button', { name: 'Next' }))
    expect(await within(manager).findByText('second.md')).toBeInTheDocument()
    expect(within(manager).getByText('12')).toBeInTheDocument()
    await user.click(within(manager).getByRole('button', { name: /refresh/i }))
    await waitFor(() =>
      expect(
        vi.mocked(api.post).mock.calls.filter(
          ([url, body]) =>
            url === '/api/me/polarrag/documents/_list' &&
            (body as { after_doc_id?: string }).after_doc_id === 'doc-a',
        ),
      ).toHaveLength(2),
    )
  })

  it('lets a member find, rechunk, and delete an authorized document', async () => {
    const user = userEvent.setup()
    vi.mocked(api.get).mockImplementation((url) =>
      Promise.resolve({
        data:
          url === '/api/me/agent-connections'
            ? []
            : {
                database_instances: [],
                knowledge_resources: [
                  {
                    knowledge_resource_id: 'resource-1',
                    knowledge_space_id: 'space-1',
                    knowledge_space_name: 'Engineering',
                    polarrag_instance_id: 'rag-1',
                    polarrag_instance_name: 'Primary RAG',
                    name: 'Public KB',
                    kb_type: 'PUBLIC',
                    usage: null,
                    upload_ready: false,
                  },
                ],
              },
      } as never),
    )
    vi.mocked(api.post).mockImplementation((url) =>
      Promise.resolve({
        data: url.endsWith('/_list')
          ? {
              documents: [],
              has_more: false,
              next_after_doc_id: null,
            }
          : url.endsWith('/_find')
          ? {
              documents: [
                {
                  doc_id: 'doc-a',
                  kb_id: 'public-kb',
                  filename: 'guide.md',
                  status: 'COMPLETED',
                  active_generation: 0,
                },
              ],
            }
          : {
              doc_id: 'doc-a',
              status: 'RECHUNKING',
              noop: false,
              target_generation: 1,
            },
      } as never),
    )
    vi.mocked(api.delete).mockResolvedValue({
      data: { doc_id: 'doc-a', status: 'DELETING', task_id: 'delete-task' },
    } as never)

    render(
      <MemoryRouter>
        <MyInstances />
      </MemoryRouter>,
    )
    await user.click(
      await screen.findByRole('button', {
        name: /manage documents for Public KB/i,
      }),
    )
    const manager = screen.getByRole('dialog', { name: /manage documents/i })
    await user.type(within(manager).getByLabelText(/document filename/i), 'guide')
    await user.click(within(manager).getByRole('button', { name: /^find$/i }))

    expect(await within(manager).findByText('guide.md')).toBeInTheDocument()
    expect(api.post).toHaveBeenCalledWith(
      '/api/me/polarrag/documents/_find',
      {
        knowledge_resource_id: 'resource-1',
        filename: 'guide',
        limit: 20,
      },
    )

    await user.click(
      within(manager).getByRole('button', { name: /rechunk guide.md/i }),
    )
    await user.click(
      await screen.findByRole('button', { name: /start rechunk/i }),
    )
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith(
        '/api/me/polarrag/documents/doc-a/rechunk',
        {
          knowledge_resource_id: 'resource-1',
          chunk_strategy: 'inherit',
          chunk_max_tokens: undefined,
        },
      ),
    )

    await user.click(
      within(manager).getByRole('button', { name: /delete guide.md/i }),
    )
    await user.click(
      await screen.findByRole('button', { name: /confirm delete/i }),
    )
    await waitFor(() =>
      expect(api.delete).toHaveBeenCalledWith(
        '/api/me/polarrag/documents/doc-a',
        { data: { knowledge_resource_id: 'resource-1' } },
      ),
    )
  })

  it('shows a useful error instead of silently rendering an empty table', async () => {
    vi.mocked(api.get).mockRejectedValue(new Error('network failed'))

    render(<MyInstances isAdmin />)

    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Could not load your accessible resources.',
      ),
    )
  })

  it('directs an administrator with no resources to PolarRAG management', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: {
        database_instances: [],
        knowledge_resources: [],
      },
    } as never)

    render(
      <MemoryRouter>
        <MyInstances isAdmin />
      </MemoryRouter>,
    )

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(
      'Administrator access does not grant knowledge access.',
    )
    expect(
      screen.getByRole('link', { name: 'Manage PolarRAG instances' }),
    ).toHaveAttribute('href', '/instances?type=polarrag')
  })
})
