import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from '../../api/client'
import LocaleProvider from '../../i18n/LocaleProvider'
import { createTestI18n } from '../../i18n/i18n'
import MyInstances from './index'

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    default: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  }
})

const memberAgent = {
  assignment_id: 'assignment-1',
  agent_id: 'agent-1',
  agent_name: 'Scoped Agent',
  agent_status: 'active' as const,
  polarrag_instances: [{ id: 'rag-1', name: 'Primary RAG' }],
  password_reveal_available: true,
  token: null,
}

const secondMemberAgent = {
  ...memberAgent,
  assignment_id: 'assignment-2',
  agent_id: 'agent-2',
  agent_name: 'Second Agent',
}

function knowledgeResourcesResponse(name: string, kbId: string) {
  return {
    data: {
      database_instances: [],
      knowledge_resources: [
        {
          knowledge_resource_id: `resource-${kbId}`,
          knowledge_space_id: 'space-1',
          knowledge_space_name: 'Engineering',
          polarrag_instance_id: 'rag-1',
          polarrag_instance_name: 'Primary RAG',
          name,
          kb_id: kbId,
          kb_type: 'PUBLIC',
          usage: null,
          upload_ready: true,
        },
      ],
    },
  }
}

function deferred<T>() {
  let resolve: (value: T) => void
  let reject: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve: resolve!, reject: reject! }
}

async function selectKnowledgeBases(user: ReturnType<typeof userEvent.setup>) {
  await user.click(
    await screen.findByRole('button', {
      name: /(?:knowledge bases for Scoped Agent|Scoped Agent 的知识库)/i,
    }),
  )
}

