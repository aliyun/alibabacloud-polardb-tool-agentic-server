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
})
