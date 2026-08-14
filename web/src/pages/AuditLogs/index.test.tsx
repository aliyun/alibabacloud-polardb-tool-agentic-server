import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from '../../api/client'
import AuditLogs from './index'

vi.mock('../../api/client', () => ({
  default: { get: vi.fn() },
}))

const polarragAudit = {
  id: 'audit-rag-1',
  user_id: 'user-1',
  agent_id: null,
  category: 'polarrag',
  instance_id: null,
  action: 'polarrag.kb_search',
  sql_text: null,
  sql_type: null,
  status: 'success',
  error_message: null,
  error_code: null,
  duration_ms: 123,
  row_count: null,
  client_info: null,
  user_name: 'Alice',
  agent_name: null,
  instance_name: null,
  db_name: null,
  target_type: 'knowledge_resource',
  target_id: 'resource-1',
  request_id: 'request-1',
  polarrag: {
    instance_ids: ['rag-1'],
    instance_names: ['xiaoyuan-polarrag'],
    space_ids: ['mcp-e2e-space'],
    space_names: ['PAS MCP E2E Space'],
    kb_ids: ['kb-public-e2e'],
    kb_names: ['kb-public-e2e'],
    knowledge_resource_ids: ['resource-1'],
    knowledge_resource_names: ['kb-public-e2e'],
    hit_count: 7,
    successful_searches: 1,
    failed_searches: 0,
    partial_failure_count: 1,
    polarrag_status: 'success',
  },
  created_at: '2026-08-05T04:09:10Z',
}

describe('Audit Logs', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.get).mockImplementation((_url, config) => {
      const category = (
        config?.params as Record<string, unknown> | undefined
      )?.category
      return Promise.resolve({
        data: {
          items: category === 'polarrag' ? [polarragAudit] : [],
          total: category === 'polarrag' ? 1 : 0,
        },
      } as never)
    })
  })

  it('shows PolarRAG actions and resolved catalog coordinates in its tab', async () => {
    const user = userEvent.setup()
    render(<AuditLogs />)

    await user.click(await screen.findByRole('tab', { name: 'PolarRAG' }))

    await waitFor(() =>
      expect(api.get).toHaveBeenCalledWith('/api/audit-logs', {
        params: {
          category: 'polarrag',
          offset: 0,
          limit: 50,
        },
      }),
    )
    const row = await screen.findByRole('row', {
      name: /Alice polarrag\.kb_search/i,
    })
    const audit = within(row)
    expect(audit.getByText('xiaoyuan-polarrag')).toBeInTheDocument()
    expect(audit.getByText('PAS MCP E2E Space')).toBeInTheDocument()
    expect(audit.getByText('kb-public-e2e')).toBeInTheDocument()
    expect(audit.getByText('7')).toBeInTheDocument()
    expect(audit.getByText('1')).toBeInTheDocument()
  })

  it('keeps SQL and combined audit views on the same page', async () => {
    render(<AuditLogs />)

    expect(await screen.findByRole('tab', { name: 'All' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'SQL' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'PolarRAG' })).toBeInTheDocument()
    expect(
      screen.getByText(/database and PolarRAG MCP audit trail/i),
    ).toBeInTheDocument()
  })
})
