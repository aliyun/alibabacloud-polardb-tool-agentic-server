import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from './client'
import {
  checkPolarRAGInstance,
  createEnterprisePrincipal,
  createPolarRAGInstance,
  deleteEnterprisePrincipal,
  disablePolarRAGInstance,
  disablePolarRAGSpace,
  enablePolarRAGSpace,
  listEnterprisePrincipals,
  listPolarRAGInstances,
  listPolarRAGSpaces,
  syncPolarRAGSpace,
  updateEnterprisePrincipal,
  updatePolarRAGInstance,
} from './polarrag'

vi.mock('./client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}))

describe('PolarRAG admin API client', () => {
  beforeEach(() => vi.clearAllMocks())

  it('uses fixed instance and Space administration routes', async () => {
    const instanceInput = {
      name: 'Primary RAG',
      scheme: 'https' as const,
      host: 'rag.example.test',
      port: 9200,
      username: 'pas-service',
      password: 'secret',
      tls_verify: true,
      ca_bundle: null,
    }
    const updateInput = {
      name: 'Primary RAG 2',
      password: 'rotated',
    }

    await listPolarRAGInstances()
    await createPolarRAGInstance(instanceInput)
    await updatePolarRAGInstance('instance/a', updateInput)
    await checkPolarRAGInstance('instance/a')
    await disablePolarRAGInstance('instance/a')
    await listPolarRAGSpaces('instance/a')
    await enablePolarRAGSpace('instance/a', 'space/a')
    await syncPolarRAGSpace('instance/a', 'space/a')
    await disablePolarRAGSpace('instance/a', 'space/a')

    expect(api.get).toHaveBeenNthCalledWith(1, '/api/polarrag/instances')
    expect(api.post).toHaveBeenNthCalledWith(
      1,
      '/api/polarrag/instances',
      instanceInput,
    )
    expect(api.patch).toHaveBeenCalledWith(
      '/api/polarrag/instances/instance%2Fa',
      updateInput,
    )
    expect(api.post).toHaveBeenNthCalledWith(
      2,
      '/api/polarrag/instances/instance%2Fa/check',
    )
    expect(api.delete).toHaveBeenCalledWith(
      '/api/polarrag/instances/instance%2Fa',
    )
    expect(api.get).toHaveBeenNthCalledWith(
      2,
      '/api/polarrag/instances/instance%2Fa/spaces',
    )
    expect(api.post).toHaveBeenNthCalledWith(
      3,
      '/api/polarrag/instances/instance%2Fa/spaces/enable',
      { space_id: 'space/a' },
    )
    expect(api.post).toHaveBeenNthCalledWith(
      4,
      '/api/polarrag/instances/instance%2Fa/spaces/space%2Fa/sync',
    )
    expect(api.delete).toHaveBeenNthCalledWith(
      2,
      '/api/polarrag/instances/instance%2Fa/spaces/space%2Fa',
    )
  })

  it('uses server-controlled principal routes and allowlisted payloads', async () => {
    const input = {
      identity_domain: 'tenant-a',
      provider: 'feishu' as const,
      principal_type: 'user' as const,
      principal_id: 'ou-1',
      valid_until: null,
    }

    await listEnterprisePrincipals('user/a')
    await createEnterprisePrincipal('user/a', input)
    await updateEnterprisePrincipal('user/a', 'principal/a', {
      status: 'disabled',
      valid_until: null,
    })
    await deleteEnterprisePrincipal('user/a', 'principal/a')

    expect(api.get).toHaveBeenCalledWith(
      '/api/polarrag/users/user%2Fa/principals',
    )
    expect(api.post).toHaveBeenCalledWith(
      '/api/polarrag/users/user%2Fa/principals',
      input,
    )
    expect(api.patch).toHaveBeenCalledWith(
      '/api/polarrag/users/user%2Fa/principals/principal%2Fa',
      { status: 'disabled', valid_until: null },
    )
    expect(api.delete).toHaveBeenCalledWith(
      '/api/polarrag/users/user%2Fa/principals/principal%2Fa',
    )
  })
})