describe('My Instances page', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows current-user database instances and knowledge spaces without listing KBs', async () => {
    vi.mocked(api.get).mockImplementation((url) =>
      Promise.resolve({
        data:
          url === '/api/me/agent-connections'
            ? [memberAgent]
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
    expect(screen.getByText('Enterprise Space')).toBeInTheDocument()
    expect(screen.queryByText('Public KB')).not.toBeInTheDocument()
    expect(screen.getByText('Primary RAG')).toBeInTheDocument()
    expect(
      screen.queryByRole('columnheader', { name: 'Usage' }),
    ).not.toBeInTheDocument()
    expect(screen.queryByText('Not specified')).not.toBeInTheDocument()
    expect(api.get).toHaveBeenCalledWith('/api/me/resources')
  })

  it('opens the selected Agent knowledge bases in a drawer with the resource ID', async () => {
    const user = userEvent.setup()
    vi.mocked(api.get).mockImplementation((url, config) => {
      if (url === '/api/me/agent-connections') {
        return Promise.resolve({
          data: [
            {
              assignment_id: 'assignment-1',
              agent_id: 'agent-1',
              agent_name: 'Scoped Agent',
              agent_status: 'active',
              polarrag_instances: [{ id: 'rag-1', name: 'Primary RAG' }],
              password_reveal_available: true,
              token: null,
            },
          ],
        } as never)
      }
      if (!config) {
        return Promise.resolve({
          data: { database_instances: [], knowledge_resources: [] },
        } as never)
      }
      expect(config).toEqual({ params: { agent_id: 'agent-1' } })
      return Promise.resolve({
        data: {
          database_instances: [],
          knowledge_resources: [
            {
              knowledge_resource_id: 'resource-1',
              knowledge_space_id: 'space-1',
              knowledge_space_name: 'Engineering',
              polarrag_instance_id: 'rag-1',
              polarrag_instance_name: 'Primary RAG',
              name: 'Scoped KB',
              kb_id: 'kb-scoped',
              kb_type: 'PUBLIC',
              usage: null,
              upload_ready: true,
            },
          ],
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
        name: /knowledge bases for Scoped Agent/i,
      }),
    )

    const drawer = await screen.findByRole('dialog', {
      name: /knowledge bases for Scoped Agent/i,
    })
    expect(within(drawer).getByText('Scoped KB')).toBeInTheDocument()
    expect(within(drawer).getByText('resource-1')).toBeInTheDocument()
    expect(within(drawer).queryByText('kb-scoped')).not.toBeInTheDocument()
    expect(api.get).toHaveBeenCalledWith('/api/me/resources', {
      params: { agent_id: 'agent-1' },
    })
  })

  it('keeps the latest Agent knowledge bases when an earlier request resolves late', async () => {
    const user = userEvent.setup()
    const firstRequest = deferred<ReturnType<typeof knowledgeResourcesResponse>>()
    const secondRequest = deferred<ReturnType<typeof knowledgeResourcesResponse>>()
    vi.mocked(api.get).mockImplementation((url, config) => {
      if (url === '/api/me/agent-connections') {
        return Promise.resolve({
          data: [memberAgent, secondMemberAgent],
        } as never)
      }
      if (!config) return Promise.resolve(knowledgeResourcesResponse('Overview KB', 'overview')) as never
      const agentId = (config as { params: { agent_id: string } }).params.agent_id
      return (agentId === memberAgent.agent_id
        ? firstRequest.promise
        : secondRequest.promise) as never
    })

    render(
      <MemoryRouter>
        <MyInstances />
      </MemoryRouter>,
    )

    await selectKnowledgeBases(user)
    await user.click(
      await screen.findByRole('button', {
        name: /knowledge bases for Second Agent/i,
      }),
    )
    secondRequest.resolve(knowledgeResourcesResponse('KB Agent B', 'kb-b'))
    expect(await screen.findByText('KB Agent B')).toBeInTheDocument()

    await act(async () => {
      firstRequest.resolve(knowledgeResourcesResponse('KB Agent A', 'kb-a'))
      await firstRequest.promise
    })
    expect(screen.queryByText('KB Agent A')).not.toBeInTheDocument()
    expect(screen.getByText('KB Agent B')).toBeInTheDocument()
  })

  it('clears the previous Agent knowledge bases when the new request fails', async () => {
    const user = userEvent.setup()
    vi.mocked(api.get).mockImplementation((url, config) => {
      if (url === '/api/me/agent-connections') {
        return Promise.resolve({
          data: [memberAgent, secondMemberAgent],
        } as never)
      }
      if (!config) return Promise.resolve(knowledgeResourcesResponse('Overview KB', 'overview')) as never
      const agentId = (config as { params: { agent_id: string } }).params.agent_id
      return agentId === memberAgent.agent_id
        ? Promise.resolve(knowledgeResourcesResponse('KB Agent A', 'kb-a'))
        : Promise.reject(new Error('network failed'))
    })

    render(
      <MemoryRouter>
        <MyInstances />
      </MemoryRouter>,
    )

    await selectKnowledgeBases(user)
    expect(await screen.findByText('KB Agent A')).toBeInTheDocument()
    await user.click(
      await screen.findByRole('button', {
        name: /knowledge bases for Second Agent/i,
      }),
    )

    expect(
      await screen.findByText('Could not load your accessible resources.'),
    ).toBeInTheDocument()
    expect(screen.queryByText('KB Agent A')).not.toBeInTheDocument()
  })

  it('lets a member upload to an upload-ready visible KB', async () => {
    const user = userEvent.setup()
    vi.mocked(api.get).mockImplementation((url) =>
      Promise.resolve({
        data:
          url === '/api/me/agent-connections'
            ? [memberAgent]
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
    await selectKnowledgeBases(user)
    await user.click(await screen.findByRole('button', { name: /upload to Public KB/i }))
    const dialogs = await screen.findAllByRole('dialog')
    const dialog = dialogs[dialogs.length - 1]!
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
    expect((body as FormData).get('agent_id')).toBe('agent-1')
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
            ? [memberAgent]
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
      <LocaleProvider i18nInstance={createTestI18n('zh-CN')}>
        <MemoryRouter>
          <MyInstances />
        </MemoryRouter>
      </LocaleProvider>,
    )

    await selectKnowledgeBases(user)

    const upload = await screen.findByRole('button', {
      name: /upload to Public KB/i,
    })
    expect(upload).toBeDisabled()
    await user.hover(upload.parentElement!)
    expect(await screen.findByRole('tooltip')).toHaveTextContent(
      '请联系管理员为该 Space 配置并验证 OSS AccessKey 凭据。',
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

    expect(await screen.findByText('Engineering')).toBeInTheDocument()
    expect(screen.queryByText('Public KB')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /upload/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /manage documents/i })).not.toBeInTheDocument()
  })

  it('loads, refreshes, and paginates readable documents on demand', async () => {
    const user = userEvent.setup()
    vi.mocked(api.get).mockImplementation((url) =>
      Promise.resolve({
        data:
          url === '/api/me/agent-connections'
            ? [memberAgent]
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
                  file_size_bytes: 1536,
                  created_at: '2026-08-12T03:04:05Z',
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
    await selectKnowledgeBases(user)
    await user.click(
      await screen.findByRole('button', {
        name: /manage documents for Public KB/i,
      }),
    )
    const dialogs = await screen.findAllByRole('dialog')
    const manager = dialogs[dialogs.length - 1]!

    expect(await within(manager).findByText('first.md')).toBeInTheDocument()
    expect(within(manager).getByText('CHUNKING')).toBeInTheDocument()
    expect(api.post).toHaveBeenCalledWith(
      '/api/me/polarrag/documents/_list',
      {
        agent_id: 'agent-1',
        knowledge_resource_id: 'resource-1',
        size: 20,
        after_doc_id: null,
      },
    )

    await user.click(within(manager).getByRole('button', { name: 'Next' }))
    expect(await within(manager).findByText('second.md')).toBeInTheDocument()
    expect(within(manager).getByRole('columnheader', { name: 'Size' })).toBeInTheDocument()
    expect(within(manager).getByRole('columnheader', { name: 'Chunks' })).toBeInTheDocument()
    expect(within(manager).getByRole('columnheader', { name: 'Uploaded' })).toBeInTheDocument()
    expect(within(manager).queryByRole('columnheader', { name: 'Generation' })).not.toBeInTheDocument()
    expect(within(manager).getByText('1.5 KiB')).toBeInTheDocument()
    expect(within(manager).getByText('12')).toBeInTheDocument()
    expect(within(manager).getByText(/Aug 12, 2026/)).toBeInTheDocument()
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
            ? [memberAgent]
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
    await selectKnowledgeBases(user)
    await user.click(
      await screen.findByRole('button', {
        name: /manage documents for Public KB/i,
      }),
    )
    const dialogs = await screen.findAllByRole('dialog')
    const manager = dialogs[dialogs.length - 1]!
    await user.type(within(manager).getByLabelText(/document filename/i), 'guide')
    await user.click(within(manager).getByRole('button', { name: /^find$/i }))

    expect(await within(manager).findByText('guide.md')).toBeInTheDocument()
    expect(api.post).toHaveBeenCalledWith(
      '/api/me/polarrag/documents/_find',
      {
        agent_id: 'agent-1',
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
          agent_id: 'agent-1',
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
        {
          data: {
            agent_id: 'agent-1',
            knowledge_resource_id: 'resource-1',
          },
        },
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

  it('shows an empty knowledge-spaces state when an administrator has no resources', async () => {
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

    expect(await screen.findByText('No accessible knowledge spaces')).toBeInTheDocument()
  })
})
