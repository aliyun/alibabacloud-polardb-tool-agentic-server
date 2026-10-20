import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import api from '../../api/client'
import { createTestI18n } from '../../i18n/i18n'
import LocaleProvider from '../../i18n/LocaleProvider'
import DocumentManager from './DocumentManager'

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    default: { post: vi.fn(), delete: vi.fn() },
  }
})

const resource = {
  knowledge_resource_id: 'resource-1',
  knowledge_space_name: 'Engineering',
  polarrag_instance_name: 'Primary RAG',
  name: 'Public KB',
}

function renderedDocumentIds(): string[] {
  return Array.from(document.querySelectorAll('tbody tr'))
    .map((row) => row.getAttribute('data-row-key'))
    .filter((value): value is string => value !== null)
}

describe('DocumentManager', () => {
  it('keeps upstream cursor order by default and supports current-page sorting', async () => {
    vi.mocked(api.post).mockResolvedValue({
      data: {
        documents: [
          { doc_id: 'doc-old', filename: 'zeta.txt', file_size_bytes: 100, created_at: '2026-08-20T10:00:00Z' },
          { doc_id: 'doc-new', filename: 'alpha.txt', file_size_bytes: 300, created_at: '2026-08-22T10:00:00Z' },
          { doc_id: 'doc-middle', filename: 'middle.txt', file_size_bytes: 200, created_at: '2026-08-21T10:00:00Z' },
        ],
        next_after_doc_id: null,
      },
    } as never)
    const user = userEvent.setup()

    render(
      <LocaleProvider i18nInstance={createTestI18n('en-US')}>
        <DocumentManager resource={resource} agentId="agent-1" onClose={vi.fn()} />
      </LocaleProvider>,
    )

    await waitFor(() =>
      expect(renderedDocumentIds()).toEqual(['doc-old', 'doc-new', 'doc-middle']),
    )

    await user.click(screen.getByRole('columnheader', { name: 'Size' }))

    expect(renderedDocumentIds()).toEqual(['doc-old', 'doc-middle', 'doc-new'])
  })

  it('uses page and total when PolarRAG returns the 1.0.7 list shape', async () => {
    vi.mocked(api.post).mockImplementation((_url, body) => {
      const page = (body as { page: number }).page
      return Promise.resolve({
        data: {
          documents: [{ doc_id: `doc-${page}`, updated_at: '2026-09-14T10:00:00Z' }],
          total: 21,
          page,
          size: 20,
          has_more: page === 1,
          next_after_doc_id: null,
        },
      } as never)
    })
    const user = userEvent.setup()

    render(
      <LocaleProvider i18nInstance={createTestI18n('en-US')}>
        <DocumentManager resource={resource} agentId="agent-1" onClose={vi.fn()} />
      </LocaleProvider>,
    )

    expect(await screen.findByText('doc-1')).toBeInTheDocument()
    expect(screen.getByText('Page 1 · 21 documents')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Next' }))
    expect(await screen.findByText('doc-2')).toBeInTheDocument()
    expect(
      vi.mocked(api.post).mock.calls.some(([, body]) =>
        (body as { after_doc_id: string | null; page: number }).after_doc_id === null
        && (body as { page: number }).page === 2,
      ),
    ).toBe(true)
  })

  it('shows document permission actions and principal details', async () => {
    vi.mocked(api.post).mockResolvedValue({
      data: {
        documents: [
          {
            doc_id: 'doc-acl',
            filename: 'permissions.md',
            acl: {
              identity_domain: 'tenant-a',
              state: 'ACTIVE',
              updating: false,
              acl_version: 3,
              acl_set_id: 'acl-set-a',
              grants: [
                {
                  principal: { provider: 'feishu', type: 'group', id: 'engineering' },
                  actions: ['READ', 'MANAGE'],
                },
              ],
            },
          },
        ],
        next_after_doc_id: null,
      },
    } as never)
    const user = userEvent.setup()

    render(
      <LocaleProvider i18nInstance={createTestI18n('en-US')}>
        <DocumentManager resource={resource} agentId="agent-1" onClose={vi.fn()} />
      </LocaleProvider>,
    )

    expect(await screen.findByRole('columnheader', { name: 'Permissions' })).toBeInTheDocument()
    expect(screen.getByText('READ')).toBeInTheDocument()
    expect(screen.getByText('MANAGE')).toBeInTheDocument()
    await user.hover(screen.getByLabelText('View permissions for permissions.md'))
    expect(await screen.findByText('feishu · group · engineering')).toBeInTheDocument()
    expect(screen.getByText('tenant-a · ACL v3 · ACTIVE')).toBeInTheDocument()
  })

  it('shows force rechunk only for PolarRAG 1.0.7 or later', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: {
      documents: [{ doc_id: 'doc-a', filename: 'guide.md' }],
      next_after_doc_id: null,
    } } as never)
    const user = userEvent.setup()
    const view = render(
      <LocaleProvider i18nInstance={createTestI18n('en-US')}>
        <DocumentManager resource={{ ...resource, force_rechunk_supported: false }} agentId="agent-1" onClose={vi.fn()} />
      </LocaleProvider>,
    )
    await user.click(await screen.findByRole('button', { name: 'Rechunk guide.md' }))
    expect(screen.queryByRole('checkbox', { name: /force rechunk/i })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Start Rechunk' }))
    expect(api.post).toHaveBeenCalledWith(
      '/api/me/polarrag/documents/doc-a/rechunk',
      expect.objectContaining({ force: false }),
    )
    view.unmount()

    render(
      <LocaleProvider i18nInstance={createTestI18n('en-US')}>
        <DocumentManager resource={{ ...resource, force_rechunk_supported: true }} agentId="agent-1" onClose={vi.fn()} />
      </LocaleProvider>,
    )
    await user.click(await screen.findByRole('button', { name: 'Rechunk guide.md' }))
    await user.click(screen.getByRole('checkbox', { name: /force rechunk/i }))
    await user.click(screen.getByRole('button', { name: 'Start Rechunk' }))
    expect(api.post).toHaveBeenCalledWith(
      '/api/me/polarrag/documents/doc-a/rechunk',
      expect.objectContaining({ force: true }),
    )
  })

  it('deletes a document using the selected Agent and knowledge resource', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: {
      documents: [{ doc_id: 'doc-a', filename: 'guide.md' }],
      next_after_doc_id: null,
    } } as never)
    vi.mocked(api.delete).mockResolvedValue({ data: {
      doc_id: 'doc-a', status: 'DELETING', task_id: 'delete-task',
    } } as never)
    const user = userEvent.setup()
    render(
      <LocaleProvider i18nInstance={createTestI18n('en-US')}>
        <DocumentManager resource={resource} agentId="agent-1" onClose={vi.fn()} />
      </LocaleProvider>,
    )
    await user.click(await screen.findByRole('button', { name: 'Delete guide.md' }))
    await user.click(screen.getByRole('button', { name: 'Confirm delete' }))
    expect(api.delete).toHaveBeenCalledWith('/api/me/polarrag/documents/doc-a', {
      data: { agent_id: 'agent-1', knowledge_resource_id: 'resource-1' },
    })
  })
})
