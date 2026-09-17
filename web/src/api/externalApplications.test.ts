import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from './client'
import {
  createExternalApplication,
  rotateExternalApplicationSecret,
  testExternalApplication,
  updateExternalApplicationStatus,
} from './externalApplications'

vi.mock('./client', () => ({
  default: {
    post: vi.fn(),
    put: vi.fn(),
  },
}))

describe('External Applications API', () => {
  beforeEach(() => vi.clearAllMocks())

  it('registers an application with its target and Agent policy', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: {} } as never)

    await createExternalApplication({
      name: 'Knowledge assistant',
      targets: ['mcp', 'api'],
      agent_policy: 'caller_selectable',
      fixed_agent_id: null,
      secret_expires_at: null,
    })

    expect(api.post).toHaveBeenCalledWith(
      '/api/v1/external-auth/clients',
      {
        name: 'Knowledge assistant',
        targets: ['mcp', 'api'],
        agent_policy: 'caller_selectable',
        fixed_agent_id: null,
        secret_expires_at: null,
      },
    )
  })

  it('encodes client ids for management and test requests', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: {} } as never)
    vi.mocked(api.put).mockResolvedValue({ data: {} } as never)

    await rotateExternalApplicationSecret('client/one', null)
    await updateExternalApplicationStatus('client/one', 'disabled')
    await testExternalApplication('client/one', {
      subject_token: 'external-token',
      resource: 'https://pas.example.com/mcp',
      agent_id: null,
    })

    expect(api.post).toHaveBeenNthCalledWith(
      1,
      '/api/v1/external-auth/clients/client%2Fone/rotate-secret',
      { secret_expires_at: null },
    )
    expect(api.put).toHaveBeenCalledWith(
      '/api/v1/external-auth/clients/client%2Fone/status',
      { status: 'disabled' },
    )
    expect(api.post).toHaveBeenNthCalledWith(
      2,
      '/api/v1/external-auth/clients/client%2Fone/test',
      {
        subject_token: 'external-token',
        resource: 'https://pas.example.com/mcp',
        agent_id: null,
      },
    )
  })
})
